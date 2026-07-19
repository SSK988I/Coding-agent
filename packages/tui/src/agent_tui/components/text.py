"""Text component.

Displays multi-line text with word wrapping, padding, and an optional
background color function. Caches rendered output by (text, width).

"""
from __future__ import annotations

from typing import Callable

from agent_tui.tui import Component
from agent_tui.utils import apply_background_to_line, visible_width, wrap_text_with_ansi


class Text(Component):
    """Multi-line text with word wrap and optional background.

    Args:
        text: The text to display.
        padding_x: Left/right padding in columns (default 1).
        padding_y: Top/bottom padding in lines (default 1).
        custom_bg_fn: Optional ``(text: str) -> str`` that wraps text in a
            background ANSI sequence. When set, padding lines also get the bg.
    """

    def __init__(
        self,
        text: str = "",
        padding_x: int = 1,
        padding_y: int = 1,
        custom_bg_fn: "Callable[[str], str] | None" = None,
    ) -> None:
        self._text = text
        self._padding_x = padding_x
        self._padding_y = padding_y
        self._custom_bg_fn = custom_bg_fn

        # Cache.
        self._cached_text: "str | None" = None
        self._cached_width: int = -1
        self._cached_lines: "list[str] | None" = None

    @property
    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text
        self.invalidate()

    def set_custom_bg_fn(self, bg_fn: "Callable[[str], str] | None") -> None:
        self._custom_bg_fn = bg_fn
        self.invalidate()

    def invalidate(self) -> None:
        self._cached_text = None
        self._cached_width = -1
        self._cached_lines = None

    def render(self, width: int) -> list[str]:
        # Check cache.
        if (
            self._cached_lines is not None
            and self._cached_text == self._text
            and self._cached_width == width
        ):
            return self._cached_lines

        # Empty text → no lines.
        if not self._text or not self._text.strip():
            result: list[str] = []
            self._cached_text = self._text
            self._cached_width = width
            self._cached_lines = result
            return result

        # Tabs → 3 spaces.
        normalized = self._text.replace("\t", "   ")

        # Content width after padding.
        content_width = max(1, width - self._padding_x * 2)

        # Word-wrap (preserves ANSI, does NOT pad).
        wrapped_lines = wrap_text_with_ansi(normalized, content_width)

        # Add margins and background.
        left_margin = " " * self._padding_x
        right_margin = " " * self._padding_x
        content_lines: list[str] = []
        for line in wrapped_lines:
            line_with_margins = left_margin + line + right_margin
            if self._custom_bg_fn:
                content_lines.append(apply_background_to_line(line_with_margins, width, self._custom_bg_fn))
            else:
                vis_len = visible_width(line_with_margins)
                padding_needed = max(0, width - vis_len)
                content_lines.append(line_with_margins + " " * padding_needed)

        # Top/bottom padding.
        empty_line = " " * width
        empty_lines: list[str] = []
        for _ in range(self._padding_y):
            if self._custom_bg_fn:
                empty_lines.append(apply_background_to_line(empty_line, width, self._custom_bg_fn))
            else:
                empty_lines.append(empty_line)

        result = [*empty_lines, *content_lines, *empty_lines]

        # Update cache.
        self._cached_text = self._text
        self._cached_width = width
        self._cached_lines = result

        return result if result else [""]
