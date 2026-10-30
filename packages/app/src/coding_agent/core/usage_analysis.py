"""Small, read-only request usage projections for the session analysis drawer."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable, TypedDict

from agent_core.session.types import SessionEntry, SessionMessageEntry


class UsageRequest(TypedDict):
    id: str
    index: int
    timestamp: float | None
    provider: str
    model: str
    stopReason: str
    inputTokens: int | None
    uncachedInputTokens: int | None
    outputTokens: int | None
    cacheReadTokens: int | None
    cacheWriteTokens: int | None
    cacheHitRate: float | None
    cost: float | None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        numeric = float(value)
    except (ValueError, OverflowError):
        return None
    return numeric if math.isfinite(numeric) and numeric >= 0 else None


def _tokens(value: Any) -> int | None:
    numeric = _number(value)
    # JSON consumers use JavaScript numbers. Reject corrupt values rather than
    # silently truncating fractional tokens or losing integer precision.
    if numeric is None or numeric > 2**53 - 1 or not numeric.is_integer():
        return None
    return int(numeric)


def _timestamp(message: Any, entry: SessionEntry) -> float | None:
    recorded = _number(getattr(message, "timestamp", None))
    if recorded is not None:
        return recorded
    try:
        parsed = datetime.fromisoformat(entry.timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return _number(parsed.timestamp() * 1000)
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def project_usage_requests(entries: Iterable[SessionEntry]) -> list[UsageRequest]:
    """Project each persisted model request, retaining unknown usage as gaps.

    The caller supplies the active branch, including entries before compaction.
    No message content, tool arguments or provider response payload is exposed.
    """
    requests: list[UsageRequest] = []
    for entry in entries:
        if not isinstance(entry, SessionMessageEntry):
            continue
        message = entry.message
        if getattr(message, "role", None) != "assistant":
            continue
        usage = getattr(message, "usage", None)
        inp = _tokens(getattr(usage, "input", None))
        output = _tokens(getattr(usage, "output", None))
        read = _tokens(getattr(usage, "cache_read", None))
        write = _tokens(getattr(usage, "cache_write", None))
        reported = getattr(usage, "cache_read_reported", None)
        has_usage = reported is True or any(value is not None and value > 0 for value in (inp, output, read, write))
        total_input = _tokens(inp + read + write) if has_usage and inp is not None and read is not None and write is not None else None
        cache_known = has_usage and (reported is True or (reported is None and read is not None and read > 0))
        raw_cost = _number(getattr(getattr(usage, "cost", None), "total", None))
        cost = raw_cost if has_usage or (raw_cost is not None and raw_cost > 0) else None
        requests.append({
            "id": entry.id,
            "index": len(requests) + 1,
            "timestamp": _timestamp(message, entry),
            "provider": _text(getattr(message, "provider", "")),
            "model": _text(getattr(message, "model", "")),
            "stopReason": _text(getattr(message, "stop_reason", "")) or "unknown",
            "inputTokens": total_input,
            "uncachedInputTokens": inp if has_usage else None,
            "outputTokens": output if has_usage else None,
            "cacheReadTokens": read if cache_known else None,
            "cacheWriteTokens": write if cache_known else None,
            "cacheHitRate": read / total_input * 100 if cache_known and read is not None and total_input else None,
            "cost": cost,
        })
    return requests
