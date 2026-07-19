"""Tests for LoginDialogComponent."""
from __future__ import annotations

import re

from coding_agent.modes.interactive.components.login_dialog import (
    LoginDialogComponent,
)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _theme():
    from agent_tui.theme import load_theme

    return load_theme("dark")


def _make(**kw):
    submitted, cancelled = [], []

    def on_submit(key):
        submitted.append(key)

    def on_cancel():
        cancelled.append(True)

    dlg = LoginDialogComponent(_theme(), "DeepSeek", on_submit, on_cancel, **kw)
    return dlg, submitted, cancelled


# ── render ─────────────────────────────────────────────────────────────


def test_render_has_top_and_bottom_borders():
    dlg, _, _ = _make()
    lines = dlg.render(60)
    assert _strip(lines[0]) == "─" * 60
    assert _strip(lines[-1]) == "─" * 60


def test_render_includes_provider_hint():
    dlg, _, _ = _make()
    body = "\n".join(_strip(line) for line in dlg.render(80))
    assert "DeepSeek" in body
    assert "API key" in body


def test_render_includes_input_prompt():
    dlg, _, _ = _make()
    body = "\n".join(_strip(line) for line in dlg.render(80))
    assert "> " in body


def test_render_has_five_lines():
    dlg, _, _ = _make()
    lines = dlg.render(60)
    # border, accent title, warning hint, input, border
    assert len(lines) == 5


# ── input ──────────────────────────────────────────────────────────────


def test_typing_updates_input_value():
    dlg, _, _ = _make()
    for ch in "sk-abc":
        dlg.handle_input(ch)
    assert dlg._input.value == "sk-abc"


def test_backspace_removes_char():
    dlg, _, _ = _make()
    for ch in "abc":
        dlg.handle_input(ch)
    dlg.handle_input("\x7f")  # backspace
    assert dlg._input.value == "ab"


def test_enter_fires_on_submit_with_typed_key():
    dlg, submitted, cancelled = _make()
    for ch in "sk-secret":
        dlg.handle_input(ch)
    dlg.handle_input("\r")  # enter
    assert submitted == ["sk-secret"]
    assert cancelled == []


def test_enter_on_empty_submits_empty_string():
    dlg, submitted, _ = _make()
    dlg.handle_input("\r")
    assert submitted == [""]


def test_escape_fires_on_cancel():
    dlg, submitted, cancelled = _make()
    dlg.handle_input("\x1b")
    assert cancelled == [True]
    assert submitted == []


def test_ctrl_c_fires_on_cancel():
    dlg, _, cancelled = _make()
    dlg.handle_input("\x03")
    assert cancelled == [True]


# ── border color injection ─────────────────────────────────────────────


def test_border_color_fn_used():
    seen = []
    dlg, _, _ = _make(border_color_fn=lambda s: (seen.append(s) or f"<{s}>"))
    lines = dlg.render(40)
    # The border color fn should have wrapped the border glyphs.
    assert any("<" in line for line in lines)
    assert seen  # was called


def test_default_border_color_uses_theme():
    dlg, _, _ = _make()
    lines = dlg.render(40)
    # theme.fg("border", ...) wraps in an ANSI escape.
    assert _ANSI_RE.search(lines[0]) is not None
