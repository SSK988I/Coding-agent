from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest
from agent_core import AgentContext, BeforeToolCallContext, SessionManager
from agent_llm import AssistantMessage, AssistantMessageEventStream, Model, ModelCost, TextContent, ToolCall, UserMessage

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.plan_mode import PlanState
from coding_agent.core.retry import RetryPolicy
from coding_agent.desktop.protocol import RpcError
from coding_agent.desktop.runtime import DesktopRuntime
from coding_agent.desktop.side_conversations import SideConversations
from coding_agent.desktop.workbench import HIDDEN_DIRECTORIES, MAX_PREVIEW_BYTES, WorkspaceWorkbench


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


def legacy_file_listing(workbench, params):
    directory = workbench.path(params.get("path", ""))
    show_hidden = bool(params.get("showHidden", False))
    entries = []
    for child in sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold())):
        if not show_hidden and (child.name.startswith(".") or child.name in HIDDEN_DIRECTORIES):
            continue
        outside = not child.resolve().is_relative_to(workbench.workspace)
        metadata = child.lstat()
        entries.append({
            "name": child.name, "path": child.relative_to(workbench.workspace).as_posix(),
            "kind": "directory" if child.is_dir() and not outside else "file",
            "size": metadata.st_size, "symlink": child.is_symlink(), "blocked": outside,
        })
        if len(entries) >= 500:
            break
    return {"path": directory.relative_to(workbench.workspace).as_posix(), "entries": entries, "truncated": len(entries) == 500}


@pytest.mark.parametrize("show_hidden", [False, True])
def test_file_listing_preserves_directory_first_casefold_order_and_hidden_rules(tmp_path, show_hidden):
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("z-dir", "A-dir", ".hidden-dir", "node_modules"):
        (root / name).mkdir()
    for name in ("Zulu.txt", "alpha.txt", "Beta.txt", ".hidden-file", "ß.txt", "ss.txt"):
        (root / name).write_text(name, encoding="utf-8")
    workbench = WorkspaceWorkbench(root, "session", tmp_path / "cache")
    params = {"showHidden": show_hidden}
    actual = workbench.list_files(params)
    assert actual == legacy_file_listing(workbench, params)
    tied = [child.name for child in root.iterdir() if child.name.casefold() == "ss.txt"]
    expected = ([".hidden-dir", "A-dir", "node_modules", "z-dir", ".hidden-file"] if show_hidden else ["A-dir", "z-dir"]) + ["alpha.txt", "Beta.txt", *tied, "Zulu.txt"]
    assert [entry["name"] for entry in actual["entries"]] == expected


@pytest.mark.parametrize("count", [499, 500, 501])
def test_file_listing_keeps_the_visible_500_entry_cap(tmp_path, count):
    root = tmp_path / "workspace"
    root.mkdir()
    for index in range(count):
        (root / f"file-{index:03}.txt").touch()
    (root / ".hidden").touch()
    workbench = WorkspaceWorkbench(root, "session", tmp_path / "cache")
    actual = workbench.list_files({})
    assert actual == legacy_file_listing(workbench, {})
    assert len(actual["entries"]) == min(count, 500)
    assert actual["truncated"] is (count >= 500)


def test_file_listing_preserves_internal_external_and_broken_symlinks(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "inside.txt").write_text("internal", encoding="utf-8")
    (root / "inside-dir").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("external", encoding="utf-8")
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    targets = {
        "inside-link": (root / "inside.txt", False, False, "file"),
        "inside-dir-link": (root / "inside-dir", True, False, "directory"),
        "outside-link": (outside, False, True, "file"),
        "outside-dir-link": (outside_dir, True, True, "file"),
        "broken-inside": (root / "missing.txt", False, False, "file"),
        "broken-outside": (tmp_path / "missing.txt", False, True, "file"),
    }
    for name, (target, directory, _, _) in targets.items():
        try:
            (root / name).symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"Symlink creation is unavailable: {exc}")
    workbench = WorkspaceWorkbench(root, "session", tmp_path / "cache")
    actual = workbench.list_files({})
    assert actual == legacy_file_listing(workbench, {})
    entries = {entry["name"]: entry for entry in actual["entries"]}
    for name, (_, _, blocked, kind) in targets.items():
        assert entries[name]["symlink"] is True
        assert entries[name]["blocked"] is blocked
        assert entries[name]["kind"] == kind
        assert entries[name]["size"] == (root / name).lstat().st_size


