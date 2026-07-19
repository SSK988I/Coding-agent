"""Compaction summary prompts + conversation serialization.

The four prompt constants drive the summarization LLM call. ``serialize_conversation``
flattens messages into a labeled plaintext block so the model doesn't mistake
the input for a conversation to continue.

"""
from __future__ import annotations

from typing import Any


__all__ = [
    "SUMMARIZATION_SYSTEM_PROMPT",
    "SUMMARIZATION_PROMPT",
    "UPDATE_SUMMARIZATION_PROMPT",
    "TURN_PREFIX_SUMMARIZATION_PROMPT",
    "SUMMARY_TOOL_RESULT_LIMIT",
    "ESTIMATED_IMAGE_CHARS",
    "serialize_conversation",
    "truncate_for_summary",
]

#: Tool result text is truncated to this many chars before going into a summary prompt.
SUMMARY_TOOL_RESULT_LIMIT = 2000

#: Rough char cost of one image for token estimation.
ESTIMATED_IMAGE_CHARS = 4800


SUMMARIZATION_SYSTEM_PROMPT = (
    "You are a summarization assistant. Your task is to read the provided conversation "
    "and produce a structured summary. Do not continue the conversation or answer the "
    "user's request. Output only the summary."
)

SUMMARIZATION_PROMPT = """Produce a structured summary of the conversation above, following this format exactly:

## Goal
What the user is trying to accomplish.

## Constraints & Preferences
Any stated requirements, constraints, style preferences, or rules.

## Progress
- **Done:** completed steps.
- **In Progress:** currently active work.
- **Blocked:** anything blocked, with the blocker.

## Key Decisions
Important decisions and their rationale.

## Next Steps
The immediate next actions.

## Critical Context
Anything else essential to continue effectively: file paths, function/class names, exact error messages, commands, identifiers.

Be concise. Preserve exact file paths, identifiers, and error text verbatim. Do not include conversational filler."""


UPDATE_SUMMARIZATION_PROMPT = """You are updating an existing summary with new conversation history.

Rules:
- Keep all existing information that is still relevant.
- Add new information from the new conversation.
- Move items from "In Progress" to "Done" when the new history shows they completed.
- Keep exact file paths, function/class names, and error messages verbatim.
- Do not remove information unless it is clearly superseded.

Output the full updated summary in the same format (Goal / Constraints & Preferences / Progress / Key Decisions / Next Steps / Critical Context)."""


TURN_PREFIX_SUMMARIZATION_PROMPT = """Summarize the beginning of an interrupted turn so it can be reattached. Format:

## Original Request
The user's request that started this turn.

## Early Progress
What was done before the interruption.

## Context for Suffix
What the model needs to know to continue coherently after the cut.

Be concise."""


# ─── conversation serialization ────────────────────

def truncate_for_summary(text: str, limit: int = SUMMARY_TOOL_RESULT_LIMIT) -> str:
    """Truncate text to ``limit`` chars with an ellipsis marker."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


def _content_text(content: Any) -> str:
    """Extract a best-effort text representation from message content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not hasattr(block, "type"):
                continue
            btype = block.type
            if btype == "text" and hasattr(block, "text"):
                parts.append(block.text)
            elif btype == "thinking" and hasattr(block, "thinking"):
                parts.append(f"(thinking) {block.thinking}")
            elif btype == "image":
                parts.append("[image]")
            elif btype == "toolCall" and hasattr(block, "name"):
                args = getattr(block, "arguments", {}) or {}
                import json as _json
                parts.append(f"{block.name}({_json.dumps(args, ensure_ascii=False)})")
        return "\n".join(parts)
    return str(content)


def serialize_conversation(messages: list) -> str:
    """Flatten messages into labeled plaintext for the summarization prompt.

    Each message becomes a
    labeled block. Tool results are truncated to SUMMARY_TOOL_RESULT_LIMIT chars.
    """
    lines: list[str] = []
    for m in messages:
        role = getattr(m, "role", None)
        if role == "user":
            lines.append(f"[User]: {_content_text(getattr(m, 'content', ''))}")
        elif role == "assistant":
            content = getattr(m, "content", [])
            # Split thinking from text/tool_calls for clarity.
            thinking_parts: list[str] = []
            text_parts: list[str] = []
            tool_parts: list[str] = []
            for block in content if isinstance(content, list) else []:
                btype = getattr(block, "type", None)
                if btype == "thinking":
                    thinking_parts.append(getattr(block, "thinking", ""))
                elif btype == "text":
                    text_parts.append(getattr(block, "text", ""))
                elif btype == "toolCall":
                    import json as _json
                    args = getattr(block, "arguments", {}) or {}
                    tool_parts.append(f"{getattr(block, 'name', '')}({_json.dumps(args, ensure_ascii=False)})")
            if thinking_parts:
                lines.append(f"[Assistant thinking]: {' '.join(thinking_parts)}")
            if text_parts:
                lines.append(f"[Assistant]: {' '.join(text_parts)}")
            if tool_parts:
                lines.append(f"[Assistant tool calls]: {'; '.join(tool_parts)}")
        elif role == "toolResult":
            text = _content_text(getattr(m, "content", []))
            tool_name = getattr(m, "tool_name", "tool")
            lines.append(f"[Tool result ({tool_name})]: {truncate_for_summary(text)}")
        elif role == "compactionSummary":
            lines.append(f"[Prior summary]: {getattr(m, 'summary', '')}")
    return "\n\n".join(lines)
