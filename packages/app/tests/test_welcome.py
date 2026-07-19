"""Tests for the responsive interactive welcome card."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_tui import load_theme
from agent_tui.utils import visible_width
from coding_agent.modes.interactive.components.welcome import WelcomeComponent


def _component() -> WelcomeComponent:
    session = SimpleNamespace(
        model=SimpleNamespace(id="deepseek-v4-pro"),
        thinking_level="medium",
        tools=[SimpleNamespace(name=name) for name in ("read", "edit", "bash")],
    )
    return WelcomeComponent(session, load_theme("dark"), "0.1.0")


@pytest.mark.parametrize("width", [24, 35, 36, 48, 58, 80, 120])
def test_welcome_never_exceeds_terminal_width(width: int) -> None:
    lines = _component().render(width)
    assert lines
    assert all(visible_width(line) == width for line in lines)


def test_wide_welcome_surfaces_runtime_context_and_shortcuts() -> None:
    rendered = "\n".join(_component().render(80))
    assert "CODING AGENT" in rendered
    assert "deepseek-v4-pro" in rendered
    assert "medium" in rendered
    assert "TOOLS" in rendered
    assert "/help" in rendered
    assert "/model" in rendered


def test_narrow_welcome_uses_compact_layout() -> None:
    rendered = "\n".join(_component().render(30))
    assert "CODING AGENT" in rendered
    assert "deepseek-v4-pro" in rendered
    assert "/help" in rendered
    assert "╭" not in rendered
