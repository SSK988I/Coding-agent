"""Keyboard-driven permission selection and individual approval dialogs."""
from __future__ import annotations

from typing import Callable

from agent_tui.components.select_list import SelectItem, SelectList
from agent_tui.keys import matches_key
from agent_tui.theme import Theme
from agent_tui.utils import truncate_to_width


class PermissionMenuComponent:
    def __init__(
        self, theme: Theme, title: str, description: str,
        items: list[SelectItem], on_select: Callable[[str], None], on_close: Callable[[], None],
        selected: str | None = None,
    ) -> None:
        self.focused = True
        self._theme = theme
        self._title = title
        self._description = description
        self._on_select = on_select
        self._on_close = on_close
        self._list = SelectList(max_visible=len(items))
        self._list.set_items(items)
        for index, item in enumerate(items):
            if item.value == selected:
                for _ in range(index):
                    self._list.move_down()
                break

    def handle_input(self, data: str) -> bool:
        if matches_key(data, "escape") or matches_key(data, "ctrl+c"):
            self._on_close()
        elif matches_key(data, "up"):
            self._list.move_up()
        elif matches_key(data, "down"):
            self._list.move_down()
        elif matches_key(data, "enter"):
            item = self._list.get_selected()
            if item is not None:
                self._on_select(item.value)
        else:
            return False
        return True

    def render(self, width: int) -> list[str]:
        width = max(1, width)
        content_width = max(1, width - 2)
        rows = [self._title, *self._description.splitlines(), *self._list.render(content_width),
                "↑↓ 选择 · Enter 确认 · Esc 返回"]
        return [self._theme.fg("warning", "─" * width),
                *(truncate_to_width("  " + row, width, ellipsis="…") for row in rows),
                self._theme.fg("warning", "─" * width)]
