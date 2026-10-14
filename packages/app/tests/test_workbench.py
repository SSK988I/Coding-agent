from __future__ import annotations

import asyncio
import hashlib
import subprocess

import pytest
from agent_core import AgentContext, BeforeToolCallContext, SessionManager
from agent_llm import AssistantMessage, AssistantMessageEventStream, Model, ModelCost, TextContent, ToolCall, UserMessage

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.desktop.protocol import RpcError
from coding_agent.desktop.runtime import DesktopRuntime
from coding_agent.desktop.side_conversations import SideConversations
from coding_agent.desktop.workbench import WorkspaceWorkbench


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], stdin=subprocess.DEVNULL, capture_output=True, check=True).stdout


def repository(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "core.autocrlf", "false")
    (root / "示例 file.py").write_text("original\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    return root


def test_changes_sources_leave_index_and_external_helpers_untouched(tmp_path, monkeypatch):
    root = repository(tmp_path)
    workbench = WorkspaceWorkbench(root, "session", tmp_path / "cache")
    sentinel = tmp_path / "sentinel"
    git(root, "config", "diff.external", f"touch {sentinel}")
    git(root, "config", "diff.test.textconv", f"touch {sentinel}")
    (root / ".gitattributes").write_text("*.py diff=test\n")
    (root / "示例 file.py").write_text("changed\nextra\n", encoding="utf-8")
    (root / "untracked.py").write_text("new\n", encoding="utf-8")
    index = root / ".git" / "index"
    original = hashlib.sha256(index.read_bytes()).digest()
    unstaged = workbench.changes({"source": "unstaged"})
    assert {file["path"] for file in unstaged["files"]} == {".gitattributes", "untracked.py", "示例 file.py"}
    changed = next(file for file in unstaged["files"] if file["path"] == "示例 file.py")
    assert changed["additions"] == 2 and changed["deletions"] == 1
    assert hashlib.sha256(index.read_bytes()).digest() == original
    assert not sentinel.exists()
    git(root, "add", "示例 file.py")
    staged = workbench.changes({"source": "staged"})
    assert [file["path"] for file in staged["files"]] == ["示例 file.py"]
    seen = []
    run = subprocess.run
    def inspect(argv, **kwargs):
        seen.append((argv, kwargs))
        return run(argv, **kwargs)
    monkeypatch.setattr(subprocess, "run", inspect)
    workbench.changes({"source": "staged"})
    for argv, options in seen:
        assert options["stdin"] == subprocess.DEVNULL
        if "diff" in argv:
            assert {"--no-ext-diff", "--no-textconv", "--no-color"} <= set(argv)
    assert not sentinel.exists()


def test_session_and_last_turn_baselines_survive_reopen(tmp_path):
    root = repository(tmp_path)
    workbench = WorkspaceWorkbench(root, "session", tmp_path / "cache")
    index = (root / ".git" / "index").read_bytes()
    asyncio.run(workbench.capture())
    (root / "示例 file.py").write_text("first task\n", encoding="utf-8")
    assert workbench.changes({"source": "turn"})["files"][0]["deletions"] == 1
    asyncio.run(workbench.capture())
    (root / "示例 file.py").write_text("second task\n", encoding="utf-8")
    restored = WorkspaceWorkbench(root, "session", tmp_path / "cache")
    assert "-first task" in restored.changes({"source": "turn"})["files"][0]["diff"]
    assert "-original" in restored.changes({"source": "session"})["files"][0]["diff"]
    assert (root / ".git" / "index").read_bytes() == index


def test_file_preview_and_scope_reject_escape(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "readme.md").write_text("# 标题\n", encoding="utf-8", newline="\n")
    (workspace / "data.bin").write_bytes(b"\0\xff")
    workbench = WorkspaceWorkbench(workspace, "session", tmp_path / "cache")
    assert workbench.read_file({"path": "readme.md"})["text"] == "# 标题\n"
    assert workbench.read_file({"path": "data.bin"})["binary"] is True
    assert {entry["name"] for entry in workbench.list_files({})["entries"]} == {"readme.md", "data.bin"}
    with pytest.raises(RpcError, match="工作区"):
        workbench.read_file({"path": "../outside"})
    with pytest.raises(RpcError):
        workbench.path("bad\x00path")


def test_non_git_and_missing_baseline_are_explicit(tmp_path):
    workbench = WorkspaceWorkbench(tmp_path, "session", tmp_path / "cache")
    with pytest.raises(RpcError) as error:
        workbench.changes({"source": "unstaged"})
    assert error.value.code == "NOT_GIT_REPOSITORY"
    with pytest.raises(RpcError) as error:
        workbench.changes({"source": "turn"})
    assert error.value.code == "BASELINE_UNAVAILABLE"


def make_parent(tmp_path):
    return AgentSession(AgentSessionConfig(
        cwd=str(tmp_path), model=Model(id="test", provider="test", cost=ModelCost(0, 0, 0, 0)),
        session_manager=SessionManager.create(cwd=str(tmp_path), sessions_dir=tmp_path / "sessions"),
    ))


def test_side_conversation_keeps_parent_context_and_run_independent(tmp_path):
    async def scenario():
        parent = make_parent(tmp_path)
        parent.session_manager.append_message(UserMessage(content="PRIVATE MAIN CONVERSATION"))
        parent.agent.load_messages(parent.session_manager.build_session_context().messages)
        entered = asyncio.Event()
        class Blocked:
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration
            async def result(self):
                entered.set()
                await asyncio.Event().wait()
        parent.agent.stream_fn = lambda *_: Blocked()
        main = asyncio.create_task(parent.prompt("Main task"))
        await entered.wait()
        side = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "ask")
        snapshot = side.open()
        live = side.records[snapshot["conversationId"]]
        contexts = []
        def answer(model, context, options=None):
            contexts.append(context)
            stream = AssistantMessageEventStream()
            message = AssistantMessage(content=[TextContent(text="Independent answer")])
            stream.push({"type": "done", "reason": "stop", "message": message})
            return stream
        live.session.agent.stream_fn = answer
        before = list(parent.state.messages)
        side.send(snapshot["conversationId"], "Side question")
        await live.task
        assert live.status == "settled" and not main.done()
        assert parent.state.messages == before
        assert "PRIVATE MAIN" not in repr(contexts[0])
        assert not {"submit_plan", "request_user_input", "subagent_spawn"} & {tool.name for tool in live.session.tools}
        assert all(not tool.prefer_external for tool in live.session.tools if tool.name in {"grep", "find"})
        await side.close()
        assert not main.done()
        resumed = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "ask")
        assert len(resumed.list()) == 1
        restored = resumed.open(snapshot["conversationId"])
        assert restored["status"] == "settled" and len(restored["messages"]) == 2
        await resumed.close()
        await parent.abort()
        main.cancel()
        await asyncio.gather(main, return_exceptions=True)
        await parent.aclose()
    asyncio.run(scenario())


