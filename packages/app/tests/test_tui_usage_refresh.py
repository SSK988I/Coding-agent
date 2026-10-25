"""Footer snapshots follow committed session events, not streaming frames."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agent_core import SessionManager
from agent_core.tools.bash import BashRawResult
from agent_llm import AssistantMessage, Model, ModelCost, TextContent, ToolCall, ToolResultMessage, Usage, UserMessage
from agent_tui.theme import load_theme

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.modes.interactive.components.footer import FooterComponent
from coding_agent.modes.interactive.interactive_mode import InteractiveMode


@pytest.fixture
def usage_controller(tmp_path, monkeypatch):
    manager = SessionManager.create(cwd=str(tmp_path), in_memory=True)
    session = AgentSession(AgentSessionConfig(
        cwd=str(tmp_path), model=Model(id="usage-test", provider="test", context_window=10_000, cost=ModelCost(0, 0, 0, 0)),
        tools=[], no_tools=True, no_builtin_tools=True, session_manager=manager,
    ))
    snapshots = []
    original_stats = session.get_stats

    def tracked_stats():
        stats = original_stats()
        snapshots.append(stats)
        return stats

    monkeypatch.setattr(session, "get_stats", tracked_stats)
    monkeypatch.setattr(FooterComponent, "refresh_git_branch", lambda self: None)
    footer = FooterComponent(session, load_theme("dark"))
    controller = SimpleNamespace(
        _session=session, _footer=footer, theme=load_theme("dark"),
        tui=SimpleNamespace(request_render=Mock()), _pending_tools={},
        _streaming_component=None, chat_container=SimpleNamespace(remove_child=Mock()),
        _clear_status_indicator=Mock(), _add_system_message=Mock(),
        _render_subagents=Mock(), _on_agent_event=Mock(),
        _effective_api_key=lambda: "test-key-not-sent",
    )
    for name in ("_on_session_event", "_refresh_footer", "_respond"):
        setattr(controller, name, getattr(InteractiveMode, name).__get__(controller, SimpleNamespace))
    unsubscribe = session.on_event(controller._on_session_event)
    try:
        yield session, controller, snapshots
    finally:
        unsubscribe()
        session.dispose()


def _render(controller, snapshots):
    assert controller._footer.render(240)
    return snapshots[-1]


def _assistant():
    return AssistantMessage(
        model="usage-test", provider="test",
        content=[TextContent(text="answer"), ToolCall(id="read-1", name="read", arguments={"path": "a.py"})],
        usage=Usage(input=100, cache_read=900, output=100, cache_read_reported=True),
    )


def test_committed_messages_and_turn_results_refresh_but_streaming_does_not(usage_controller):
    session, controller, snapshots = usage_controller

    async def exercise():
        initial = _render(controller, snapshots)
        await session.agent._process_events({"type": "message_end", "message": UserMessage(content="x" * 400)})
        user_stats = _render(controller, snapshots)
        assert user_stats.user_messages == 1
        assert user_stats.metrics.context_tokens > initial.metrics.context_tokens

        assistant = _assistant()
        await session.agent._process_events({"type": "message_end", "message": assistant})
        stats = _render(controller, snapshots)
        assert stats.assistant_messages == 1 and stats.tool_calls == 1
        assert stats.metrics.cache_hit_rate == 90
        # Directly restored/committed messages have no verified request binding.
        assert stats.metrics.context_estimated is True
        committed_calls = len(snapshots)

        partial = AssistantMessage(content=[TextContent(text="new reply")], usage=Usage(input=99_999))
        for _ in range(5):
            await session.agent._process_events({"type": "message_update", "message": partial})
            assert _render(controller, snapshots) is stats
        assert partial not in session.state.messages
        assert len(snapshots) == committed_calls

        result = ToolResultMessage(tool_call_id="read-1", tool_name="read", content=[TextContent(text="x" * 400)])
        await session.agent._process_events({
            "type": "tool_execution_end", "tool_call_id": "read-1", "result": result,
        })
        assert _render(controller, snapshots) is stats
        assert stats.tool_results == 0

        await session.agent._process_events({"type": "turn_end", "message": assistant, "tool_results": [result]})
        updated = _render(controller, snapshots)
        assert updated.tool_results == 1
        assert updated.metrics.context_tokens == stats.metrics.context_tokens + 100
        assert updated.metrics.context_estimated is True
        assert len(snapshots) == committed_calls + 1

    asyncio.run(exercise())


def test_explicit_refresh_tracks_branch_model_and_new_session(usage_controller):
    session, controller, snapshots = usage_controller

    async def seed():
        await session.agent._process_events({"type": "message_end", "message": UserMessage(content="question")})
        await session.agent._process_events({"type": "message_end", "message": _assistant()})

    asyncio.run(seed())
    old = _render(controller, snapshots)
    manager = session.session_manager
    manager.set_leaf_id(manager.get_branch()[0].id)
    session.agent.load_messages(manager.build_session_context().messages)
    assert _render(controller, snapshots) is old
    controller._refresh_footer()
    branch = _render(controller, snapshots)
    assert branch.assistant_messages == 0 and branch.user_messages == 1
    assert branch.metrics.cache_hit_rate is None

    session.set_model(Model(id="next-model", provider="test", context_window=20_000, cost=ModelCost(0, 0, 0, 0)))
    controller._refresh_footer()
    assert _render(controller, snapshots).metrics.context_window == 20_000

    session.new_session()
    controller._refresh_footer()
    new = _render(controller, snapshots)
    assert new.session_id != old.session_id
    assert new.total_messages == 0 and new.tool_results == 0


def test_clear_invalidates_context_without_rewriting_persisted_usage(usage_controller):
    session, controller, snapshots = usage_controller
    asyncio.run(session.agent._process_events({"type": "message_end", "message": _assistant()}))
    before = _render(controller, snapshots)
    controller.chat_container.clear = Mock()
    controller._tool_cards = {}
    controller._print_welcome = Mock()
    controller._render_plan_state_controls = Mock()

    InteractiveMode._cmd_clear(controller)

    after = _render(controller, snapshots)
    assert session.state.messages == []
    assert after.metrics.context_tokens < before.metrics.context_tokens
    assert after.metrics.input_tokens == before.metrics.input_tokens
    assert len(snapshots) == 2


@pytest.mark.parametrize("exclude_from_context", [False, True])
def test_bash_passthrough_refreshes_without_respond_subscription(usage_controller, exclude_from_context):
    session, controller, snapshots = usage_controller
    before = _render(controller, snapshots)
    session._bash_tool = SimpleNamespace(run_raw=AsyncMock(return_value=BashRawResult(output="x" * 1200)))

    asyncio.run(session.run_bash("fake command", exclude_from_context=exclude_from_context))

    after = _render(controller, snapshots)
    assert len(snapshots) == 2
    if exclude_from_context:
        assert after.metrics.context_tokens == before.metrics.context_tokens
    else:
        assert after.metrics.context_tokens > before.metrics.context_tokens
    controller._on_agent_event.assert_not_called()


@pytest.mark.parametrize("event_type", [
    "agent_start", "agent_end", "message_end", "turn_end",
    "compaction_start", "compaction_end", "auto_compaction_start", "auto_compaction_end",
    "bash_execution", "collaboration_mode_changed", "plan_state_changed", "plan.stateChanged",
])
def test_long_lived_session_events_invalidate_the_snapshot(usage_controller, event_type):
    session, controller, snapshots = usage_controller
    before = _render(controller, snapshots)
    session.state.system_prompt += "temporary context" * 100

    session._emit_event({"type": event_type})

    after = _render(controller, snapshots)
    assert len(snapshots) == 2
    assert after.metrics.context_tokens > before.metrics.context_tokens
    controller.tui.request_render.assert_called()


@pytest.mark.parametrize("fail", [False, True])
def test_respond_finally_refreshes_after_agent_end_prompt_cleanup(usage_controller, monkeypatch, fail):
    session, controller, snapshots = usage_controller
    before = _render(controller, snapshots)
    original_prompt = session.state.system_prompt
    in_task = []

    async def prompt(_message):
        session.state.system_prompt = original_prompt + "temporary memory " * 100
        try:
            session._emit_event({"type": "agent_start"})
            session._emit_event({"type": "agent_end"})
            in_task.append(_render(controller, snapshots).metrics.context_tokens)
            if fail:
                raise RuntimeError("fake task failed")
        finally:
            session.state.system_prompt = original_prompt

    monkeypatch.setattr(session, "prompt", prompt)

    asyncio.run(controller._respond("fake prompt"))

    assert in_task[0] > before.metrics.context_tokens
    assert _render(controller, snapshots).metrics.context_tokens == before.metrics.context_tokens
    assert len(snapshots) == 3
    assert controller._add_system_message.called is fail


def test_long_lived_session_handler_still_routes_subagent_cards(usage_controller):
    session, controller, snapshots = usage_controller
    _render(controller, snapshots)
    tasks = [{"taskId": "child-1", "state": "running"}]

    session._emit_event({"type": "subagent.stateChanged", "tasks": tasks})

    controller._render_subagents.assert_called_once_with(tasks)
    _render(controller, snapshots)
    assert len(snapshots) == 1
