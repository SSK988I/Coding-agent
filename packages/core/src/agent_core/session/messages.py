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

from agent_llm import Message, UserMessage

__all__ = [
    "CompactionSummaryMessage",
    "convert_messages_with_compaction",
    "COMPACTION_SUMMARY_PREFIX",
    "COMPACTION_SUMMARY_SUFFIX",
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
    return out
