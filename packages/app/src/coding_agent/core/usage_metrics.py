"""Shared, read-only usage projection for every client."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent_core.session.compaction import calculate_context_tokens_from_usage, estimate_tokens
from agent_core.session.prompts import ESTIMATED_IMAGE_CHARS
from coding_agent.core.messages import convert_to_llm


@dataclass
class UsageMetrics:
    # Percentages, not fractions. None means the provider did not report enough data.
    cache_hit_rate: float | None = None
    input_tokens: int = 0
    context_tokens: int | None = None
    context_window: int | None = None
    context_percent: float | None = None
    context_estimated: bool = True


def _message_tokens(message: Any) -> int:
    role = getattr(message, "role", None)
    if role not in {"user", "assistant", "toolResult"}:
        return sum(_message_tokens(converted) for converted in convert_to_llm([message]))
    if role == "user" and isinstance(message.content, str) and message.content.lstrip().startswith("/"):
        return 0
    tokens = estimate_tokens(message)
    # The core's heuristic already counts user images, but not tool-result images.
    if getattr(message, "role", None) == "toolResult":
        content = getattr(message, "content", [])
        tokens += sum(
            ESTIMATED_IMAGE_CHARS // 4 for block in content
            if getattr(block, "type", None) == "image"
        )
    return tokens


def project_usage_metrics(
    *, messages: list, entries: list, model: Any, system_prompt: str, tools: list,
    valid_anchor_ids: set[int] | None = None,
) -> UsageMetrics:
    result = UsageMetrics()
    cached = 0
    cache_complete = True
    anchor_ids: set[int] = set()
    for entry in entries:
        # Retained pre-compaction usage describes the OLD context, even though
        # the message itself is still visible after the summary.
        if getattr(entry, "type", "") in {"compaction", "model_change", "collaboration_mode_change"}:
            anchor_ids.clear()
        message = getattr(entry, "message", None)
        if getattr(message, "role", None) != "assistant":
            continue
        anchor_ids.add(id(message))
        completed = getattr(message, "stop_reason", "stop") not in {"error", "aborted"}
        usage = getattr(message, "usage", None)
        if usage is None:
            if completed:
                cache_complete = False
            continue
        inp = max(0, int(getattr(usage, "input", 0) or 0))
        read = max(0, int(getattr(usage, "cache_read", 0) or 0))
        write = max(0, int(getattr(usage, "cache_write", 0) or 0))
        total_input = inp + read + write
        result.input_tokens += total_input
        cached += read
        reported = getattr(usage, "cache_read_reported", None)
        if (total_input or completed) and not (reported is True or (reported is None and read > 0)):
            cache_complete = False
    if result.input_tokens and cache_complete:
        result.cache_hit_rate = cached / result.input_tokens * 100

    window = int(getattr(model, "context_window", 0) or 0)
    result.context_window = window if window > 0 else None
    anchor_index = None
    used = 0
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if (id(message) not in anchor_ids or (valid_anchor_ids is not None and id(message) not in valid_anchor_ids)
                or getattr(message, "stop_reason", "stop") in {"error", "aborted"}):
            continue
        if (getattr(message, "model", None), getattr(message, "provider", None)) != (
            getattr(model, "id", None), getattr(model, "provider", None),
        ):
            continue
        real = calculate_context_tokens_from_usage(getattr(message, "usage", None))
        if real > 0:
            anchor_index, used = index, real
            break
    if anchor_index is None:
        # Include prompt and tool definitions only for a fully heuristic estimate;
        # a real provider usage anchor already accounts for them.
        schemas = [{"name": tool.name, "description": tool.description, "parameters": tool.parameters} for tool in tools]
        header = len(system_prompt) + (len(json.dumps(schemas, ensure_ascii=False)) if schemas else 0)
        used = (header + 3) // 4 + sum(_message_tokens(message) for message in messages)
    else:
        trailing = messages[anchor_index + 1:]
        used += sum(_message_tokens(message) for message in trailing)
        result.context_estimated = bool(trailing)
    result.context_tokens = used
    if result.context_window is not None:
        result.context_percent = used / result.context_window * 100
    return result
