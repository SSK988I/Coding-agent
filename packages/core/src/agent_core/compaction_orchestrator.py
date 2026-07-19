"""Compaction orchestration layer.

Wires together the SessionManager, the pure compaction functions, the LLM
summarization call, and the agent state. It exposes three entry points:

  - manual_compact():      triggered by /compact
  - check_compaction():    called after each turn; handles threshold + overflow
  - (overflow recovery):   check_compaction's overflow branch

The summary call reuses the agent's configured stream, credentials, and
reasoning options.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Literal

from agent_core.session.compaction import (
    estimate_context_tokens,
    prepare_compaction,
    should_compact,
)
from agent_core.session.summarize import compact as run_compact
from agent_core.session.types import CompactionResult, CompactionSettings

__all__ = [
    "CompactionOrchestrator",
    "CompactionOutcome",
    "CompactionReason",
]

CompactionReason = Literal["manual", "threshold", "overflow"]

#: Callback shape: (event: {"type": "compaction_start"|"compaction_end", ...}) -> None.
#: The orchestrator calls it around each compaction so the caller (AgentSession)
#: can forward lifecycle events to its listeners without coupling to internals.
CompactionEventCallback = Callable[[dict], None]


@dataclass
class CompactionOutcome:
    """Result of a compaction attempt."""
    performed: bool
    reason: CompactionReason | None = None
    need_retry: bool = False  # True when overflow recovery should re-prompt
    error: str | None = None
    summary_preview: str | None = None


class CompactionOrchestrator:
    """Orchestrates compaction for an Agent + SessionManager pair.

    Construct with the agent and session; call manual_compact() on /compact,
    and check_compaction() after each turn ends. The agent's stream_fn /
    get_api_key / reasoning are read at call time so model switches take effect.
    """

    def __init__(self, agent: Any, session_manager: Any) -> None:
        self.agent = agent
        self.session_manager = session_manager
        self._overflow_attempted = False
        #: Optional lifecycle-event callback (compaction_start / compaction_end).
        #: Set by AgentSession to forward events to its listeners.
        self.on_event: CompactionEventCallback | None = None
        #: Abort signal for the in-flight compaction summary LLM call.
        self._abort_signal: asyncio.Event | None = None

    def abort(self) -> None:
        """Request abort of the in-flight compaction (best-effort).

        Sets the abort signal so the next checkpoint in ``_perform_compaction``
        stops. The caller should still await the outstanding compaction call.
        """
        if self._abort_signal is not None:
            self._abort_signal.set()

    def reset_overflow_guard(self) -> None:
        """Call at the start of each user turn."""
        self._overflow_attempted = False

    # ─── helpers ───────────────────────────────────────────────────────

    def _settings(self) -> CompactionSettings:
        # Future: read from a settings manager. For now, defaults.
        return CompactionSettings()

    def _context_window(self) -> int:
        return int(getattr(self.agent.state.model, "context_window", 0) or 0)

    def _model(self) -> Any:
        return self.agent.state.model

    def _stream_fn(self) -> Any:
        return self.agent.stream_fn

    def _get_api_key(self) -> Any:
        return getattr(self.agent, "get_api_key", None)

    def _reasoning(self) -> Any:
        return getattr(self.agent, "reasoning", None)

    # ─── core compaction (shared by manual + auto) ────────────────────

    async def _perform_compaction(
        self,
        *,
        reason: CompactionReason,
        custom_instructions: str | None = None,
    ) -> CompactionOutcome:
        """Prepare + run + persist compaction, then rebuild agent state.

        Emits ``compaction_start`` before and ``compaction_end`` after (via the
        optional ``on_event`` callback), so the caller can surface the lifecycle
        to UI listeners. Honors an abort signal set by :meth:`abort`.
        """
        branch = self.session_manager.get_branch()
        settings = self._settings()
        preparation = prepare_compaction(branch, settings)
        if preparation is None:
            # Nothing to compact (e.g. last entry is already a compaction).
            if branch and _last_is_compaction(branch):
                return CompactionOutcome(performed=False, reason=reason, error="Already compacted")
            return CompactionOutcome(performed=False, reason=reason, error="Nothing to compact")

        # Emit compaction_start and arm the abort signal for this run.
        self._abort_signal = asyncio.Event()
        self._emit({"type": "compaction_start", "reason": reason})
        try:
            result: CompactionResult = await run_compact(
                preparation,
                model=self._model(),
                stream_fn=self._stream_fn(),
                get_api_key=self._get_api_key(),
                reasoning=self._reasoning(),
                custom_instructions=custom_instructions,
            )
        except Exception as e:
            # Pair the start event with an end so listeners aren't left waiting.
            self._abort_signal = None
            self._emit({"type": "compaction_end", "reason": reason, "aborted": True, "error": str(e)})
            return CompactionOutcome(performed=False, reason=reason, error=str(e))
        finally:
            signal, self._abort_signal = self._abort_signal, None

        # Best-effort abort: if abort() was called during the summary call, drop
        # the result. (run_compact does not accept a signal; full cooperative
        # cancellation is observed before and after the LLM call.)
        if signal is not None and signal.is_set():
            self._emit({"type": "compaction_end", "reason": reason, "aborted": True})
            return CompactionOutcome(performed=False, reason=reason, error="Compaction aborted")

        # Persist the compaction entry.
        self.session_manager.append_compaction(result)

        # Rebuild the agent transcript from the session (applies the compaction).
        ctx = self.session_manager.build_session_context()
        self.agent.load_messages(ctx.messages)

        outcome = CompactionOutcome(
            performed=True, reason=reason,
            summary_preview=(result.summary[:80] + "…") if len(result.summary) > 80 else result.summary,
        )
        self._emit({
            "type": "compaction_end",
            "reason": reason,
            "aborted": False,
            "summary_preview": outcome.summary_preview,
        })
        return outcome

    def _emit(self, event: dict) -> None:
        """Forward a lifecycle event to the optional callback."""
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                pass

    # ─── manual /compact ──────────────────

    async def manual_compact(self, custom_instructions: str | None = None) -> CompactionOutcome:
        return await self._perform_compaction(
            reason="manual", custom_instructions=custom_instructions,
        )

    # ─── automatic threshold + overflow ───

    async def check_compaction(
        self,
        *,
        skip_aborted_check: bool = False,
    ) -> CompactionOutcome:
        """Check whether to compact after a turn; act if needed.

        Two triggers:
          1. overflow: the last assistant message errored due to context size
          2. threshold: estimated/real context tokens exceed the window's reserve

        Returns the outcome (performed=False if nothing was done). On overflow
        with willRetry, outcome.need_retry=True so the caller re-prompts.
        """
        settings = self._settings()
        if not settings.enabled:
            return CompactionOutcome(performed=False)

        context_window = self._context_window()
        if context_window <= 0:
            return CompactionOutcome(performed=False)

        messages = self.agent.state.messages
        if not messages:
            return CompactionOutcome(performed=False)

        last = messages[-1]
        last_role = getattr(last, "role", None)
        if last_role != "assistant":
            # Only check after an assistant turn.
            return CompactionOutcome(performed=False)

        stop_reason = getattr(last, "stop_reason", "stop")

        # Skip aborted responses when asked (e.g. user-initiated abort).
        if skip_aborted_check and stop_reason == "aborted":
            return CompactionOutcome(performed=False)

        # Don't re-compact if we already compacted after this message.
        latest_compaction = self.session_manager.get_latest_compaction_entry()
        if latest_compaction is not None:
            # The compaction entry was appended after the last assistant message;
            # if the agent state's last message predates it, skip.
            last_ts = getattr(last, "timestamp", 0) or 0
            compaction_ts = _entry_timestamp(latest_compaction)
            if compaction_ts and last_ts and last_ts <= compaction_ts:
                return CompactionOutcome(performed=False)

        # ── Case 1: overflow ──
        if _is_context_overflow(last, context_window):
            will_retry = stop_reason != "stop"
            if not will_retry:
                # Completed answer that overflowed: compact but don't retry.
                return await self._run_auto_compaction(reason="overflow", will_retry=False)
            if self._overflow_attempted:
                # Only one recovery attempt.
                return CompactionOutcome(
                    performed=False, reason="overflow",
                    error="Context overflow recovery failed after one compact-and-retry attempt.",
                )
            self._overflow_attempted = True
            # Strip the errored assistant message from agent state (kept in file).
            self.agent.load_messages(messages[:-1])
            return await self._run_auto_compaction(reason="overflow", will_retry=True)

        # ── Case 2: threshold ──
        # Prefer real usage; fall back to estimation.
        usage = getattr(last, "usage", None)
        direct_tokens = _calculate_context_tokens_from_usage(usage)
        if stop_reason == "error" or direct_tokens == 0:
            est = estimate_context_tokens(messages)
            if est.last_usage_index is None:
                return CompactionOutcome(performed=False)
            context_tokens = est.tokens
        else:
            context_tokens = direct_tokens

        if should_compact(context_tokens, context_window, settings):
            return await self._run_auto_compaction(reason="threshold", will_retry=False)

        return CompactionOutcome(performed=False)

    async def _run_auto_compaction(
        self, *, reason: CompactionReason, will_retry: bool
    ) -> CompactionOutcome:
        """Run compaction for an automatic trigger."""
        outcome = await self._perform_compaction(reason=reason)
        if outcome.performed and will_retry:
            outcome.need_retry = True
        return outcome


# ─── module-level helpers (free functions, no state) ───────────────────

def _last_is_compaction(branch: list) -> bool:
    from agent_core.session.types import CompactionEntry
    return bool(branch) and isinstance(branch[-1], CompactionEntry)


def _entry_timestamp(entry: Any) -> float:
    """Parse an entry's ISO timestamp to epoch seconds; 0 on failure."""
    ts = getattr(entry, "timestamp", "") or ""
    if not ts:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _is_context_overflow(message: Any, context_window: int) -> bool:
    """True when the message indicates a context-window overflow.

    Heuristic matching the isContextOverflow: stop_reason == "length" or an
    error message mentioning context/overflow. (A faithful subset.)
    """
    stop = getattr(message, "stop_reason", "stop")
    if stop == "length":
        return True
    if stop == "error":
        err = (getattr(message, "error_message", "") or "").lower()
        if any(k in err for k in ("context", "overflow", "too long", "too many tokens", "maximum context")):
            return True
    return False


def _calculate_context_tokens_from_usage(usage: Any) -> int:
    if usage is None:
        return 0
    total = getattr(usage, "total_tokens", 0) or 0
    if total:
        return int(total)
    inp = int(getattr(usage, "input", 0) or 0)
    out = int(getattr(usage, "output", 0) or 0)
    cr = int(getattr(usage, "cache_read", 0) or 0)
    cw = int(getattr(usage, "cache_write", 0) or 0)
    return inp + out + cr + cw
