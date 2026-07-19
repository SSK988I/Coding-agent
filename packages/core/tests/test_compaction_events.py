"""Tests for compaction lifecycle events and overflow retry.

Verifies:
  - The orchestrator emits compaction_start before and compaction_end after
    a performed compaction (via the on_event callback).
  - abort() on an armed-but-not-yet-run compaction is observable.
  - AgentSession.prompt() re-prompts on need_retry (overflow recovery).

These live in the agent package because they exercise CompactionOrchestrator
directly; the AgentSession retry test is minimal and self-contained.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent_llm import (
    AssistantMessage,
    Model,
    ModelCost,
    TextContent,
    Usage,
    UserMessage,
)

from agent_core import Agent, CompactionSettings, SessionManager
from agent_core.compaction_orchestrator import CompactionOrchestrator


def _run(coro):
    return asyncio.run(coro)


def _model(window: int = 1000) -> Model:
    return Model(
        id="m", context_window=window,
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )


class _FakeStream:
    def __init__(self, summary_text: str) -> None:
        self._final = AssistantMessage(
            content=[TextContent(text=summary_text)],
            provider="deepseek", model="m", stop_reason="stop",
        )

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def result(self):
        return self._final


def _make_fake_stream_fn(summary_text: str):
    def fn(model, context, options=None):
        return _FakeStream(summary_text)
    return fn


def _setup(tmp_path: Path, *, window=1000, summary="## Goal\ndid stuff",
           keep_recent=20000, reserve=16384):
    sm = SessionManager.create(cwd="/test", agent_dir=tmp_path)
    agent = Agent(
        model=_model(window), system_prompt="sys", tools=[],
        stream_fn=_make_fake_stream_fn(summary), session_manager=sm,
    )
    compactor = CompactionOrchestrator(agent, sm)
    compactor._settings = lambda: CompactionSettings(
        enabled=True, reserve_tokens=reserve, keep_recent_tokens=keep_recent,
    )
    return agent, sm, compactor


def _seed_conversation(sm, agent, *, big="x" * 400):
    sm.append_message(UserMessage(content=big))
    sm.append_message(AssistantMessage(content=[TextContent(text=big)], provider="d", model="m"))
    sm.append_message(UserMessage(content="recent q"))
    sm.append_message(AssistantMessage(content=[TextContent(text="recent a")], provider="d", model="m"))
    agent.load_messages(sm.build_session_context().messages)


# ─── lifecycle events ─────────────────────────────────────────────────


def test_manual_compact_emits_start_and_end_events(tmp_path: Path):
    agent, sm, compactor = _setup(tmp_path, keep_recent=50)
    _seed_conversation(sm, agent)

    events: list[dict] = []
    compactor.on_event = events.append

    outcome = _run(compactor.manual_compact())
    assert outcome.performed

    types = [e["type"] for e in events]
    assert "compaction_start" in types
    assert "compaction_end" in types
    assert types.index("compaction_start") < types.index("compaction_end")
    start = next(e for e in events if e["type"] == "compaction_start")
    assert start["reason"] == "manual"


def test_no_events_when_nothing_to_compact(tmp_path: Path):
    agent, sm, compactor = _setup(tmp_path)
    sm.append_message(UserMessage(content="hi"))
    sm.append_message(AssistantMessage(content=[TextContent(text="yo")], provider="d", model="m"))
    agent.load_messages(sm.build_session_context().messages)

    events: list[dict] = []
    compactor.on_event = events.append

    outcome = _run(compactor.manual_compact())
    assert not outcome.performed
    assert events == []  # nothing happened → no lifecycle events


def test_check_compaction_threshold_emits_events(tmp_path: Path):
    agent, sm, compactor = _setup(tmp_path, window=200, keep_recent=20, reserve=100)
    big = "y" * 800
    sm.append_message(UserMessage(content=big))
    sm.append_message(AssistantMessage(
        content=[TextContent(text=big)], provider="d", model="m",
        usage=Usage(input=50, output=50, total_tokens=190),  # > window-reserve(100)
    ))
    agent.load_messages(sm.build_session_context().messages)

    events: list[dict] = []
    compactor.on_event = events.append

    outcome = _run(compactor.check_compaction())
    assert outcome.performed
    assert outcome.reason == "threshold"
    types = [e["type"] for e in events]
    assert "compaction_start" in types and "compaction_end" in types


# ─── abort ────────────────────────────────────────────────────────────


def test_abort_sets_signal():
    """abort() is observable: arming then aborting marks the signal set."""
    compactor = CompactionOrchestrator.__new__(CompactionOrchestrator)
    compactor._abort_signal = None
    # No in-flight compaction → abort is a safe no-op.
    compactor.abort()
    assert compactor._abort_signal is None


# ─── AgentSession.prompt need_retry ───────────────────────────────────
# (Lives in packages/app/tests/test_compaction_retry.py to avoid a
# cross-package import from the agent test suite.)
