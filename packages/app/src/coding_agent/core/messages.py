"""Custom message types and LLM transformer.

Extends the base ``Message`` union with coding-agent specific roles
(bashExecution, custom, branchSummary, compactionSummary) and converts them
back to LLM-compatible ``Message`` objects for prompting and summarization.

Because Python's ``Message`` union is closed (UserMessage | AssistantMessage |
ToolResultMessage), the custom roles are represented as lightweight dataclasses
with a distinct ``role`` string. ``convert_to_llm`` duck-types on ``role``.

"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from agent_llm import (
    ImageContent,
    Message,
    TextContent,
    UserMessage,
)

# ─── Summary wrappers ────────────────────────────

#: Wraps a compaction summary so the model treats it as prior context.
#: The trailing newline after ``<summary>`` is required by the message format.
COMPACTION_SUMMARY_PREFIX = (
    "The conversation history before this point was compacted into the following summary:\n"
    "\n<summary>\n"
)
COMPACTION_SUMMARY_SUFFIX = "\n</summary>"

#: Wraps a branch summary. No trailing newline after ``<summary>``.
BRANCH_SUMMARY_PREFIX = (
    "The following is a summary of a branch that this conversation came back from:\n"
    "\n<summary>"
)
BRANCH_SUMMARY_SUFFIX = "</summary>"


# ─── Custom message roles ────────────────────────


@dataclass
class BashExecutionMessage:
    """A bash command run via the ``!`` shell-passthrough command.

    ``exclude_from_context`` (the ``!!`` prefix) drops it from LLM context.
    """

    role: Literal["bashExecution"] = "bashExecution"
    command: str = ""
    output: str = ""
    exit_code: int | None = None
    cancelled: bool = False
    truncated: bool = False
    full_output_path: str | None = None
    timestamp: float = 0.0
    exclude_from_context: bool = False


@dataclass
class CustomMessage:
    """Extension-injected message (role ``custom``)."""

    role: Literal["custom"] = "custom"
    custom_type: str = ""
    content: str | list[TextContent | ImageContent] = ""
    display: bool = True
    details: Any = None
    timestamp: float = 0.0


@dataclass
class BranchSummaryMessage:
    """Summary of an abandoned branch (role ``branchSummary``)."""

    role: Literal["branchSummary"] = "branchSummary"
    summary: str = ""
    from_id: str = ""
    timestamp: float = 0.0


@dataclass
class CompactionSummaryMessage:
    """Summary produced by context compaction (role ``compactionSummary``)."""

    role: Literal["compactionSummary"] = "compactionSummary"
    summary: str = ""
    tokens_before: int = 0
    timestamp: float = 0.0


#: Any message the agent/session may hold: the LLM union plus the custom roles.
AgentAppMessage = Any


# ─── bash execution → text ───────────────────────


def bash_execution_to_text(msg: BashExecutionMessage) -> str:
    """Render a BashExecutionMessage as user-facing text for LLM context."""
    text = f"Ran `{msg.command}`\n"
    if msg.output:
        text += f"```\n{msg.output}\n```"
    else:
        text += "(no output)"
    if msg.cancelled:
        text += "\n\n(command cancelled)"
    elif msg.exit_code is not None and msg.exit_code != 0:
        text += f"\n\nCommand exited with code {msg.exit_code}"
    if msg.truncated and msg.full_output_path:
        text += f"\n\n[Output truncated. Full output: {msg.full_output_path}]"
    return text


# ─── factories ────────────────────────────────


def create_branch_summary_message(summary: str, from_id: str, timestamp: float) -> BranchSummaryMessage:
    return BranchSummaryMessage(summary=summary, from_id=from_id, timestamp=timestamp)


def create_compaction_summary_message(
    summary: str, tokens_before: int, timestamp: float,
) -> CompactionSummaryMessage:
    return CompactionSummaryMessage(
        summary=summary, tokens_before=tokens_before, timestamp=timestamp,
    )


def create_custom_message(
    custom_type: str,
    content: str | list[TextContent | ImageContent],
    display: bool,
    details: Any,
    timestamp: float,
) -> CustomMessage:
    return CustomMessage(
        custom_type=custom_type, content=content, display=display,
        details=details, timestamp=timestamp,
    )


# ─── convert_to_llm ───────────────────────────


def convert_to_llm(messages: list[AgentAppMessage]) -> list[Message]:
    """Transform app messages (incl. custom roles) into LLM-compatible messages.

    - bashExecution: dropped if ``exclude_from_context``, else user text.
    - custom: user message (string wrapped in a TextContent).
    - branchSummary / compactionSummary: user message wrapped in prefix/suffix.
    - user / assistant / toolResult: passed through unchanged.
    - unknown roles: dropped.
    """
    result: list[Message] = []
    for m in messages:
        role = getattr(m, "role", None)
        if role in ("user", "assistant", "toolResult"):
            result.append(m)
        elif role == "bashExecution":
            if getattr(m, "exclude_from_context", False):
                continue
            result.append(UserMessage(
                content=[TextContent(text=bash_execution_to_text(m))],
                timestamp=getattr(m, "timestamp", 0.0),
            ))
        elif role == "custom":
            content = m.content
            if isinstance(content, str):
                content = [TextContent(text=content)]
            result.append(UserMessage(
                content=content, timestamp=getattr(m, "timestamp", 0.0),
            ))
        elif role == "branchSummary":
            text = BRANCH_SUMMARY_PREFIX + m.summary + BRANCH_SUMMARY_SUFFIX
            result.append(UserMessage(
                content=[TextContent(text=text)],
                timestamp=getattr(m, "timestamp", 0.0),
            ))
        elif role == "compactionSummary":
            text = COMPACTION_SUMMARY_PREFIX + m.summary + COMPACTION_SUMMARY_SUFFIX
            result.append(UserMessage(
                content=[TextContent(text=text)],
                timestamp=getattr(m, "timestamp", 0.0),
            ))
        # Unknown roles are omitted from the LLM context.
    return result
