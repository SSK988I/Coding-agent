"""UserMessageComponent — renders user input in the TUI.

Wraps user text in a box with
userMessageBg background and applies Markdown rendering.

"""
from __future__ import annotations

from agent_tui import Box, Container, Markdown
from agent_tui.components.markdown import MarkdownTheme


class UserMessageComponent(Container):
    """Renders a user message with themed background and Markdown."""

    def __init__(
        self,
        text: str,
        *,
        markdown_theme: MarkdownTheme | None = None,
        output_pad: int = 1,
        bg_fn=None,
    ) -> None:
        super().__init__()
        self._text = text
        self._markdown_theme = markdown_theme
        self._output_pad = output_pad
        self._bg_fn = bg_fn

        self._build()

    def _build(self) -> None:
        """Build the component tree: Box > Markdown."""
        self.clear()
        box = Box(
            padding_x=self._output_pad,
            padding_y=1,
            bg_fn=self._bg_fn,
        )
        box.add_child(Markdown(
            self._text,
            theme=self._markdown_theme,
        ))
        self.add_child(box)

    def set_text(self, text: str) -> None:
        """Update the message text and rebuild."""
        self._text = text
        self._build()
        self.invalidate()