def test_side_stop_does_not_stop_parent_and_recovery_never_restarts(tmp_path):
    async def scenario():
        parent = make_parent(tmp_path)
        side = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "ask")
        identity = side.open()["conversationId"]
        live = side.records[identity]
        entered = asyncio.Event()
        class Blocked:
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration
            async def result(self):
                entered.set()
                await asyncio.Event().wait()
        live.session.agent.stream_fn = lambda *_: Blocked()
        side.send(identity, "Do not auto restart")
        await entered.wait()
        await side.stop(identity)
        assert live.status == "cancelled" and not parent._retry_abort_event.is_set()
        await side.close()
        status = tmp_path / "cache" / "chats" / f"{identity}.status.json"
        status.write_text('{"status":"running"}')
        resumed = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "ask")
        assert resumed.open(identity)["status"] == "uncertain"
        assert resumed.records[identity].task is None
        await resumed.close()
        await parent.aclose()
    asyncio.run(scenario())


def test_runtime_rejects_stale_tool_requests(tmp_path):
    async def scenario():
        runtime = DesktopRuntime(lambda *_: None)
        try:
            payload = await runtime.dispatch("workspace.open", {"path": str(tmp_path)})
            scope = {"sessionId": payload["sessionId"], "workspacePath": payload["path"]}
            await runtime.dispatch("workbench.files.list", scope)
            await runtime.dispatch("session.new", {})
            with pytest.raises(RpcError) as error:
                await runtime.dispatch("workbench.files.list", scope)
            assert error.value.code == "STALE_WORKSPACE"
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_snapshot_excludes_own_storage_and_preserves_missing_newlines(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    file = root / "counter.py"
    file.write_text("--old", encoding="utf-8")
    workbench = WorkspaceWorkbench(root, "session", root / "cache")
    asyncio.run(workbench.capture())
    file.write_text("++new", encoding="utf-8")
    changes = workbench.changes({"source": "turn"})
    assert len(changes["files"]) == 1
    changed = changes["files"][0]
    assert changed["additions"] == changed["deletions"] == 1
    assert "---old\n\\ No newline at end of file\n" in changed["diff"]
    assert "+++new\n\\ No newline at end of file\n" in changed["diff"]
    asyncio.run(workbench.capture())
    assert not workbench.changes({"source": "turn"})["files"]


def test_side_parallel_approvals_have_separate_decisions(tmp_path):
    async def scenario():
        parent = make_parent(tmp_path)
        side = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "ask")
        identity = side.open()["conversationId"]
        live = side.records[identity]
        def context(command):
            call = ToolCall(id=command, name="bash", arguments={"command": command})
            return BeforeToolCallContext(assistant_message=AssistantMessage(content=[call]), tool_call=call, args=call.arguments, context=AgentContext())
        signal = asyncio.Event()
        first = asyncio.create_task(side._approve(live, context("echo first"), signal))
        second = asyncio.create_task(side._approve(live, context("echo second"), signal))
        await asyncio.sleep(0)
        assert live.approval is not None
        first_id = live.approval["approvalId"]
        side.resolve(identity, first_id, True)
        assert await first is None
        await asyncio.sleep(0)
        assert live.approval is not None and live.approval["approvalId"] != first_id
        with pytest.raises(RpcError):
            side.resolve(identity, first_id, True)
        side.resolve(identity, live.approval["approvalId"], False)
        result = await second
        assert result is not None and result.block
        assert live.approval is None and live.decision is None
        await side.close()
        await parent.aclose()
    asyncio.run(scenario())


