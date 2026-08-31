"""Focused selector for one structured Plan Mode question."""
from __future__ import annotations

from typing import Callable

from agent_tui.components.select_list import SelectItem, SelectList
from agent_tui.keys import matches_key
from agent_tui.theme import Theme
from agent_tui.utils import truncate_to_width

from coding_agent.core.plan_mode import PlanQuestion
from coding_agent.modes.interactive.components.text_input import TextInput


class PlanQuestionComponent:
    def __init__(
        self, theme: Theme, question: PlanQuestion,
        on_answer: Callable[[str], None], on_cancel: Callable[[], None],
    ) -> None:
        self._theme = theme
        self._question = question
        self._on_answer = on_answer
        self._on_cancel = on_cancel
        self._custom = False
        self._input = TextInput()
        self._list = SelectList(max_visible=4)
        items = [
            SelectItem(value=option.label, label=option.label, description=option.description)
            for option in question.options
        ]
        if question.allow_custom:
            items.append(SelectItem(value="__custom__", label="自定义回答", description="输入其他答案"))
        self._list.set_items(items)
        self.focused = True

    def handle_input(self, data: str) -> bool:
        if matches_key(data, "escape") or matches_key(data, "ctrl+c"):
            self._on_cancel()
            return True
        if self._custom:
            if matches_key(data, "enter"):
                answer = self._input.value.strip()
                if answer:
                    self._on_answer(answer)
                return True
            return self._input.handle_input(data)
        if matches_key(data, "up"):
            self._list.move_up()
            return True
        if matches_key(data, "down"):
            self._list.move_down()
            return True
        if matches_key(data, "enter"):
            selected = self._list.get_selected()
            if selected is not None:
                if selected.value == "__custom__":
                    self._custom = True
                else:
                    self._on_answer(selected.value)
            return True
        return False

    def render(self, width: int) -> list[str]:
        border = self._theme.fg("warning", "─" * width)
        content_width = max(1, width - 4)
        lines = [border, f"  PLAN · {self._question.header}"]
        lines.append("  " + truncate_to_width(self._question.question, content_width, ellipsis="…"))
        if self._custom:
            lines.append("  自定义回答（Enter 提交，Esc 稍后回答）")
            lines.append("  " + self._input.render(content_width))
        else:
            lines.extend("  " + row for row in self._list.render(content_width))
            lines.append("  ↑↓ 选择 · Enter 回答 · Esc 停止并保留问题")
        lines.append(border)
        return lines
