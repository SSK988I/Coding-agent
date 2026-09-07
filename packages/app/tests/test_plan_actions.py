from __future__ import annotations

from agent_tui import load_theme

from coding_agent.core.plan_mode import PlanRevision
from coding_agent.modes.interactive.components.plan_actions import (
    PlanActionsComponent,
    PlanModeMenuComponent,
)


def test_ready_plan_offers_execute_or_supplement_choices() -> None:
    plan = PlanRevision(
        plan_id="plan-1",
        revision=2,
        title="Plan title",
        markdown="# Plan title",
        digest="abcdef0123456789",
        source_message_id="message-2",
    )
    component = PlanActionsComponent(load_theme("dark"), plan, lambda _: None, lambda: None)

    rendered = "\n".join(component.render(100))

    assert "当前会话执行" in rendered
    assert "新会话复核" in rendered
    assert "补充想法" in rendered
    assert "继续规划" not in rendered


def test_plan_phase_menus_keep_an_explicit_exit() -> None:
    theme = load_theme("dark")
    expectations = {
        "drafting": ("继续规划", "整理并提交", "取消规划"),
        "awaiting_answer": ("回答待处理问题", "取消规划"),
        "executing": ("查看运行状态", "停止执行"),
        "uncertain": ("查看恢复详情", "重新规划", "取消规划"),
        "recovery_error": ("查看恢复错误", "重新规划", "取消规划"),
    }

    for phase, labels in expectations.items():
        component = PlanModeMenuComponent(theme, phase, lambda _: None, lambda: None)
        rendered = "\n".join(component.render(100))
        for label in labels:
            assert label in rendered
