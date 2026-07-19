"""Message <-> JSON serialization for session persistence.

Messages use dataclasses in memory, so persistence needs explicit encoders and
type-aware decoders.

Design:
  - Encode: ``dataclasses.asdict`` for the dataclass tree, which captures all
    fields including opaque blobs like ``thinking_signature`` (must round-trip
    verbatim for providers that require signatures on historical thinking).
  - Decode: dispatch on the dict's ``role`` (messages) or ``type`` (content
    blocks / nested usage) discriminator, reconstructing the dataclass tree.

The decoder discriminates content blocks and messages by their ``type`` and
``role`` fields, and reconstructs nested usage and cost records.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from agent_llm import (
    AssistantMessage,
    ImageContent,
    Message,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)

__all__ = [
    "message_to_dict",
    "dict_to_message",
    "messages_to_dicts",
    "dicts_to_messages",
    "content_block_to_dict",
    "dict_to_content_block",
    "usage_to_dict",
    "dict_to_usage",
]


# ─── content blocks ────────────────────────────────

def content_block_to_dict(block: Any) -> dict:
    """Encode a single content block (TextContent/ThinkingContent/ImageContent/ToolCall)."""
    if dataclasses.is_dataclass(block):
        return dataclasses.asdict(block)
    if isinstance(block, dict):
        return dict(block)
    raise TypeError(f"Cannot serialize content block of type {type(block)!r}")


def dict_to_content_block(d: dict) -> Any:
    """Decode a content block dict by its ``type`` discriminator."""
    btype = d.get("type")
    if btype == "text":
        return TextContent(
            text=d.get("text", ""),
            text_signature=d.get("text_signature"),
        )
    if btype == "thinking":
        return ThinkingContent(
            thinking=d.get("thinking", ""),
            thinking_signature=d.get("thinking_signature"),
            redacted=bool(d.get("redacted", False)),
        )
    if btype == "image":
        return ImageContent(
            data=d.get("data", ""),
            mime_type=d.get("mime_type", ""),
        )
    if btype == "toolCall":
        return ToolCall(
            id=d.get("id", ""),
            name=d.get("name", ""),
            arguments=dict(d.get("arguments") or {}),
            thought_signature=d.get("thought_signature"),
        )
    raise ValueError(f"Unknown content block type: {btype!r}")


# ─── usage / cost ──────────────────────────

def usage_to_dict(usage: Usage) -> dict:
    if dataclasses.is_dataclass(usage):
        return dataclasses.asdict(usage)
    return dict(usage)


def dict_to_usage(d: dict | None) -> Usage:
    if not d:
        return Usage()
    cost_raw = d.get("cost") or {}
    return Usage(
        input=int(d.get("input", 0) or 0),
        output=int(d.get("output", 0) or 0),
        cache_read=int(d.get("cache_read", 0) or 0),
        cache_write=int(d.get("cache_write", 0) or 0),
        cache_write_1h=int(d.get("cache_write_1h", 0) or 0),
        reasoning=d.get("reasoning"),
        total_tokens=int(d.get("total_tokens", 0) or 0),
        cost=UsageCost(
            input=float(cost_raw.get("input", 0) or 0),
            output=float(cost_raw.get("output", 0) or 0),
            cache_read=float(cost_raw.get("cache_read", 0) or 0),
            cache_write=float(cost_raw.get("cache_write", 0) or 0),
            total=float(cost_raw.get("total", 0) or 0),
        ),
    )


# ─── user content (str | list[UserContentBlock]) ──────────────────────

def _encode_user_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return [content_block_to_dict(c) for c in content]
    return content


def _decode_user_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return [dict_to_content_block(c) if isinstance(c, dict) else c for c in content]
    return content


# ─── messages ───────────────────────────────────────

def message_to_dict(message: Message) -> dict:
    """Encode a Message (User/Assistant/ToolResult) to a JSON-safe dict.

    Preserves all fields including opaque provider signatures and nested Usage.
    """
    role = getattr(message, "role", None)
    if role == "user":
        return {
            "role": "user",
            "content": _encode_user_content(message.content),
            "timestamp": getattr(message, "timestamp", None),
        }
    if role == "assistant":
        return {
            "role": "assistant",
            "content": [content_block_to_dict(b) for b in message.content],
            "api": message.api,
            "provider": message.provider,
            "model": message.model,
            "response_model": message.response_model,
            "response_id": message.response_id,
            "usage": usage_to_dict(message.usage),
            "stop_reason": message.stop_reason,
            "error_message": message.error_message,
            "timestamp": getattr(message, "timestamp", None),
        }
    if role == "toolResult":
        return {
            "role": "toolResult",
            "tool_call_id": message.tool_call_id,
            "tool_name": message.tool_name,
            "content": [content_block_to_dict(b) for b in message.content],
            "details": message.details,
            "is_error": message.is_error,
            "timestamp": getattr(message, "timestamp", None),
        }
    # Unknown role: best-effort asdict (for forward compat / custom messages).
    if dataclasses.is_dataclass(message):
        return dataclasses.asdict(message)
    raise TypeError(f"Cannot serialize message of type {type(message)!r}")


def dict_to_message(d: dict) -> Message:
    """Decode a dict to a Message by its ``role`` discriminator."""
    role = d.get("role")
    if role == "user":
        return UserMessage(
            content=_decode_user_content(d.get("content", "")),
            timestamp=float(d.get("timestamp") or 0.0),
        )
    if role == "assistant":
        return AssistantMessage(
            content=[dict_to_content_block(b) for b in (d.get("content") or [])],
            api=d.get("api", ""),
            provider=d.get("provider", ""),
            model=d.get("model", ""),
            response_model=d.get("response_model"),
            response_id=d.get("response_id"),
            usage=dict_to_usage(d.get("usage")),
            stop_reason=d.get("stop_reason", "stop"),
            error_message=d.get("error_message"),
            timestamp=float(d.get("timestamp") or 0.0),
        )
    if role == "toolResult":
        return ToolResultMessage(
            tool_call_id=d.get("tool_call_id", ""),
            tool_name=d.get("tool_name", ""),
            content=[dict_to_content_block(b) for b in (d.get("content") or [])],
            details=d.get("details"),
            is_error=bool(d.get("is_error", False)),
            timestamp=float(d.get("timestamp") or 0.0),
        )
    raise ValueError(f"Unknown message role: {role!r}")


# ─── list helpers ─────────────────────────────────────────────────────

def messages_to_dicts(messages: list) -> list[dict]:
    return [message_to_dict(m) for m in messages]


def dicts_to_messages(dicts: list) -> list:
    return [dict_to_message(d) for d in dicts]
