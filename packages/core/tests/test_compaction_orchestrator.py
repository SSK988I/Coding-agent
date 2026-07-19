"""CompactionOrchestrator tests with a fake stream_fn (no API key).

Drives manual_compact() and check_compaction() end-to-end with a stubbed
stream_fn that returns a canned summary, verifying:
  - manual compaction persists a compaction entry + rebuilds the transcript
  - the transcript after compaction starts with a CompactionSummaryMessage
  - threshold-triggered auto-compaction fires when context_tokens exceed reserve
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
from agent_core.session.messages import CompactionSummaryMessage


def _run(coro):
    return asyncio.run(coro)


def _model(window: int = 1000) -> Model:
    return Model(
        id="m", context_window=window,
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )


class _FakeStream:
    """A minimal AssistantMessageEventStream stand-in that yields a canned summary.

    Implements async iteration + .result() returning a final AssistantMessage.
    """

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
    """A stream_fn that ignores input and returns a fake stream."""
    def fn(model, context, options=None):
        return _FakeStream(summary_text)
    return fn


def _setup(
    tmp_path: Path,
    *,
    window: int = 1000,
    summary: str = "## Goal\ndid stuff",
    keep_recent: int = 20000,
    reserve: int = 16384,
):
    sm = SessionManager.create(cwd="/test", agent_dir=tmp_path)
    agent = Agent(
        model=_model(window),
        system_prompt="sys", tools=[],
        stream_fn=_make_fake_stream_fn(summary),
        session_manager=sm,
    )
    compactor = CompactionOrchestrator(agent, sm)
    # Override settings for deterministic test behavior.
    compactor._settings = lambda: CompactionSettings(
        enabled=True, reserve_tokens=reserve, keep_recent_tokens=keep_recent,
    )
    return agent, sm, compactor


# ─── manual_compact ───────────────────────────────────────────────────

def test_manual_compact_persists_entry_and_rebuilds_transcript(tmp_path: Path):
    agent, sm, compactor = _setup(tmp_path, keep_recent=50)  # force summarization
    # Seed a conversation with enough content to summarize.
    big = "x" * 400  # ~100 tokens
    sm.append_message(UserMessage(content=big))
    sm.append_message(AssistantMessage(content=[TextContent(text=big)], provider="d", model="m"))
    sm.append_message(UserMessage(content="recent q"))
    sm.append_message(AssistantMessage(content=[TextContent(text="recent a")], provider="d", model="m"))
    # Mirror into agent transcript (as _process_events would).
    ctx = sm.build_session_context()
    agent.load_messages(ctx.messages)

    outcome = _run(compactor.manual_compact())
    assert outcome.performed
    assert outcome.reason == "manual"

    # Session now has a compaction entry appended.
    from agent_core.session.types import CompactionEntry
    assert isinstance(sm.entries[-1], CompactionEntry)
    # Transcript rebuilt: first message is a CompactionSummaryMessage.
    assert isinstance(agent.state.messages[0], CompactionSummaryMessage)
    # The summary carried the summarized history's content (the big block).
    assert "did stuff" in agent.state.messages[0].summary
    # The original first user message (400 x's) is no longer a standalone user turn.
    for m in agent.state.messages:
        if getattr(m, "role", None) == "user":
            assert m.content != big  # only "recent q" remains as a user message


def test_manual_compact_returns_none_when_nothing_to_compact(tmp_path: Path):
    agent, sm, compactor = _setup(tmp_path)
    sm.append_message(UserMessage(content="hi"))
    sm.append_message(AssistantMessage(content=[TextContent(text="yo")], provider="d", model="m"))
    agent.load_messages(sm.build_session_context().messages)

    # Tiny conversation: keep_recent huge → nothing to summarize.
    outcome = _run(compactor.manual_compact())
    assert outcome.performed is False


# ─── check_compaction: threshold ──────────────────────────────────────

def test_check_compaction_threshold_triggers_when_over_reserve(tmp_path: Path):
    # Tiny window + small keep_recent so compaction actually summarizes.
    agent, sm, compactor = _setup(tmp_path, window=200, keep_recent=20, reserve=100)
    big = "y" * 800
    sm.append_message(UserMessage(content=big))
    sm.append_message(AssistantMessage(
        content=[TextContent(text=big)], provider="d", model="m",
        usage=Usage(input=50, output=50, total_tokens=190),  # > window-reserve(100)
    ))
    agent.load_messages(sm.build_session_context().messages)

    outcome = _run(compactor.check_compaction())
    assert outcome.performed is True
    assert outcome.reason == "threshold"


def test_check_compaction_noop_when_disabled(tmp_path: Path):
    agent, sm, compactor = _setup(tmp_path, window=100)
    # Disable compaction via the orchestrator's settings.
    compactor._settings = lambda: CompactionSettings(enabled=False)
    sm.append_message(UserMessage(content="hi"))
    sm.append_message(AssistantMessage(content=[TextContent(text="yo")], provider="d", model="m"))
    agent.load_messages(sm.build_session_context().messages)
    outcome = _run(compactor.check_compaction())
    assert outcome.performed is False


def test_check_compaction_skips_when_last_not_assistant(tmp_path: Path):
    agent, sm, compactor = _setup(tmp_path, window=100)
    sm.append_message(UserMessage(content="hi"))
    agent.load_messages(sm.build_session_context().messages)
    outcome = _run(compactor.check_compaction())
    assert outcome.performed is False


# ─── overflow recovery ────────────────────────────────────────────────

def test_check_compaction_overflow_strips_and_compacts(tmp_path: Path):
    # Need a multi-turn conversation so there's history to summarize after strip.
    agent, sm, compactor = _setup(tmp_path, window=1000, keep_recent=20)
    big = "z" * 400
    # Earlier turn (will be summarized).
    sm.append_message(UserMessage(content=big))
    sm.append_message(AssistantMessage(content=[TextContent(text=big)], provider="d", model="m"))
    # Current turn: user + overflowed assistant.
    sm.append_message(UserMessage(content=big))
    err_msg = AssistantMessage(
        content=[TextContent(text="")], provider="d", model="m",
        stop_reason="error",
        error_message="context length exceeded maximum context window",
        usage=Usage(total_tokens=5000),
    )
    sm.append_message(err_msg)
    agent.load_messages(sm.build_session_context().messages)
    assert len(agent.state.messages) == 4

    outcome = _run(compactor.check_compaction())
    assert outcome.performed
    assert outcome.reason == "overflow"
    assert outcome.need_retry is True
    # After compaction, transcript starts with CompactionSummaryMessage.
    assert isinstance(agent.state.messages[0], CompactionSummaryMessage)