def test_file_listing_closes_the_scanner_and_maps_scan_errors(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "sample.txt").write_text("sample", encoding="utf-8")
    workbench = WorkspaceWorkbench(root, "session", tmp_path / "cache")
    original_scandir = os.scandir
    closed = []

    @contextmanager
    def tracked_scandir(path):
        with original_scandir(path) as scanner:
            try:
                yield scanner
            finally:
                closed.append(Path(path))

    monkeypatch.setattr(os, "scandir", tracked_scandir)
    assert workbench.list_files({})["entries"][0]["name"] == "sample.txt"
    assert closed == [root]

    def denied(_path):
        raise PermissionError("listing denied")

    monkeypatch.setattr(os, "scandir", denied)
    with pytest.raises(RpcError, match="listing denied") as error:
        workbench.list_files({})
    assert error.value.code == "FILE_READ_ERROR"


def test_file_listing_maps_entry_metadata_errors_after_closing_scanner(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    file = root / "sample.txt"
    file.touch()
    workbench = WorkspaceWorkbench(root, "session", tmp_path / "cache")
    closed = []

    class UnreadableEntry:
        name = "sample.txt"
        path = str(file)

        def is_dir(self):
            return False

        def stat(self, *, follow_symlinks=True):
            raise PermissionError("metadata denied")

    @contextmanager
    def failing_scandir(_path):
        try:
            yield iter([UnreadableEntry()])
        finally:
            closed.append(True)

    def denied_lstat(_path, *args, **kwargs):
        raise PermissionError("metadata denied")

    monkeypatch.setattr(os, "scandir", failing_scandir)
    monkeypatch.setattr(Path, "lstat", denied_lstat)
    with pytest.raises(RpcError, match="metadata denied") as error:
        workbench.list_files({})
    assert error.value.code == "FILE_READ_ERROR"
    assert closed == [True]


def test_file_listing_reuses_scanned_metadata_instead_of_restatting_each_path(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (root / name).write_text(name, encoding="utf-8")
    (root / "folder").mkdir()
    workbench = WorkspaceWorkbench(root, "session", tmp_path / "cache")
    expected = legacy_file_listing(workbench, {})
    original_stat = Path.stat
    entry_stats = []

    def counted_stat(path, *args, **kwargs):
        if path.parent == root:
            entry_stats.append(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counted_stat)
    assert workbench.list_files({}) == expected
    assert entry_stats == []


def test_file_listing_releases_processed_entries_and_their_metadata(tmp_path, monkeypatch):
    import gc
    import weakref

    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (root / name).touch()
    workbench = WorkspaceWorkbench(root, "session", tmp_path / "cache")
    expected = legacy_file_listing(workbench, {})
    original_scandir = os.scandir
    processed = []

    class ObservedEntry:
        def __init__(self, entry):
            self.entry = entry

        def __getattr__(self, name):
            return getattr(self.entry, name)

        def stat(self, **kwargs):
            gc.collect()
            assert all(reference() is None for reference in processed)
            processed.append(weakref.ref(self))
            return self.entry.stat(**kwargs)

    @contextmanager
    def observed_scandir(path):
        with original_scandir(path) as scanner:
            yield (ObservedEntry(entry) for entry in scanner)

    monkeypatch.setattr(os, "scandir", observed_scandir)
    assert workbench.list_files({}) == expected
    assert len(processed) == 3
    gc.collect()
    assert all(reference() is None for reference in processed)


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


def test_capture_serializes_once_and_preserves_original_session_baseline(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workbench = WorkspaceWorkbench(workspace, "session", tmp_path / "cache")
    snapshot = {"files": {"示例.txt": {"text": "first\n"}}}
    monkeypatch.setattr(workbench, "_snapshot", lambda: snapshot)
    original_dumps = json.dumps
    serialized_snapshots = []

    def counted_dumps(value, **kwargs):
        serialized_snapshots.append(value)
        return original_dumps(value, **kwargs)

    monkeypatch.setattr("coding_agent.desktop.workbench.json.dumps", counted_dumps)
    workbench._capture()
    session_file = workbench.storage / "session.json"
    turn_file = workbench.storage / "turn.json"
    original_session = session_file.read_text(encoding="utf-8")
    assert original_session == turn_file.read_text(encoding="utf-8")
    assert json.loads(original_session) == snapshot
    assert len(serialized_snapshots) == 1

    snapshot = {"files": {"示例.txt": {"text": "second\n"}}}
    workbench._capture()
    assert len(serialized_snapshots) == 2
    assert session_file.read_text(encoding="utf-8") == original_session
    assert json.loads(turn_file.read_text(encoding="utf-8")) == snapshot


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


@pytest.mark.parametrize("remaining", [0, 1, 2, 3, 6, 50])
def test_bounded_diff_preserves_utf8_preview_and_remaining_budget(remaining, monkeypatch):
    monkeypatch.setattr("coding_agent.desktop.workbench.MAX_PREVIEW_BYTES", 7)
    for diff in ("", "ASCII\n", "中文\n", "😀尾\n", "a\r\nb\n"):
        encoded = diff.encode("utf-8")
        limit = min(7, remaining)
        preview = encoded[:limit].decode("utf-8", errors="ignore")
        expected = preview, len(encoded) > limit, remaining - len(preview.encode("utf-8"))
        assert WorkspaceWorkbench._bounded_diff(diff, remaining) == expected, diff


def test_untruncated_diff_does_not_copy_or_decode_its_encoded_bytes():
    class NoSliceBytes(bytes):
        def __getitem__(self, key):
            pytest.fail("An untruncated diff must not slice and decode its encoded bytes")

        def decode(self, encoding="utf-8", errors="strict"):
            pytest.fail("An untruncated diff must reuse its original text")

    class DiffText(str):
        def encode(self, encoding="utf-8", errors="strict"):
            return NoSliceBytes(super().encode(encoding, errors))

    diff = DiffText("diff 内容 😀\n")
    budget = len(diff.encode("utf-8"))
    assert WorkspaceWorkbench._bounded_diff(diff, budget) == (diff, False, 0)


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


@pytest.mark.parametrize("transition", ["plan", "closed"])
def test_side_rechecks_parent_after_approval_before_real_write(tmp_path, monkeypatch, transition):
    async def scenario():
        parent = make_parent(tmp_path)
        waiting = asyncio.Event()
        def emit(_event, payload):
            if payload.get("approval"):
                waiting.set()
        side = SideConversations(parent, tmp_path / "cache", emit, lambda: "ask")
        calls = 0
        def provider(*_args):
            nonlocal calls
            calls += 1
            message = AssistantMessage(content=[ToolCall(id="write", name="write", arguments={"path": ".env.pending", "content": "not authorized"})]) if calls == 1 else AssistantMessage(content=[TextContent(text="done")])
            stream = AssistantMessageEventStream()
            stream.push({"type": "done", "reason": "tool_use" if calls == 1 else "stop", "message": message})
            return stream
        monkeypatch.setattr("agent_llm.compat.stream_simple", provider)
        identity = side.open()["conversationId"]
        live = side.records[identity]
        try:
            side.send(identity, "write a file")
            await asyncio.wait_for(waiting.wait(), 2)
            approval_id = live.approval["approvalId"]
            if transition == "plan":
                parent.enter_plan_mode()
                side.resolve(identity, approval_id, True)
            else:
                side.resolve(identity, approval_id, True)
                side.closed = True
            await asyncio.wait_for(live.task, 2)
            assert not (tmp_path / ".env.pending").exists()
            results = [message for message in live.session.state.messages if message.role == "toolResult"]
            assert results and results[0].is_error
        finally:
            await side.close()
            await parent.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("extra_files", [0, 8])
def test_untracked_previews_do_not_launch_per_file_git_diff(tmp_path, monkeypatch, extra_files):
    root = repository(tmp_path)
    (root / "示例 file.py").write_text("tracked change\n", encoding="utf-8", newline="\n")
    (root / "new.txt").write_text("new\nline\n", encoding="utf-8", newline="\n")
    (root / "角色 图.txt").write_text("图文\n", encoding="utf-8", newline="\n")
    (root / "empty.txt").touch()
    (root / "binary.bin").write_bytes(b"\0\xff")
    (root / "large.txt").write_bytes(b"x" * (MAX_PREVIEW_BYTES + 1))
    for index in range(extra_files):
        (root / f"extra-{index}.txt").write_text("extra\n", encoding="utf-8", newline="\n")
    workbench = WorkspaceWorkbench(root, "session", tmp_path / "cache")
    original_index = (root / ".git" / "index").read_bytes()
    seen = []
    run = subprocess.run

    def inspect(argv, **kwargs):
        seen.append((argv, kwargs))
        return run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", inspect)
    result = workbench.changes({"source": "unstaged"})

    assert len(seen) == 5, "Untracked files must not each start an empty git diff process"
    per_file = [argv for argv, _options in seen if "diff" in argv and "--name-only" not in argv]
    assert len(per_file) == 1 and per_file[0][-1] == "示例 file.py"
    for argv, options in seen:
        assert options["stdin"] == subprocess.DEVNULL
        assert options["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert "core.fsmonitor=false" in argv and "--no-pager" in argv
        if "diff" in argv:
            assert {"--no-ext-diff", "--no-textconv", "--no-color"} <= set(argv)
    assert (root / ".git" / "index").read_bytes() == original_index
    names = [file["path"] for file in result["files"]]
    assert names == sorted(names) and len(names) == 6 + extra_files
    assert result["additions"] == 4 + extra_files and result["deletions"] == 1
    assert not result["truncated"]
    files = {file["path"]: file for file in result["files"]}
    tracked = files.pop("示例 file.py")
    assert tracked["status"] == "M" and tracked["additions"] == tracked["deletions"] == 1
    assert "-original\n+tracked change\n" in tracked["diff"].replace("\r\n", "\n")
    assert all(file["status"] == "A" and file["deletions"] == 0 for file in files.values())
    assert files["new.txt"]["additions"] == 2 and "+new\n+line\n" in files["new.txt"]["diff"]
    assert files["角色 图.txt"]["additions"] == 1 and "+图文\n" in files["角色 图.txt"]["diff"]
    assert files["empty.txt"]["diff"] == "" and files["empty.txt"]["additions"] == 0
    assert files["binary.bin"]["binary"] and files["binary.bin"]["diff"] == "Binary files differ"
    assert not files["large.txt"]["binary"] and files["large.txt"]["diff"] == "File too large to preview"


@pytest.mark.parametrize("source,expected_calls", [("staged", 4), ("branch", 5)])
def test_tracked_change_sources_keep_per_file_git_diff(tmp_path, monkeypatch, source, expected_calls):
    root = repository(tmp_path)
    (root / "示例 file.py").write_text("staged change\n", encoding="utf-8", newline="\n")
    git(root, "add", "示例 file.py")
    (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    workbench = WorkspaceWorkbench(root, "session", tmp_path / "cache")
    seen = []
    run = subprocess.run

    def inspect(argv, **kwargs):
        seen.append(argv)
        return run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", inspect)
    result = workbench.changes({"source": source, "baseRef": "main"})

    assert len(seen) == expected_calls
    per_file = [argv for argv in seen if "diff" in argv and "--name-only" not in argv]
    assert len(per_file) == 1 and per_file[0][-1] == "示例 file.py"
    assert not any("ls-files" in argv for argv in seen)
    assert [file["path"] for file in result["files"]] == ["示例 file.py"]
    assert result["files"][0]["status"] == "M"
    assert "+staged change\n" in result["files"][0]["diff"]


@pytest.mark.parametrize("scenario_name", ["allow", "deny", "plan_before", "plan_during", "stop"])
def test_native_side_search_obeys_own_approval_and_current_parent(tmp_path, monkeypatch, scenario_name):
    async def scenario():
        parent = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=Model(id="test", provider="deepseek", cost=ModelCost(0, 0, 0, 0)),
            session_manager=SessionManager.create(cwd=str(tmp_path), sessions_dir=tmp_path / "sessions"),
            web_search_enabled=True,
        ))
        waiting = asyncio.Event()
        approvals, captured = [], []
        def emit(_event, payload):
            if payload.get("approval"):
                approvals.append(payload["approval"])
                waiting.set()
        def provider(_model, context, options):
            captured.append((context, options or {}))
            stream = AssistantMessageEventStream()
            stream.push({"type": "done", "reason": "stop", "message": AssistantMessage(content=[TextContent(text="done")])})
            return stream
        monkeypatch.setattr("agent_llm.compat.stream_simple", provider)
        side = SideConversations(parent, tmp_path / "cache", emit, lambda: "ask")
        identity = side.open()["conversationId"]
        live = side.records[identity]
        waiter = None
        try:
            if scenario_name == "plan_before":
                parent.enter_plan_mode()
            side.send(identity, "research")
            if scenario_name != "plan_before":
                waiter = asyncio.create_task(waiting.wait())
                await asyncio.wait({waiter, live.task}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
                assert waiting.is_set(), f"Native search started without a side approval: {live.status}, {live.error}, provider calls={len(captured)}"
                assert captured == [], "Provider started before native search approval"
                approval = live.approval
                assert approval["toolName"] == "web_search"
                if scenario_name == "stop":
                    await side.stop(identity)
                    assert captured == [] and live.approval is None
                    return
                if scenario_name == "plan_during":
                    parent.enter_plan_mode()
                side.resolve(identity, approval["approvalId"], scenario_name != "deny")
            await asyncio.wait_for(live.task, 2)
            assert bool(captured[0][1].get("web_search")) is (scenario_name == "allow")
            assert not any(tool.name == "web_search" for tool in (captured[0][0].tools or []))
            if scenario_name == "plan_before":
                assert approvals == []
        finally:
            if waiter is not None:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            await side.close()
            await parent.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("source", ["unstaged", "session", "turn"])
def test_diff_counts_keep_source_lines_that_look_like_file_headers(tmp_path, source):
    root = repository(tmp_path)
    file = root / "counter.py"
    file.write_text("-- counter;\ncontext\n", encoding="utf-8", newline="\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "counter")
    workbench = WorkspaceWorkbench(root, "session", tmp_path / "cache")
    asyncio.run(workbench.capture())
    file.write_text("++ counter;\ncontext\n", encoding="utf-8", newline="\n")
    changed = workbench.changes({"source": source})["files"][0]
    assert changed["additions"] == changed["deletions"] == 1
    assert "--- counter;\n+++ counter;" in changed["diff"]


@pytest.mark.parametrize("source", ["session", "turn"])
def test_baseline_diff_preview_is_byte_bounded_and_reports_truncation(tmp_path, source):
    root = tmp_path / "project"
    root.mkdir()
    file = root / "large.txt"
    file.write_bytes(b"a" * 600000 + b"\n")
    workbench = WorkspaceWorkbench(root, "session", tmp_path / "cache")
    asyncio.run(workbench.capture())
    file.write_bytes(b"b" * 600000 + b"\n")
    result = workbench.changes({"source": source})
    assert len(result["files"][0]["diff"].encode("utf-8")) <= MAX_PREVIEW_BYTES
    assert result["truncated"] and result["files"][0]["truncated"]


@pytest.mark.parametrize("source", ["unstaged", "session", "turn"])
def test_all_diff_sources_share_utf8_response_budget(tmp_path, monkeypatch, source):
    root = repository(tmp_path)
    for index in range(3):
        (root / f"text{index}.txt").write_text("旧" * 200 + "\n", encoding="utf-8", newline="\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "multi file baseline")
    workbench = WorkspaceWorkbench(root, "session", tmp_path / "cache")
    asyncio.run(workbench.capture())
    for index in range(3):
        (root / f"text{index}.txt").write_text("新" * 200 + "\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr("coding_agent.desktop.workbench.MAX_PREVIEW_BYTES", 1024)
    monkeypatch.setattr("coding_agent.desktop.workbench.MAX_DIFF_BYTES", 1500)
    result = workbench.changes({"source": source})
    assert len(result["files"]) == 3
    assert sum(len(file["diff"].encode("utf-8")) for file in result["files"]) <= 1500
    assert all(len(file["diff"].encode("utf-8")) <= 1024 for file in result["files"])
    assert result["truncated"] and all(file["truncated"] for file in result["files"])
    assert result["additions"] == result["deletions"] == 3


def test_native_search_approval_is_once_per_side_run_not_shared_with_parent(tmp_path, monkeypatch):
    async def scenario():
        parent = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=Model(id="test", provider="deepseek", cost=ModelCost(0, 0, 0, 0)),
            web_search_enabled=True,
        ))
        (tmp_path / "sample.txt").write_text("sample", encoding="utf-8")
        approvals, captured = [], []
        waiting = asyncio.Event()
        def emit(_event, payload):
            approval = payload.get("approval")
            if approval:
                approvals.append(approval)
                waiting.set()
        def provider(_model, _context, options):
            captured.append(options or {})
            message = AssistantMessage(content=[ToolCall(id="read", name="read", arguments={"path": "sample.txt"})]) if len(captured) == 1 else AssistantMessage(content=[TextContent(text="done")])
            stream = AssistantMessageEventStream()
            stream.push({"type": "done", "reason": "tool_use" if len(captured) == 1 else "stop", "message": message})
            return stream
        monkeypatch.setattr("agent_llm.compat.stream_simple", provider)
        side = SideConversations(parent, tmp_path / "cache", emit, lambda: "ask")
        identity = side.open()["conversationId"]
        live = side.records[identity]
        try:
            for run in range(2):
                waiting.clear()
                side.send(identity, "research")
                await asyncio.wait_for(waiting.wait(), 2)
                assert len(captured) == (0 if run == 0 else 2)
                side.resolve(identity, live.approval["approvalId"], True)
                await asyncio.wait_for(live.task, 2)
            ids = {approval["approvalId"] for approval in approvals}
            assert len(ids) == 2
            assert len(captured) == 3 and all(options.get("web_search") for options in captured)
            assert parent.state.messages == []
        finally:
            await side.close()
            await parent.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["unchanged", "plan", "recovery_error", "tool_removed", "closed", "stop"])
def test_native_side_search_rechecks_parent_on_provider_retry(tmp_path, monkeypatch, transition):
    async def scenario():
        parent = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=Model(id="test", provider="deepseek", cost=ModelCost(0, 0, 0, 0)),
            web_search_enabled=True,
            retry_policy=RetryPolicy(max_retries=1, initial_delay=0, max_delay=0),
        ))
        approvals, captured, retries = [], [], []
        waiting = asyncio.Event()
        def emit(_event, payload):
            approval = payload.get("approval")
            if approval:
                approvals.append(approval["approvalId"])
                waiting.set()
        def provider(_model, context, options):
            captured.append((context, dict(options or {}), parent.collaboration_mode))
            stream = AssistantMessageEventStream()
            if len(captured) == 1:
                message = AssistantMessage(stop_reason="error", error_message="HTTP 503 service unavailable")
                stream.push({"type": "error", "reason": "error", "error": message})
            else:
                message = AssistantMessage(content=[TextContent(text="done")])
                stream.push({"type": "done", "reason": "stop", "message": message})
            return stream
        monkeypatch.setattr("agent_llm.compat.stream_simple", provider)
        side = SideConversations(parent, tmp_path / "cache", emit, lambda: "ask")
        identity = side.open()["conversationId"]
        live = side.records[identity]
        checks = []
        authorize = live.session._config.before_native_web_search
        async def checked_authorize(signal):
            allowed = await authorize(signal)
            checks.append(allowed)
            return allowed
        live.session._config.before_native_web_search = checked_authorize
        def on_retry(event):
            if event.get("type") != "retry":
                return
            retries.append(event)
            if transition == "plan":
                parent.enter_plan_mode()
            elif transition == "recovery_error":
                parent._plan_state = PlanState(mode="default", phase="recovery_error")
                parent._refresh_collaboration_runtime()
            elif transition == "tool_removed":
                parent._tools = [tool for tool in parent.tools if tool.name != "web_search"]
            elif transition == "closed":
                side.closed = True
            elif transition == "stop":
                live.session._retry_abort_event.set()
        live.session.on_event(on_retry)
        try:
            side.send(identity, "research")
            await asyncio.wait_for(waiting.wait(), 2)
            side.resolve(identity, live.approval["approvalId"], True)
            await asyncio.wait_for(live.task, 2)
            assert len(set(approvals)) == 1, "Retries must not repeat the same run's user approval"
            assert len(retries) == 1
            assert captured[0][1].get("web_search") is True
            if transition == "stop":
                assert len(captured) == 1
                return
            assert len(captured) == 2
            allowed = transition == "unchanged"
            assert bool(captured[1][1].get("web_search")) is allowed
            assert checks == [True, allowed], "Every actual Provider attempt must recheck the parent"
            for context, options, _mode in captured:
                assert not any(tool.name == "web_search" for tool in (context.tools or []))
                assert (context.system_prompt or "").count("<native_web_search>") == int(bool(options.get("web_search")))
            if transition == "plan":
                assert captured[1][2] == "plan"
            assert live.status == "settled"
        finally:
            await side.close()
            await parent.aclose()
    asyncio.run(scenario())
