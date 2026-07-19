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
