"""Tests for the /thinking command and AgentSession.set_thinking_level."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_llm import Model, ModelCost

from agent_core import SessionManager

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.defaults import (
    DEFAULT_THINKING_LEVEL,
    THINKING_LEVEL_DESCRIPTIONS,
    VALID_THINKING_LEVELS,
)


def _run(coro):
    return asyncio.run(coro)


def _model() -> Model:
    return Model(
        id="m", context_window=1000,
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )


def _make_session():
    sm = SessionManager.create(cwd="/test", in_memory=True)
    config = AgentSessionConfig(model=_model(), cwd="/test", session_manager=sm)
    session = AgentSession(config)
    # Stub the agent's stream_fn to avoid real calls during construction.
    return session


# ─── defaults ─────────────────────────────────────────────────────────


def test_valid_levels_complete():
    assert VALID_THINKING_LEVELS == ("off", "minimal", "low", "medium", "high", "xhigh")


def test_default_is_medium():
    assert DEFAULT_THINKING_LEVEL == "medium"


def test_every_level_has_description():
    for level in VALID_THINKING_LEVELS:
        assert level in THINKING_LEVEL_DESCRIPTIONS
        assert THINKING_LEVEL_DESCRIPTIONS[level]


# ─── set_thinking_level ───────────────────────────────────────────────


def test_set_thinking_level_updates_agent_reasoning():
    session = _make_session()
    session.set_thinking_level("high")
    assert session.thinking_level == "high"
    assert session.agent.reasoning == "high"


def test_set_thinking_level_none():
    session = _make_session()
    session.set_thinking_level(None)
    assert session.thinking_level is None
    assert session.agent.reasoning is None


def test_set_thinking_level_emits_event():
    session = _make_session()
    events: list[dict] = []
    session.on_event(events.append)
    session.set_thinking_level("low")
    changed = [e for e in events if e.get("type") == "thinking_level_changed"]
    assert len(changed) == 1
    assert changed[0]["level"] == "low"


def test_set_thinking_level_persists_to_session():
    session = _make_session()
    session.set_thinking_level("xhigh")
    # SessionManager.append_thinking_level_change should have recorded it.
    # The in-memory session tracks entries; verify a thinking-level entry exists.
    from agent_core.session.types import ThinkingLevelChangeEntry
    tl_entries = [e for e in session.session_manager.entries
                  if isinstance(e, ThinkingLevelChangeEntry)]
    assert len(tl_entries) >= 1
    assert tl_entries[-1].thinking_level == "xhigh"
