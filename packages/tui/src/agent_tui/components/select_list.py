"""SelectList component.

A reusable popup list used by autocomplete (slash commands + argument
selection). Renders one item per line with the selected item in reverse
video; supports Up/Down navigation with wrap-around and a scroll window.

The component does not filter internally — the
caller (the autocomplete provider) hands in an already-filtered item list.
The popup only owns selection state + rendering.

"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List

from agent_tui.tui import Component
from agent_tui.utils import visible_width


@dataclass
class SelectItem:
    """One row in a SelectList.

    ``value`` is what gets inserted into the editor when the item is
    accepted; ``label`` is the primary display text; ``description`` is a
    secondary, dimmer column.
    """
    value: str
    label: str
    description: "str | None" = None


class SelectList(Component):
    """Popup selection list.

    Maintains ``items``, ``selected_index``, and a ``max_visible`` scroll
    window. Auto-scrolls to keep the selection visible and shows a
    ``(N/M)`` indicator when the list overflows.

    The list is rendered full-width (the editor caller pads each line) —
    no internal padding; consumers may wrap with a layout helper.
    """

    def __init__(
        self,
        items: "list[SelectItem] | None" = None,
        max_visible: int = 5,
        theme: Any = None,
    ) -> None:
        self._items: list[SelectItem] = list(items) if items else []
        self._max_visible = max(1, max_visible)
        self._selected_index = 0
        self._scroll_offset = 0
        # Theme is optional and currently only controls the reverse-video
        # style (kept for forward-compat with the SelectListTheme).
        self._theme = theme

    # ── public state ──────────────────────────────────────────────────

    @property
    def items(self) -> "list[SelectItem]":
        return self._items

    def set_items(self, items: "list[SelectItem]") -> None:
        """Replace the list. Resets selection to the top."""
        self._items = list(items)
        self._selected_index = 0
        self._scroll_offset = 0

    @property
    def selected_index(self) -> int:
        return self._selected_index

    def set_selected_index(self, index: int) -> None:
        """Clamp + scroll the window to keep the index visible."""
        if not self._items:
            self._selected_index = 0
            return
        self._selected_index = max(0, min(index, len(self._items) - 1))
        self._scroll_to_selection()

    def get_selected(self) -> "SelectItem | None":
        """Return the highlighted item, or None if the list is empty."""
        if not self._items:
            return None
        return self._items[self._selected_index]

    def is_empty(self) -> bool:
        return not self._items

    # ── navigation ────────────────────────────────────────────────────

    def move_up(self) -> None:
        """Move selection up, wrapping to the bottom."""
        if not self._items:
            return
        self._selected_index = (self._selected_index - 1) % len(self._items)
        self._scroll_to_selection()

    def move_down(self) -> None:
        """Move selection down, wrapping to the top."""
        if not self._items:
            return
        self._selected_index = (self._selected_index + 1) % len(self._items)
        self._scroll_to_selection()

    # ── scroll ────────────────────────────────────────────────────────

    def _scroll_to_selection(self) -> None:
        # Keep the selection within the visible window.
        if self._selected_index < self._scroll_offset:
            self._scroll_offset = self._selected_index
        elif self._selected_index >= self._scroll_offset + self._max_visible:
            self._scroll_offset = self._selected_index - self._max_visible + 1
        max_scroll = max(0, len(self._items) - self._max_visible)
        self._scroll_offset = max(0, min(self._scroll_offset, max_scroll))

    # ── render ────────────────────────────────────────────────────────

    def render(self, width: int) -> List[str]:
        """Render the visible window to lines.

        Each line is ``label`` + padded ``description``; the selected line
        is wrapped in reverse-video (``\\x1b[7m ... \\x1b[0m``). When the
        list overflows the window, a ``(N/M)`` indicator is appended.
        """
        if not self._items:
            return []

        total = len(self._items)
        end = min(self._scroll_offset + self._max_visible, total)
        visible = self._items[self._scroll_offset:end]

        # Reserve trailing space for the "(N/M)" indicator when overflowing.
        overflow = total > self._max_visible
        indicator = ""
        if overflow:
            indicator = f" ({self._selected_index + 1}/{total})"

        lines: list[str] = []
        for offset, item in enumerate(visible):
            actual_index = self._scroll_offset + offset
            is_selected = actual_index == self._selected_index

            label = item.label or ""
            # Label column: up to half the width, so description still fits.
            label_col = max(8, (width - (len(indicator) if overflow else 0)) // 2)
            if len(label) > label_col:
                label = label[: label_col - 1] + "…"

            line = label.ljust(label_col)
            if item.description:
                remaining = width - visible_width(line) - (len(indicator) if (overflow and is_selected) else 0)
                desc = item.description
                if visible_width(desc) > remaining:
                    desc = desc[: max(0, remaining - 1)] + "…"
                line += desc

            # Truncate to width, then pad to width.
            line = _truncate_to_width(line, width - (len(indicator) if (overflow and is_selected) else 0))
            line = line.ljust(width - (len(indicator) if (overflow and is_selected) else 0))
            if overflow and is_selected:
                line = line + indicator

            if is_selected:
                line = f"\x1b[7m{line}\x1b[0m"

            lines.append(line)

        return lines


def _truncate_to_width(text: str, width: int) -> str:
    """Best-effort truncate so ``visible_width(result) <= width``."""
    if visible_width(text) <= width:
        return text
    out = ""
    for ch in text:
        # ANSI escape sequences (like the reverse-video marker) have width 0;
        # visible_width accounts for them, so appending is safe.
        candidate = out + ch
        if visible_width(candidate) > width:
            break
        out = candidate
    return out
