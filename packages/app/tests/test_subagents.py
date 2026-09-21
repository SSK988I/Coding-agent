"""Offline execution, isolation, limits and branch ownership of subagents."""
from __future__ import annotations

import asyncio

import pytest
from agent_core import SessionManager
from agent_llm import AssistantMessage, AssistantMessageEventStream, Model, ModelCost, TextContent, ToolCall, UserMessage

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.subagents import SubagentError


def make_session(tmp_path, **kwargs):
    return AgentSession(AgentSessionConfig(
        cwd=str(tmp_path), model=Model(id="test", cost=ModelCost(0, 0, 0, 0)),
        session_manager=SessionManager.create(cwd=str(tmp_path), agent_dir=tmp_path), **kwargs,
    ))


def final_stream(message):
    stream = AssistantMessageEventStream()
    stream.push({"type": "done", "reason": message.stop_reason, "message": message})
    return stream


def test_readonly_child_uses_independent_context_and_persists_report(tmp_path):
    async def scenario():
        session = make_session(tmp_path)
        session.session_manager.append_message(UserMessage(content="PRIVATE PARENT CHAT"))
        session.agent.load_messages(session.session_manager.build_session_context().messages)
        observed = []

        def stream(model, context, options=None):
            observed.append(context)
            return final_stream(AssistantMessage(content=[TextContent(text="Checked src/app.py:12; findings need verification.")]))

        session.agent.stream_fn = stream
        spawned = session.subagents.spawn("Inspect src/app.py", "review")
        result = await session.subagents.wait(spawned["taskId"], 1)
        assert result["status"] == "completed" and result["turns"] == 1
        assert "src/app.py:12" in result["output"]
        assert len(observed[0].messages) == 1
        assert "PRIVATE" not in repr(observed[0])
        assert {tool.name for tool in observed[0].tools} == {
            "read", "grep", "find", "ls", "git_status", "git_log", "git_diff", "git_show",
        }
        assert len(session.state.messages) == 1
        assert "PRIVATE" in session.state.messages[0].content
        reopened = SessionManager.open(session.session_manager.path)
        other = make_session(tmp_path)
        other._attach_session_manager(reopened)
        assert other.subagents.get(result["taskId"])["output"] == result["output"]
        assert len(reopened.build_session_context().messages) == 1
        await other.aclose()
        await session.aclose()

    asyncio.run(scenario())


def test_child_cannot_mutate_or_submit_or_delegate(tmp_path):
    async def scenario():
        session = make_session(tmp_path)
        session.enter_plan_mode()
        before = session.plan_state.to_payload()
        calls = []

        def stream(model, context, options=None):
            calls.append(context)
            if len(calls) == 1:
                return final_stream(AssistantMessage(content=[
                    ToolCall(id=name, name=name, arguments={})
                    for name in ("bash", "write", "edit", "submit_plan", "subagent_spawn")
                ], stop_reason="tool_use"))
            assert all(m.is_error for m in context.messages if getattr(m, "role", None) == "toolResult")
            return final_stream(AssistantMessage(content=[TextContent(text="Cannot make changes; read-only report.")]))

        session.agent.stream_fn = stream
        task = session.subagents.spawn("Review only")
        result = await session.subagents.wait(task["taskId"], 1)
        assert result["status"] == "completed"
        assert session.plan_state.to_payload() == before
        assert session.subagents.snapshot() == [result]
        await session.aclose()

    asyncio.run(scenario())


def test_child_respects_parent_tools_and_python_search(tmp_path):
    session = make_session(tmp_path, excluded_tool_names=["read", "git_show"])
    child = session._make_readonly_subagent()
    assert not {"read", "git_show", "write", "bash"} & {t.name for t in child.state.tools}
    assert all(not t.prefer_external for t in child.state.tools if t.name in {"grep", "find"})
    disabled = make_session(tmp_path, no_tools=True)
    with pytest.raises(SubagentError, match="未启用"):
        disabled.subagents.spawn("inspect")


@pytest.mark.parametrize("action,expected", [("cancel", "cancelled"), ("timeout", "timed_out")])
def test_stop_closes_model_producer_and_reports_truthful_status(tmp_path, action, expected):
    async def scenario():
        session = make_session(tmp_path)
        started, closed = asyncio.Event(), asyncio.Event()

        def stream(*_args):
            result = AssistantMessageEventStream()

            async def producer():
                try:
                    started.set()
                    await asyncio.Event().wait()
                finally:
                    closed.set()

            result.set_producer(asyncio.create_task(producer()))
            return result

        session.agent.stream_fn = stream
        session.subagents.timeout_seconds = 0.05 if action == "timeout" else 180
        task = session.subagents.spawn("inspect")
        await started.wait()
        if action == "cancel":
            result = await session.subagents.cancel(task["taskId"])
        else:
            result = await session.subagents.wait(task["taskId"], 1)
        assert result["status"] == expected
        assert closed.is_set()
        await session.aclose()

    asyncio.run(scenario())


