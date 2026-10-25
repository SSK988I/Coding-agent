"""The two clients share one branch-aware usage projection."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from agent_core import SessionManager
from agent_core.session.messages import CompactionSummaryMessage
from agent_llm import (
    AssistantMessage, AssistantMessageEventStream, ImageContent, Model, ModelCost,
    TextContent, ToolResultMessage, Usage, UserMessage,
)

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.messages import BashExecutionMessage, BranchSummaryMessage, CustomMessage
from coding_agent.core.usage_metrics import project_usage_metrics
from coding_agent.desktop.runtime import DesktopRuntime


def model(window=100_000, name="test-model"):
    return Model(id=name, provider="test", context_window=window, cost=ModelCost(0, 0, 0, 0))


def assistant(inp=100, cached=900, output=100, *, reported=True, name="test-model", stop="stop"):
    return AssistantMessage(
        model=name, provider="test", content=[TextContent(text="answer")], stop_reason=stop,
        usage=Usage(input=inp, cache_read=cached, output=output, cache_read_reported=reported),
    )


def entry(message):
    return SimpleNamespace(type="message", message=message)


def project(messages, *, entries=None, current_model=None, prompt="", tools=None):
    return project_usage_metrics(
        messages=messages, entries=entries if entries is not None else [entry(m) for m in messages],
        model=current_model or model(), system_prompt=prompt, tools=tools or [],
    )


def test_cached_tokens_count_toward_context_and_weighted_cumulative_hit_rate():
    first = assistant(inp=100, cached=900, output=100)
    second = assistant(inp=1000, cached=0, output=20)
    stats = project([first, second])
    assert stats.cache_hit_rate == 45
    assert stats.input_tokens == 2000
    assert stats.context_tokens == 1020  # Last request, not all requests added together.
    assert stats.context_percent == pytest.approx(1.02)
    assert stats.context_estimated is False
    assert project([first]).context_tokens == 1100


@pytest.mark.parametrize("reported,cached,expected", [(True, 0, 0), (False, 0, None), (None, 0, None), (None, 900, 90)])
def test_measured_zero_cache_is_distinct_from_missing_or_legacy_unknown(reported, cached, expected):
    assert project([assistant(inp=100 if cached else 1000, cached=cached, reported=reported)]).cache_hit_rate == expected


def test_no_input_or_partially_unreported_cache_does_not_invent_a_hit_rate():
    assert project([]).cache_hit_rate is None
    assert project([assistant(), assistant(inp=100, cached=0, reported=False)]).cache_hit_rate is None


def test_context_estimates_trailing_messages_and_tool_result_images():
    first = assistant()
    tool_result = ToolResultMessage(content=[TextContent(text="x" * 400), ImageContent(data="", mime_type="image/png")])
    stats = project([first, tool_result])
    assert stats.context_tokens == 1100 + 100 + 1200
    assert stats.context_estimated is True


@pytest.mark.parametrize("boundary", ["compaction", "model_change", "collaboration_mode_change"])
def test_old_usage_after_context_boundary_is_not_reused(boundary):
    old = assistant(inp=90_000, cached=0)
    summary = CompactionSummaryMessage(summary="short")
    stats = project([summary, old], entries=[entry(old), SimpleNamespace(type=boundary)], prompt="system prompt")
    assert stats.context_estimated is True
    assert stats.context_tokens < 100
    new = assistant(inp=1000, cached=0)
    stats = project([summary, old, new], entries=[entry(old), SimpleNamespace(type=boundary), entry(new)])
    assert stats.context_tokens == 1100
    assert stats.context_estimated is False


def test_model_switch_uses_new_window_and_estimates_old_models_history():
    stats = project([assistant()], current_model=model(2000, "different"), prompt="header")
    assert stats.context_window == 2000
    assert stats.context_estimated is True
    assert stats.context_tokens < 100


def test_empty_context_includes_system_prompt_and_tool_schemas():
    tool = SimpleNamespace(name="read", description="Read a file", parameters={"type": "object"})
    assert project([], prompt="x" * 400, tools=[tool]).context_tokens > 100
    assert project([]).context_tokens == 0
    assert project([], current_model=model(0)).context_percent is None
    overflow = project([assistant(inp=1500, cached=0)], current_model=model(1000))
    assert overflow.context_percent == 160


@pytest.mark.parametrize("stop", ["error", "aborted"])
def test_failed_messages_do_not_replace_last_usable_context_anchor(stop):
    stats = project([assistant(), assistant(inp=99_999, cached=0, stop=stop)])
    assert stats.context_tokens == 1101
    assert stats.context_estimated is True


@pytest.mark.parametrize("message", [
    BashExecutionMessage(command="logs", output="x" * 40_000),
    CustomMessage(content="x" * 40_000),
    BranchSummaryMessage(summary="x" * 40_000),
])
def test_custom_context_counts_actual_projected_text(message):
    assert project([assistant(), message]).context_tokens >= 11_100


def test_excluded_shell_output_and_control_commands_do_not_consume_context():
    baseline = project([assistant()]).context_tokens
    assert project([assistant(), BashExecutionMessage(output="x" * 40_000, exclude_from_context=True)]).context_tokens == baseline
    assert project([UserMessage(content="/session")]).context_tokens == 0


def test_prompt_changes_invalidate_old_measurements_until_next_request(tmp_path, monkeypatch):
    responses = iter([assistant(), assistant(inp=500, cached=0)])

    def stream(*_args):
        result = AssistantMessageEventStream()
        result.push({"type": "done", "reason": "stop", "message": next(responses)})
        return result

    monkeypatch.setattr("agent_llm.compat.stream_simple", stream)
    session = AgentSession(AgentSessionConfig(cwd=str(tmp_path), model=model(), tools=[], system_prompt="old prompt"))

    async def exercise():
        await session.prompt("first request")
        assert session.get_stats().metrics.context_estimated is False
        session._config.system_prompt = "different prompt" * 100
        session._refresh_collaboration_runtime()
        assert session.get_stats().metrics.context_estimated is True
        await session.prompt("new request")
        assert session.get_stats().metrics.context_estimated is False
        assert session.get_stats().metrics.context_tokens == 600

    try:
        asyncio.run(exercise())
    finally:
        session.dispose()


def test_session_stats_restore_and_branch_switch_follow_active_history(tmp_path):
    manager = SessionManager.create(cwd=str(tmp_path), sessions_dir=tmp_path / "sessions")
    root = manager.append_message(UserMessage(content="question"))
    manager.append_message(assistant())
    manager.flush()
    restored = SessionManager.open(manager.path)
    session = AgentSession(AgentSessionConfig(cwd=str(tmp_path), model=model(), tools=[], session_manager=restored))
    try:
        assert session.get_stats().metrics.cache_hit_rate == 90
        assert session.get_stats().metrics.context_estimated is True
        restored.leaf_id = root.id
        session.agent.load_messages(restored.build_session_context().messages)
        assert session.get_stats().metrics.cache_hit_rate is None
        assert session.get_stats().metrics.context_estimated is True
        session.agent.reset()
        assert session.get_stats().metrics.context_estimated is True
    finally:
        session.dispose()


def test_session_stats_does_not_import_message_type_per_history_entry(tmp_path):
    session = AgentSession(AgentSessionConfig(cwd=str(tmp_path), model=model(), tools=[]))
    messages = []
    for _ in range(8):
        messages.extend([UserMessage(content="question"), assistant()])
    for message in messages:
        session.session_manager.append_message(message)
    session.agent.load_messages(messages)
    original_import = __import__
    message_type_imports = []

    def tracked_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "agent_core.session.types" and "SessionMessageEntry" in fromlist:
            message_type_imports.append(name)
        return original_import(name, globals, locals, fromlist, level)

    try:
        with patch("builtins.__import__", side_effect=tracked_import):
            stats = session.get_stats()
        assert len(message_type_imports) <= 1
        assert stats.user_messages == stats.assistant_messages == 8
        assert stats.total_messages == 16
        assert stats.metrics.input_tokens == 8_000
        assert stats.metrics.cache_hit_rate == 90
    finally:
        session.dispose()


def test_desktop_snapshots_and_stats_events_use_core_projection(tmp_path):
    async def scenario():
        events = []
        runtime = DesktopRuntime(events.append)
        manager = SessionManager.create(cwd=str(tmp_path), sessions_dir=tmp_path / "sessions")
        manager.append_message(assistant())
        session = AgentSession(AgentSessionConfig(cwd=str(tmp_path), model=model(), tools=[], session_manager=manager))
        runtime._session = session
        runtime._workspace = tmp_path
        try:
            snapshot = await runtime._session_snapshot({})
            workspace = runtime._workspace_payload()
            assert snapshot["stats"]["metrics"] == workspace["stats"]["metrics"]
            runtime._on_session_event({"type": "message_end", "message": assistant()})
            event = events[-1]
            assert event["event"]["type"] == "session.statsChanged"
            assert event["sessionId"] == manager.header.id
            assert event["event"]["payload"]["stats"]["metrics"]["cache_hit_rate"] == 90
            runtime._on_session_event({"type": "message_update", "event": {"type": "text_delta", "delta": "x"}})
            assert events[-1]["event"]["type"] == "message_update"  # No full stats on every token.
        finally:
            await session.aclose()
    asyncio.run(scenario())
