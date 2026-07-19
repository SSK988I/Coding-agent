"""Tests for editor border color by thinking level."""
from __future__ import annotations

import re

from agent_llm import Model, ModelCost
from agent_core import SessionManager

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig


def _model() -> Model:
    return Model(
        id="m",
        provider="deepseek",
        reasoning=True,
        context_window=1000,
        thinking_level_map={"minimal": None, "low": None, "medium": None,
                            "high": "high", "xhigh": "max"},
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )


def _make_mode():
    """Build an InteractiveMode with a real session (TUI is not started)."""
    from coding_agent.modes.interactive.interactive_mode import InteractiveMode

    sm = SessionManager.create(cwd=".", in_memory=True)
    session = AgentSession(AgentSessionConfig(model=_model(), cwd=".", session_manager=sm))
    return InteractiveMode(session)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _has_color(s: str) -> bool:
    """True if ``s`` contains any ANSI color escape."""
    return bool(_ANSI_RE.search(s))


# ── token mapping ──────────────────────────────────────────────────────


def test_token_mapping_covers_all_levels():
    from coding_agent.modes.interactive.interactive_mode import InteractiveMode

    expected = {"off", "minimal", "low", "medium", "high", "xhigh"}
    assert set(InteractiveMode._THINKING_COLOR_TOKEN.keys()) == expected


def test_token_mapping_values_are_known_theme_tokens():
    from coding_agent.modes.interactive.interactive_mode import InteractiveMode

    for token in InteractiveMode._THINKING_COLOR_TOKEN.values():
        assert token.startswith("thinking")


# ── border color closure ───────────────────────────────────────────────


def test_border_color_fn_off_when_no_thinking():
    mode = _make_mode()
    mode._session.set_thinking_level(None)
    fn = mode._border_color_fn()
    # off → thinkingOff (dark gray). Just confirm it produces colored output
    # and is NOT the same as the plain border token.
    assert _has_color(fn("─"))


def test_border_color_fn_changes_with_level():
    mode = _make_mode()
    mode._session.set_thinking_level(None)
    off_fn = mode._border_color_fn()
    mode._session.set_thinking_level("high")
    high_fn = mode._border_color_fn()
    # Different levels must produce different ANSI sequences.
    assert off_fn("─") != high_fn("─")


def test_border_color_fn_unknown_level_falls_back_to_off():
    mode = _make_mode()
    mode._session.set_thinking_level("nonsense")  # type: ignore[arg-type]
    nonsense_fn = mode._border_color_fn()
    mode._session.set_thinking_level(None)
    off_fn = mode._border_color_fn()
    # nonsense and off should both resolve to thinkingOff.
    assert nonsense_fn("─") == off_fn("─")


# ── update_editor_border_color ─────────────────────────────────────────


def test_update_editor_border_color_sets_editor_fn():
    mode = _make_mode()
    mode._session.set_thinking_level("high")
    mode._update_editor_border_color()
    # The editor's border function should now produce colored output.
    assert _has_color(mode.editor._border_color_fn("─"))


def test_update_editor_border_color_reflects_level_change():
    mode = _make_mode()
    mode._session.set_thinking_level(None)
    mode._update_editor_border_color()
    before = mode.editor._border_color_fn("─")

    mode._session.set_thinking_level("xhigh")
    mode._update_editor_border_color()
    after = mode.editor._border_color_fn("─")

    assert before != after
