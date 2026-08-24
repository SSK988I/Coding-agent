"""Focused actions for the latest immutable Plan revision."""
from __future__ import annotations

from typing import Callable

from agent_tui.components.select_list import SelectItem, SelectList
from agent_tui.keys import matches_key
from agent_tui.theme import Theme

from coding_agent.core.plan_mode import PlanRevision


class PlanActionsComponent:
    def __init__(
        self, theme: Theme, plan: PlanRevision,
        on_action: Callable[[str], None], on_close: Callable[[], None],
    ) -> None:
        self._theme = theme
        self._plan = plan
        self._on_action = on_action
        self._on_close = on_close
        self._list = SelectList(max_visible=3)
        self._list.set_items([
            SelectItem(value="supplement", label="补充想法", description="保留 Plan Mode，在输入框补充修改要求"),
            SelectItem(value="execute", label="执行方案", description="确认当前 revision 并立即切回 Default 执行"),
            SelectItem(value="cancel", label="取消规划", description="切回 Default，不执行计划"),
        ])
        self.focused = True

    def handle_input(self, data: str) -> bool:
        if matches_key(data, "escape") or matches_key(data, "ctrl+c"):
            self._on_close()
            return True
        if matches_key(data, "up"):
            self._list.move_up()
            return True
        if matches_key(data, "down"):
            self._list.move_down()
            return True
        if matches_key(data, "enter"):
            selected = self._list.get_selected()
            if selected is not None:
                self._on_action(selected.value)
            return True
        return False

    def render(self, width: int) -> list[str]:
        border = self._theme.fg("warning", "─" * width)
        content_width = max(1, width - 4)
        lines = [
            border,
            f"  PLAN ready · revision {self._plan.revision} · {self._plan.digest[:12]}",
        ]
        lines.extend("  " + row for row in self._list.render(content_width))
        lines.append("  ↑↓ 选择 · Enter 确认 · Esc 关闭操作栏")
        lines.append(border)
        return lines
