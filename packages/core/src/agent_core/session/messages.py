"""Compaction summary message and LLM conversion support.

Additional roles such as ``compactionSummary`` and ``branchSummary`` are
represented by a small ``CompactionSummaryMessage`` dataclass and a
``convert_messages_with_compaction`` that projects the transcript to LLM
messages, wrapping compaction summaries as user messages with ``<summary>``
tags.

"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from agent_llm import Message, TextContent, ToolCall, ToolResultMessage, UserMessage

__all__ = [
    "CompactionSummaryMessage",
    "convert_messages_with_compaction",
    "COMPACTION_SUMMARY_PREFIX",
    "COMPACTION_SUMMARY_SUFFIX",
    "repair_incomplete_tool_calls",
]

COMPACTION_SUMMARY_PREFIX = (
    "The conversation history before this point was compacted into the following summary:\n\n<summary>\n"
)
COMPACTION_SUMMARY_SUFFIX = "\n</summary>"


@dataclass
class CompactionSummaryMessage:
    """A synthetic message representing a compaction summary.

    Lives in the transcript (AgentState.messages) as the first message after a
    compaction. ``convert_messages_with_compaction`` turns it into a user
    message wrapping ``summary`` in <summary> tags before sending to the LLM.
    """
    role: Literal["compactionSummary"] = "compactionSummary"
    summary: str = ""
    tokens_before: int = 0
    timestamp: float = 0.0


def repair_incomplete_tool_calls(messages: list[Message]) -> list[Message]:
    """Return a provider-safe transcript with every tool call answered.

    A process can be stopped after an assistant tool-call message is persisted
    but before its tool result is written (for example while a desktop approval
    dialog is open). OpenAI-compatible providers reject that history on the
    next request. Insert synthetic error results for only the missing calls and
    discard orphan/duplicate results; the source session entries are left
    untouched.
    """
    repaired: list[Message] = []
    pending: dict[str, str] = {}

    def close_pending() -> None:
        for tool_call_id, tool_name in pending.items():
            repaired.append(ToolResultMessage(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                content=[TextContent(text="Tool execution was interrupted before completion.")],
                is_error=True,
            ))
        pending.clear()

    for message in messages:
        role = getattr(message, "role", None)
        if role == "toolResult":
            tool_call_id = getattr(message, "tool_call_id", "")
            if tool_call_id in pending:
                repaired.append(message)
                pending.pop(tool_call_id, None)
            # A tool result without a preceding pending call is invalid too.
            continue

        if pending:
            close_pending()

        if role == "assistant" and getattr(message, "error_message", None):
            content = getattr(message, "content", [])
            has_visible_content = any(
                bool(getattr(block, "text", ""))
                or bool(getattr(block, "thinking", ""))
                or isinstance(block, ToolCall)
                for block in content if isinstance(content, list)
            )
            if not has_visible_content:
                # Transport failures are UI diagnostics, not conversation
                # turns. Replaying an empty assistant message only adds noise.
                continue

        repaired.append(message)
        if role == "assistant":
            content = getattr(message, "content", [])
            for block in content if isinstance(content, list) else []:
                if isinstance(block, ToolCall) and block.id:
                    pending[block.id] = block.name

    if pending:
        close_pending()
    return repaired


def convert_messages_with_compaction(messages: list) -> list[Message]:
    """Project transcript messages to LLM-facing messages.

    Conversion rules:
      - compactionSummary -> UserMessage with <summary>-wrapped text
      - user/assistant/toolResult -> pass through
      - anything else -> dropped
    """
    out: list[Message] = []
    for m in messages:
        role = getattr(m, "role", None)
        if role in ("user", "assistant", "toolResult"):
            out.append(m)  # type: ignore[arg-type]
        elif role == "compactionSummary":
            out.append(UserMessage(  # type: ignore[arg-type]
                content=COMPACTION_SUMMARY_PREFIX + m.summary + COMPACTION_SUMMARY_SUFFIX
            ))
    return repair_incomplete_tool_calls(out)