def test_child_turn_limit_and_recovery_of_orphan_started(tmp_path):
    async def scenario():
        session = make_session(tmp_path)
        session.subagents.max_turns = 1
        seen = []

        def stream(*args):
            seen.append(args)
            return final_stream(AssistantMessage(content=[ToolCall(id="ls", name="ls", arguments={})], stop_reason="tool_use"))

        session.agent.stream_fn = stream
        task = session.subagents.spawn("inspect")
        result = await session.subagents.wait(task["taskId"], 1)
        assert result["status"] == "failed" and "轮数" in result["error"]
        assert len(seen) == 1
        session.session_manager.append_subagent_task({**result, "taskId": "orphan", "status": "running"})
        assert session.subagents.get("orphan")["status"] == "uncertain"
        await session.aclose()

    asyncio.run(scenario())


def test_branch_switch_prevents_late_task_results_leaking(tmp_path):
    async def scenario():
        session = make_session(tmp_path)
        first = session.session_manager.append_message(UserMessage(content="first"))
        started = asyncio.Event()

        def stream(*_):
            result = AssistantMessageEventStream()
            async def produce():
                started.set()
                await asyncio.Event().wait()
            result.set_producer(asyncio.create_task(produce()))
            return result

        session.agent.stream_fn = stream
        task = session.subagents.spawn("private branch work")
        await started.wait()
        session.session_manager.set_leaf_id(first.id)
        session.refresh_plan_state_from_branch()
        await asyncio.sleep(0)
        assert session.subagents.snapshot() == []
        with pytest.raises(SubagentError):
            await session.subagents.wait(task["taskId"], 0)
        await session.aclose()
        assert session.subagents.snapshot() == []

    asyncio.run(scenario())


def test_spawn_respects_limits_and_does_not_run_if_persistence_fails(tmp_path, monkeypatch):
    async def scenario():
        session = make_session(tmp_path)
        calls = []
        session.agent.stream_fn = lambda *args: calls.append(args)
        manager = session.session_manager
        original_append = manager.append_subagent_task
        monkeypatch.setattr(manager, "append_subagent_task", lambda _: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError, match="disk full"):
            session.subagents.spawn("inspect")
        assert not calls and not session.subagents.snapshot()
        monkeypatch.setattr(manager, "append_subagent_task", original_append)
        session.subagents.max_active = 1
        task = session.subagents.spawn("first")
        with pytest.raises(SubagentError, match="最多"):
            session.subagents.spawn("second")
        with pytest.raises(SubagentError, match="子代理"):
            await session.compact(direction="implement")
        await session.subagents.cancel(task["taskId"])
        assert not calls  # cancelled before its coroutine started
        assert session.subagents.get(task["taskId"])["status"] == "cancelled"
        await session.aclose()

    asyncio.run(scenario())


def test_desktop_rpc_exposes_shared_task_snapshot_and_report(tmp_path):
    from coding_agent.desktop.runtime import DesktopRuntime

    async def scenario():
        session = make_session(tmp_path)
        session.agent.stream_fn = lambda *_: final_stream(AssistantMessage(content=[TextContent(text="report")]))
        events = []
        runtime = DesktopRuntime(events.append)
        runtime._session, runtime._workspace = session, tmp_path
        session.on_event(runtime._on_session_event)
        spawned = await runtime.dispatch("subagent.spawn", {"task": "inspect", "purpose": "review"})
        await session.subagents.wait(spawned["taskId"], 1)
        tasks = await runtime.dispatch("subagent.list", {})
        assert (await runtime.dispatch("subagent.status", {"taskId": spawned["taskId"]}))["taskId"] == spawned["taskId"]
        assert (await runtime.dispatch("subagent.wait", {"taskId": spawned["taskId"], "timeoutSeconds": 0}))["status"] == "completed"
        snapshot = await runtime.dispatch("session.snapshot", {})
        assert snapshot["subagents"] == tasks
        assert tasks[0]["output"] == "report"
        assert any(e["event"]["type"] == "subagent.stateChanged" for e in events)
        assert not any(e["event"]["type"].startswith("run.") for e in events)
        await session.aclose()

    asyncio.run(scenario())


def test_tui_task_card_and_commands_show_and_stop_tasks():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from coding_agent.modes.interactive.components.subagent import SubagentComponent
    from coding_agent.modes.interactive.interactive_mode import InteractiveMode

    task = {"taskId": "child-1", "prompt": "inspect", "status": "running", "turns": 1}
    card = SubagentComponent(task)
    assert "停止：/subagents cancel child-1" in "\n".join(card.render(100))
    card.update({**task, "status": "uncertain"})
    assert "结果未知" in "\n".join(card.render(100))
    assert "停止：" not in "\n".join(card.render(100))
    manager = SimpleNamespace(get=Mock(return_value={**task, "output": "evidence"}), snapshot=Mock(return_value=[task]), cancel=AsyncMock())
    mode = SimpleNamespace(_session=SimpleNamespace(subagents=manager), _add_assistant_text=Mock(), _render_subagents=Mock(), _add_system_message=Mock())
    asyncio.run(InteractiveMode._cmd_subagents(mode, "show child-1"))
    mode._add_assistant_text.assert_called_with("evidence")
    asyncio.run(InteractiveMode._cmd_subagents(mode, "cancel child-1"))
    manager.cancel.assert_awaited_once_with("child-1")