def test_side_save_failure_does_not_start_or_poison_status(tmp_path, monkeypatch):
    async def scenario():
        parent = make_parent(tmp_path)
        side = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "ask")
        identity = side.open()["conversationId"]
        live = side.records[identity]
        def failed_save(_live):
            raise OSError("disk full")
        monkeypatch.setattr(side, "_persist_status", failed_save)
        with pytest.raises(RpcError) as error:
            side.send(identity, "do not start")
        assert error.value.code == "SIDE_CHAT_SAVE_FAILED"
        assert live.status == "idle" and live.task is None
        await side.close()
        await parent.aclose()
    asyncio.run(scenario())


def test_existing_side_chat_cannot_keep_default_tools_after_parent_enters_plan(tmp_path):
    async def scenario():
        parent = make_parent(tmp_path)
        side = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "full_access")
        live = side.records[side.open()["conversationId"]]
        assert "write" in {tool.name for tool in live.session.tools}
        parent.enter_plan_mode()
        call = ToolCall(id="write", name="write", arguments={"path": "sample.py", "content": "change"})
        context = BeforeToolCallContext(assistant_message=AssistantMessage(content=[call]), tool_call=call, args=call.arguments, context=AgentContext())
        result = await side._approve(live, context, asyncio.Event())
        assert result is not None and result.block and result.code == "PARENT_TOOL_UNAVAILABLE"
        assert not (tmp_path / "sample.py").exists()
        await side.close()
        await parent.aclose()
    asyncio.run(scenario())


def test_immediate_side_stop_is_terminal_even_before_task_starts(tmp_path):
    async def scenario():
        parent = make_parent(tmp_path)
        side = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "ask")
        identity = side.open()["conversationId"]
        live = side.records[identity]
        async def blocked_prompt(_text):
            await asyncio.Event().wait()
        live.session.prompt = blocked_prompt
        side.send(identity, "stop immediately")
        snapshot = await side.stop(identity)
        assert snapshot["status"] == "cancelled"
        assert live.task is not None and live.task.done()
        side.send(identity, "close immediately")
        await side.close()
        assert live.status == "cancelled"
        await parent.aclose()
    asyncio.run(scenario())
