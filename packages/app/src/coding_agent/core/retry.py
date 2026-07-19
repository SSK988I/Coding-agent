"""Application-level retry policy for transient provider failures."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, cast

from agent_llm.event_stream import AssistantMessageEventStream
from agent_llm.types import AssistantMessageEvent


@dataclass(frozen=True)
class RetryPolicy:
    enabled: bool = True
    max_retries: int = 2
    initial_delay: float = 1.0
    max_delay: float = 8.0

    def delay_for(self, retry_number: int) -> float:
        return min(self.initial_delay * (2 ** max(0, retry_number - 1)), self.max_delay)


_TRANSIENT_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "rate limit",
    "too many requests",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "service unavailable",
    "server disconnected",
    "connection reset",
    "connection error",
    "network error",
    "overloaded",
)


def is_transient_error(message: str | None) -> bool:
    text = (message or "").lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def retrying_stream(
    factory: Callable[[], AssistantMessageEventStream],
    policy: RetryPolicy,
    *,
    on_retry: Callable[[int, float, str], Any] | None = None,
    abort_event: asyncio.Event | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AssistantMessageEventStream:
    """Retry a stream only when a transient failure occurs before content.

    Retrying after deltas have reached the caller would duplicate visible output,
    so such failures are forwarded immediately.  The provider's initial ``start``
    event is buffered until real content arrives or the attempt succeeds.
    """
    outer = AssistantMessageEventStream()

    async def _drive() -> None:
        for attempt in range(policy.max_retries + 1):
            inner = factory()
            buffered: list[AssistantMessageEvent] = []
            emitted_content = False
            terminal: AssistantMessageEvent | None = None

            async for event in inner:
                event_type = event.get("type")
                if event_type in {"done", "error"}:
                    terminal = event
                    continue
                if not emitted_content:
                    buffered.append(event)
                    if event_type == "start":
                        continue
                    for queued in buffered:
                        outer.push(queued)
                    buffered.clear()
                    emitted_content = True
                else:
                    outer.push(event)

            final = await inner.result()
            if terminal is None:
                terminal = cast(AssistantMessageEvent, (
                    {"type": "error", "reason": "error", "error": final}
                    if getattr(final, "stop_reason", None) == "error"
                    else {"type": "done", "reason": getattr(final, "stop_reason", "stop"), "message": final}
                ))

            error = terminal.get("error") if terminal.get("type") == "error" else None
            error_text = getattr(error, "error_message", "") if error is not None else ""
            can_retry = (
                policy.enabled
                and not emitted_content
                and attempt < policy.max_retries
                and is_transient_error(error_text)
                and not (abort_event and abort_event.is_set())
            )
            if can_retry:
                retry_number = attempt + 1
                delay = policy.delay_for(retry_number)
                if on_retry is not None:
                    result = on_retry(retry_number, delay, error_text)
                    if hasattr(result, "__await__"):
                        await result
                if abort_event is not None:
                    try:
                        await asyncio.wait_for(abort_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
                    if abort_event.is_set():
                        for queued in buffered:
                            outer.push(queued)
                        outer.push(terminal)
                        outer.end(error or final)
                        return
                else:
                    await sleep(delay)
                continue

            for queued in buffered:
                outer.push(queued)
            outer.push(terminal)
            outer.end(error or final)
            return

    asyncio.ensure_future(_drive())
    return outer


__all__ = ["RetryPolicy", "is_transient_error", "retrying_stream"]
