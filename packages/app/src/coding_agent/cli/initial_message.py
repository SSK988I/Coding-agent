"""Build the initial prompt from stdin, @file text, and the first CLI message.

Combines the three sources into a single initial prompt for non-interactive
(print) mode, consuming the first positional
message so the remainder can be sent as follow-up prompts.

"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_llm import ImageContent, TextContent, UserMessage

if TYPE_CHECKING:
    from coding_agent.cli.args import Args


@dataclass
class InitialMessageResult:
    """Result of combining stdin + @file + first message."""

    initial_message: str | None = None
    initial_images: list[ImageContent] | None = None

    def to_prompt(self) -> str | UserMessage | None:
        """Build the actual Agent prompt, preserving image attachments."""
        if not self.initial_images:
            return self.initial_message
        content = []
        if self.initial_message:
            content.append(TextContent(text=self.initial_message))
        content.extend(self.initial_images)
        return UserMessage(content=content)


def build_initial_message(
    parsed: "Args",
    file_text: str | None = None,
    file_images: list[ImageContent] | None = None,
    stdin_content: str | None = None,
) -> InitialMessageResult:
    """Combine stdin, @file text, and the first CLI message into one prompt.

    Order: ``[stdin?, file_text?, parsed.messages[0]]``, joined with ``""``
    (no separator because each piece carries its own trailing newline). The first
    positional message is consumed (mutates ``parsed.messages``) so callers can
    send the rest as follow-up prompts.
    """
    parts: list[str] = []
    if stdin_content is not None:
        # Join directly because each piece carries its own trailing newline;
        # our stdin is .strip()ed, so add one when more parts follow.
        if (file_text or (getattr(parsed, "messages", None) or [])) and not stdin_content.endswith("\n"):
            parts.append(stdin_content + "\n")
        else:
            parts.append(stdin_content)
    if file_text:
        parts.append(file_text)

    messages = list(getattr(parsed, "messages", []) or [])
    if messages:
        parts.append(messages[0])
        # Consume the first message so it is not processed twice.
        parsed.messages = messages[1:]

    return InitialMessageResult(
        initial_message="".join(parts) if parts else None,
        initial_images=file_images if file_images else None,
    )
