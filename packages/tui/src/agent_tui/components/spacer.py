"""Spacer component.

Renders N empty lines. Used for vertical spacing between message blocks.
"""
from __future__ import annotations

from agent_tui.tui import Component


class Spacer(Component):
    """Renders ``lines`` empty lines.

    ``set_lines`` updates the count; ``invalidate`` is a no-op (no cache).
    """

    def __init__(self, lines: int = 1) -> None:
        self._lines = lines

    def set_lines(self, lines: int) -> None:
        self._lines = lines

    def invalidate(self) -> None:
        pass  # No cached state.

    def render(self, width: int) -> list[str]:
        return [""] * self._lines
