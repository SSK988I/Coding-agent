from __future__ import annotations

from agent_tui import load_theme

from coding_agent.core.plan_mode import PlanRevision
from coding_agent.modes.interactive.components.plan_actions import PlanActionsComponent


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

    assert "执行方案" in rendered
    assert "补充想法" in rendered
    assert "继续规划" not in rendered
