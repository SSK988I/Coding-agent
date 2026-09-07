"""Focused, state-aware actions for Plan Mode."""
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
        self._list = SelectList(max_visible=4)
        self._list.set_items([
            SelectItem(value="supplement", label="补充想法", description="保留 Plan Mode，在输入框补充修改要求"),
            SelectItem(value="execute", label="当前会话执行", description="确认当前 revision 并立即切回 Default 执行"),
            SelectItem(value="handoff", label="新会话复核", description="仅交接已确认方案，在干净会话中再次确认"),
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


class PlanModeMenuComponent:
    """Small action menu used by a bare ``/plan`` in non-ready phases."""

    _PHASE_ITEMS: dict[str, tuple[tuple[str, str, str], ...]] = {
        "drafting": (
            ("continue", "继续规划", "返回输入框，继续补充需求或约束"),
            ("submit", "整理并提交", "让 Agent 将现有讨论整理为可复核方案"),
            ("cancel", "取消规划", "切回 Default，不执行任何方案"),
        ),
        "awaiting_answer": (
            ("answer", "回答待处理问题", "重新显示尚未回答的结构化问题"),
            ("cancel", "取消规划", "放弃问题并切回 Default"),
        ),
        "executing": (
            ("details", "查看运行状态", "显示当前执行阶段与 planId"),
            ("stop", "停止执行", "请求中止当前执行回合"),
        ),
        "uncertain": (
            ("details", "查看恢复详情", "检查无法确认终态的执行记录"),
            ("replan", "重新规划", "开始新的 Plan Episode，不自动重试"),
            ("cancel", "取消规划", "退出当前 Plan 状态"),
        ),
        "recovery_error": (
            ("details", "查看恢复错误", "显示持久化状态校验失败原因"),
            ("replan", "重新规划", "保留历史并开始新的 Plan Episode"),
            ("cancel", "取消规划", "退出当前 Plan 状态"),
        ),
    }

    def __init__(
        self,
        theme: Theme,
        phase: str,
        on_action: Callable[[str], None],
        on_close: Callable[[], None],
    ) -> None:
        if phase not in self._PHASE_ITEMS:
            raise ValueError(f"unsupported Plan phase: {phase}")
        self._theme = theme
        self._phase = phase
        self._on_action = on_action
        self._on_close = on_close
        items = self._PHASE_ITEMS[phase]
        self._list = SelectList(max_visible=len(items))
        self._list.set_items([
            SelectItem(value=value, label=label, description=description)
            for value, label, description in items
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
        lines = [border, f"  PLAN · {self._phase}"]
        lines.extend("  " + row for row in self._list.render(content_width))
        lines.append("  ↑↓ 选择 · Enter 确认 · Esc 返回")
        lines.append(border)
        return lines
