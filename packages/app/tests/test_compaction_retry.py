"""Tests for AgentSession.prompt overflow-retry behavior.

Verifies that when the compaction orchestrator reports ``need_retry=True``
(overflow recovery), ``AgentSession.prompt`` re-prompts the original message
once — mirroring the single-attempt overflow recovery.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_llm import Model, ModelCost

from agent_core import SessionManager
from agent_core.compaction_orchestrator import CompactionOutcome

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig


def _run(coro):
    return asyncio.run(coro)


def _model() -> Model:
    return Model(
        id="m", context_window=1000,
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )


def _make_session_with_stub_orchestrator():
    """Build an AgentSession, then swap in a stub orchestrator + stub agent.prompt."""
    sm = SessionManager.create(cwd="/test", in_memory=True)
    config = AgentSessionConfig(model=_model(), cwd="/test", session_manager=sm)
    session = AgentSession(config)

    fake_orch = MagicMock()
    fake_orch.reset_overflow_guard = MagicMock()
    fake_orch.abort = MagicMock()
    session._compaction_orchestrator = fake_orch
    session._agent.prompt = AsyncMock()  # type: ignore[assignment]
    return session, fake_orch


def test_prompt_re_prompts_on_need_retry():
    session, fake_orch = _make_session_with_stub_orchestrator()

    outcomes = iter([
        CompactionOutcome(performed=True, reason="overflow", need_retry=True, summary_preview="s"),
        CompactionOutcome(performed=False),
    ])
    fake_orch.check_compaction = AsyncMock(side_effect=lambda **kw: next(outcomes))

    _run(session.prompt("hello"))

    # Original prompt + one retry = two calls.
    assert session._agent.prompt.await_count == 2
    for call in session._agent.prompt.await_args_list:
        assert call.args[0] == "hello"


def test_prompt_does_not_retry_when_need_retry_false():
    session, fake_orch = _make_session_with_stub_orchestrator()
    fake_orch.check_compaction = AsyncMock(
        return_value=CompactionOutcome(performed=False),
    )

    _run(session.prompt("hello"))

    # No retry: single prompt call.
    assert session._agent.prompt.await_count == 1


def test_prompt_retries_at_most_once():
    """Only one overflow-recovery attempt is allowed per turn."""
    session, fake_orch = _make_session_with_stub_orchestrator()
    # Stub returns need_retry every time; prompt() must still stop after one retry.
    fake_orch.check_compaction = AsyncMock(
        return_value=CompactionOutcome(performed=True, reason="overflow", need_retry=True),
    )

    _run(session.prompt("hello"))

    # Original + exactly one retry, even though the stub keeps asking.
    assert session._agent.prompt.await_count == 2


def test_abort_compaction_delegates_to_orchestrator():
    session, fake_orch = _make_session_with_stub_orchestrator()
    session.abort_compaction()
    fake_orch.abort.assert_called_once()


def test_processing_flag_cleared_after_retry():
    """is_processing must be False after prompt() returns, even with a retry."""
    session, fake_orch = _make_session_with_stub_orchestrator()
    fake_orch.check_compaction = AsyncMock(
        return_value=CompactionOutcome(performed=True, reason="overflow", need_retry=True),
    )
    _run(session.prompt("hello"))
    assert session.is_processing is False


def test_overflow_retry_marks_internal_retry_without_marking_same_text_steer(tmp_path):
    """Overflow auto-retry tags only the retried prompt echo, not user-queued same-text steer."""
    from agent_llm import AssistantMessage, TextContent
    from coding_agent.desktop.runtime import DesktopRuntime

    sm = SessionManager.create(cwd=str(tmp_path), agent_dir=tmp_path)
    session = AgentSession(AgentSessionConfig(model=_model(), cwd=str(tmp_path), session_manager=sm, tools=[]))

    fake_orch = MagicMock()
    fake_orch.reset_overflow_guard = MagicMock()
    fake_orch.abort = MagicMock()
    fake_orch.check_compaction = AsyncMock(
        return_value=CompactionOutcome(performed=True, reason="overflow", need_retry=True, summary_preview="s"),
    )
    session._compaction_orchestrator = fake_orch

    runtime_events: list[dict] = []
    runtime = DesktopRuntime(runtime_events.append)
    runtime._session = session
    session.on_event(runtime._on_session_event)

    calls = 0

    class _Stream:
        def __init__(self, reply: AssistantMessage):
            self._reply = reply

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def result(self):
            return self._reply

    def stream_fn(_model_arg, _context, _options):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _Stream(AssistantMessage(
                content=[TextContent(text="")],
                stop_reason="error",
                error_message="context overflow",
            ))
        if calls == 2:
            # User actively sends the exact same text during the retried run.
            session.agent.follow_up("hello")
            return _Stream(AssistantMessage(content=[TextContent(text="retried answer")], stop_reason="end_turn"))
        return _Stream(AssistantMessage(content=[TextContent(text="follow-up answer")], stop_reason="end_turn"))

    session.agent.stream_fn = stream_fn
    _run(session.prompt("hello"))

    user_message_ends = [
        ev["event"]["payload"]
        for ev in runtime_events
        if ev["event"]["type"] == "message_end"
        and ev["event"]["payload"].get("message", {}).get("role") == "user"
    ]
    assert len(user_message_ends) == 3
    # 1st echo: initial prompt; 2nd echo: overflow internal retry; 3rd echo: explicit user follow-up with same text.
    assert user_message_ends[0].get("internalRetry") is None
    assert user_message_ends[0]["message"]["source"] == "prompt"
    assert user_message_ends[1].get("internalRetry") is True
    assert user_message_ends[1]["message"]["source"] == "internal_retry"
    assert user_message_ends[2].get("internalRetry") is None
    assert user_message_ends[2]["message"]["source"] == "follow_up"

    # Reopening from disk preserves source tags so persisted timeline deduplication matches live events.
    restored = SessionManager.open(sm.path).build_session_context().messages
    restored_users = [(m.content, m.source) for m in restored if m.role == "user"]
    assert restored_users == [
        ("hello", "prompt"),
        ("hello", "internal_retry"),
        ("hello", "follow_up"),
    ]


def test_agent_steer_and_follow_up_backfill_source_on_user_message(tmp_path):
    from agent_llm import UserMessage

    sm = SessionManager.create(cwd=str(tmp_path), agent_dir=tmp_path)
    session = AgentSession(AgentSessionConfig(model=_model(), cwd=str(tmp_path), session_manager=sm, tools=[]))

    session.agent.steer(UserMessage(content="/model 看看"))
    session.agent.follow_up(UserMessage(content="/src/app.py"))

    steered = session.agent._steering_queue.drain()
    followed = session.agent._follow_up_queue.drain()
    assert len(steered) == 1 and steered[0].source == "steer"
    assert len(followed) == 1 and followed[0].source == "follow_up"
