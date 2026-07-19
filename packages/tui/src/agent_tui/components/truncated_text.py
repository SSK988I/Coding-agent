"""TruncatedText component.

Single-line text truncated to fit the viewport width. Takes only the first
line (stops at newline), truncates with ``truncate_to_width``.
"""
from __future__ import annotations

from agent_tui.tui import Component
from agent_tui.utils import truncate_to_width, visible_width


class TruncatedText(Component):
    """Single-line text, truncated to viewport width.

    Args:
        text: The text (only the first line is used).
        padding_x: Left/right padding (default 0).
        padding_y: Top/bottom padding (default 0).
    """

    def __init__(self, text: str = "", padding_x: int = 0, padding_y: int = 0) -> None:
        self._text = text
        self._padding_x = padding_x
        self._padding_y = padding_y

    @property
    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text

    def invalidate(self) -> None:
        pass  # No cached state.

    def render(self, width: int) -> list[str]:
        result: list[str] = []
        empty_line = " " * width

        # Top padding.
        for _ in range(self._padding_y):
            result.append(empty_line)

        # Available width after padding.
        available_width = max(1, width - self._padding_x * 2)

        # Take only the first line.
        single_line = self._text
        newline_idx = self._text.find("\n")
        if newline_idx != -1:
            single_line = self._text[:newline_idx]

        # Truncate.
        display_text = truncate_to_width(single_line, available_width)

        # Add horizontal padding.
        left_pad = " " * self._padding_x
        right_pad = " " * self._padding_x
        line_with_padding = left_pad + display_text + right_pad
        vis_len = visible_width(line_with_padding)
        padding_needed = max(0, width - vis_len)
        result.append(line_with_padding + " " * padding_needed)

        # Bottom padding.
        for _ in range(self._padding_y):
            result.append(empty_line)

        return result
