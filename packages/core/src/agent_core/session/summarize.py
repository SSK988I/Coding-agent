"""Compaction LLM call.

Drives the summarization: assembles the prompt from the pure-function
preparation, calls the model (via the same stream_fn the agent uses, so auth
and provider are reused), and returns a CompactionResult.

Uses a non-streaming-style consumption: we drain the stream and take .result().
The summary model is the same model the agent is currently using.

"""
from __future__ import annotations

from typing import Any

from agent_llm import Context, UserMessage

from agent_core.session.prompts import (
    SUMMARIZATION_PROMPT,
    SUMMARIZATION_SYSTEM_PROMPT,
    TURN_PREFIX_SUMMARIZATION_PROMPT,
    UPDATE_SUMMARIZATION_PROMPT,
    serialize_conversation,
)
from agent_core.session.types import CompactionDetails, CompactionPreparation, CompactionResult
from agent_core.types import StreamFn

__all__ = ["compact", "generate_summary", "format_file_operations"]

#: Default max output tokens for a summary = 0.8 * reserve_tokens.
_SUMMARY_BUDGET_FACTOR = 0.8
#: Turn-prefix summary uses 0.5 * reserve.
_TURN_PREFIX_BUDGET_FACTOR = 0.5


def format_file_operations(ops: Any) -> str:
    """Append a <read-files>/<modified-files> block to the summary."""
    if ops is None:
        return ""
    read = sorted(getattr(ops, "read", set()) or set())
    modified = sorted((getattr(ops, "written", set()) | getattr(ops, "edited", set())) or set())
    if not read and not modified:
        return ""
    parts: list[str] = ["\n\n---\n"]
    if read:
        parts.append("<read-files>\n" + "\n".join(read) + "\n</read-files>")
    if modified:
        parts.append("<modified-files>\n" + "\n".join(modified) + "\n</modified-files>")
    return "\n".join(parts)


async def _summarize_via_stream(
    *,
    model: Any,
    stream_fn: StreamFn,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    get_api_key: Any = None,
    reasoning: Any = None,
) -> str:
    """Run a single summarization completion via the agent's stream_fn.

    Drains the event stream and returns the final text. Non-streaming
    semantically — we just collect until terminal.
    """
    context = Context(
        system_prompt=system_prompt,
        messages=[UserMessage(content=user_prompt)],
        tools=None,
    )
    options: dict = {"max_tokens": max_tokens}
    if get_api_key is not None:
        key = get_api_key(getattr(model, "provider", ""))
        if hasattr(key, "__await__"):
            key = await key  # type: ignore[assignment]
        if key:
            options["api_key"] = key
    if reasoning:
        options["reasoning"] = reasoning

    event_stream = stream_fn(model, context, options or None)
    # Drain.
    async for _ in event_stream:
        pass
    final = await event_stream.result()
    # Extract text.
    text = ""
    for b in getattr(final, "content", []) or []:
        if getattr(b, "type", None) == "text":
            text += getattr(b, "text", "") or ""
    return text


async def generate_summary(
    *,
    messages: list,
    model: Any,
    stream_fn: StreamFn,
    settings: Any,
    previous_summary: str | None = None,
    custom_instructions: str | None = None,
    get_api_key: Any = None,
    reasoning: Any = None,
    is_turn_prefix: bool = False,
) -> str:
    """Generate one summary (history or turn-prefix) via the model."""
    budget_factor = _TURN_PREFIX_BUDGET_FACTOR if is_turn_prefix else _SUMMARY_BUDGET_FACTOR
    max_tokens = max(256, int(settings.reserve_tokens * budget_factor))

    base = (TURN_PREFIX_SUMMARIZATION_PROMPT if is_turn_prefix else
            (UPDATE_SUMMARIZATION_PROMPT if previous_summary else SUMMARIZATION_PROMPT))
    if custom_instructions:
        base += "\n\nAdditional focus: " + custom_instructions

    conversation_text = serialize_conversation(messages)
    parts = [f"<conversation>\n{conversation_text}\n</conversation>\n\n"]
    if previous_summary:
        parts.append(f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n")
    parts.append(base)
    user_prompt = "".join(parts)

    return await _summarize_via_stream(
        model=model, stream_fn=stream_fn,
        system_prompt=SUMMARIZATION_SYSTEM_PROMPT,
        user_prompt=user_prompt, max_tokens=max_tokens,
        get_api_key=get_api_key, reasoning=reasoning,
    )


async def compact(
    preparation: CompactionPreparation,
    *,
    model: Any,
    stream_fn: StreamFn,
    get_api_key: Any = None,
    reasoning: Any = None,
    custom_instructions: str | None = None,
) -> CompactionResult:
    """Run the LLM summarization and return a CompactionResult."""
    settings = preparation.settings
    prev = preparation.previous_summary

    if preparation.is_split_turn:
        # History + turn-prefix.
        if preparation.messages_to_summarize:
            history_summary = await generate_summary(
                messages=preparation.messages_to_summarize,
                model=model, stream_fn=stream_fn, settings=settings,
                previous_summary=prev, custom_instructions=custom_instructions,
                get_api_key=get_api_key, reasoning=reasoning,
            )
        else:
            history_summary = prev or "No prior history."

        turn_prefix_summary = await generate_summary(
            messages=preparation.turn_prefix_messages,
            model=model, stream_fn=stream_fn, settings=settings,
            previous_summary=None, custom_instructions=custom_instructions,
            get_api_key=get_api_key, reasoning=reasoning, is_turn_prefix=True,
        )
        summary = (
            history_summary
            + "\n\n---\n\n**Turn Context (split turn):**\n\n"
            + turn_prefix_summary
        )
    else:
        summary = await generate_summary(
            messages=preparation.messages_to_summarize,
            model=model, stream_fn=stream_fn, settings=settings,
            previous_summary=prev, custom_instructions=custom_instructions,
            get_api_key=get_api_key, reasoning=reasoning,
        )

    summary += format_file_operations(preparation.file_ops)

    details: CompactionDetails | None = None
    if preparation.file_ops is not None:
        ops = preparation.file_ops
        details = CompactionDetails(
            read_files=sorted(getattr(ops, "read", set()) or set()),
            modified_files=sorted((getattr(ops, "written", set()) | getattr(ops, "edited", set())) or set()),
        )

    return CompactionResult(
        summary=summary,
        first_kept_entry_id=preparation.first_kept_entry_id,
        tokens_before=preparation.tokens_before,
        details=details,
    )
