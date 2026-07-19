"""Box component.

A container that applies padding and an optional background color function to
all its children. Caches rendered output by (width, child_lines, bg_sample)
so unchanged subtrees skip re-rendering.

"""
from __future__ import annotations

from typing import Callable

from agent_tui.tui import Component
from agent_tui.utils import apply_background_to_line, visible_width


class _RenderCache:
    __slots__ = ("child_lines", "width", "bg_sample", "lines")

    def __init__(
        self,
        child_lines: list[str],
        width: int,
        bg_sample: "str | None",
        lines: list[str],
    ) -> None:
        self.child_lines = child_lines
        self.width = width
        self.bg_sample = bg_sample
        self.lines = lines


class Box(Component):
    """Container with padding and optional background.

    Args:
        padding_x: Left/right padding (default 1).
        padding_y: Top/bottom padding (default 1).
        bg_fn: Optional ``(text: str) -> str`` background wrapper.
    """

    def __init__(
        self,
        padding_x: int = 1,
        padding_y: int = 1,
        bg_fn: "Callable[[str], str] | None" = None,
    ) -> None:
        self.children: list[Component] = []
        self._padding_x = padding_x
        self._padding_y = padding_y
        self._bg_fn = bg_fn
        self._cache: "_RenderCache | None" = None

    def add_child(self, component: Component) -> None:
        self.children.append(component)
        self._cache = None

    def remove_child(self, component: Component) -> None:
        if component in self.children:
            self.children.remove(component)
            self._cache = None

    def clear(self) -> None:
        self.children.clear()
        self._cache = None

    def set_bg_fn(self, bg_fn: "Callable[[str], str] | None") -> None:
        self._bg_fn = bg_fn
        # Don't invalidate: we detect bg_fn changes by sampling output.

    def invalidate(self) -> None:
        self._cache = None
        for child in self.children:
            child.invalidate()

    def _match_cache(
        self, width: int, child_lines: list[str], bg_sample: "str | None"
    ) -> bool:
        cache = self._cache
        return (
            cache is not None
            and cache.width == width
            and cache.bg_sample == bg_sample
            and len(cache.child_lines) == len(child_lines)
            and all(a == b for a, b in zip(cache.child_lines, child_lines))
        )

    def render(self, width: int) -> list[str]:
        if not self.children:
            return []

        content_width = max(1, width - self._padding_x * 2)
        left_pad = " " * self._padding_x

        # Render all children.
        child_lines: list[str] = []
        for child in self.children:
            for line in child.render(content_width):
                child_lines.append(left_pad + line)

        if not child_lines:
            return []

        # Sample bg_fn to detect changes.
        bg_sample = self._bg_fn("test") if self._bg_fn else None

        # Cache check.
        if self._match_cache(width, child_lines, bg_sample):
            return self._cache.lines  # type: ignore[union-attr]

        # Apply padding + background.
        result: list[str] = []
        for _ in range(self._padding_y):
            result.append(self._apply_bg("", width))
        for line in child_lines:
            result.append(self._apply_bg(line, width))
        for _ in range(self._padding_y):
            result.append(self._apply_bg("", width))

        self._cache = _RenderCache(child_lines, width, bg_sample, result)
        return result

    def _apply_bg(self, line: str, width: int) -> str:
        """Pad line to full width and apply background."""
        vis_len = visible_width(line)
        pad_needed = max(0, width - vis_len)
        padded = line + " " * pad_needed
        if self._bg_fn:
            return apply_background_to_line(padded, width, self._bg_fn)
        return padded
