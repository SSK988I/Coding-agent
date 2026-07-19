"""Tests for LoginDialogComponent color differentiation.

The component draws:
  - top/bottom borders in the static ``border`` token (blue), NOT the
    thinking-level color
  - a ``Login to <Provider>`` title line in ``accent`` + ``bold``
  - the hint text in ``warning`` (yellow)

These tests assert that color scheme so the dialog is visually distinct
from the editor (which uses thinking-level colors) and from the model
selector (which is the same blue border but no title/warning hint).
"""
from __future__ import annotations

import re

from coding_agent.modes.interactive.components.login_dialog import (
    LoginDialogComponent,
)
from agent_tui.theme import load_theme

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _make(provider="DeepSeek", **kw):
    submitted, cancelled = [], []
    dlg = LoginDialogComponent(
        load_theme("dark"),
        provider,
        submitted.append,
        lambda: cancelled.append(True),
        **kw,
    )
    return dlg, submitted, cancelled


# ── title line ────────────────────────────────────────────────────────


def test_title_line_present_and_accent_bold():
    dlg, _, _ = _make()
    lines = dlg.render(80)
    # Title is the second line (after top border).
    title_raw = lines[1]
    title = _strip(title_raw)
    assert "Login to DeepSeek" in title
    # accent (color) + bold (\x1b[1m) codes are present in the raw line.
    assert "\x1b[1m" in title_raw  # bold
    # accent resolves to a foreground color code (e.g. \x1b[38;...m).
    assert "\x1b[38;" in title_raw or "\x1b[3" in title_raw


def test_title_uses_provider_name():
    dlg, _, _ = _make(provider="OpenAI")
    title = _strip(dlg.render(80)[1])
    assert "Login to OpenAI" in title
    assert "DeepSeek" not in title


# ── hint in warning color ─────────────────────────────────────────────


def test_hint_uses_warning_color():
    """The hint text must be wrapped in the warning (yellow) token."""
    theme = load_theme("dark")
    dlg, _, _ = _make()
    lines = dlg.render(80)
    # Hint is the third line.
    hint_raw = lines[2]
    # theme.fg(token, "") emits "<open>\x1b[39m" (open then immediate reset
    # for empty text). The real rendered line keeps the open code and
    # defers the reset, so we match just the color-open prefix.
    expected_open = theme.fg("warning", "").split("\x1b[39m")[0]
    assert expected_open in hint_raw


def test_hint_text_content():
    dlg, _, _ = _make()
    hint = _strip(dlg.render(80)[2])
    assert "Enter" in hint
    assert "Esc" in hint


# ── border uses static "border" token (blue), not thinking ───────────


def test_border_uses_static_border_token():
    """The dialog border must use the ``border`` (blue) token by default,
    NOT a thinking-level token. This is what visually separates the dialog
    from the editor (whose border follows thinking level)."""
    theme = load_theme("dark")
    dlg, _, _ = _make()
    lines = dlg.render(40)
    # theme.fg(token, "") emits "<open>\x1b[39m". The real border keeps the
    # <open> prefix and defers the reset, so match just the color-open code.
    border_open = theme.fg("border", "").split("\x1b[39m")[0]
    assert lines[0].startswith(border_open)
    assert lines[-1].startswith(border_open)


def test_border_does_not_use_thinking_token():
    """Sanity check: the border color is NOT any of the thinking* tokens
    that the editor uses."""
    theme = load_theme("dark")
    dlg, _, _ = _make()
    border_line = dlg.render(40)[0]
    thinking_tokens = [
        "thinkingOff", "thinkingMinimal", "thinkingLow", "thinkingMedium",
        "thinkingHigh", "thinkingXhigh",
    ]
    for tok in thinking_tokens:
        thinking_open = theme.fg(tok, "").split("\x1b[39m")[0]
        assert not border_line.startswith(thinking_open), (
            f"border unexpectedly used thinking token {tok}"
        )


# ── caller can still override border color ────────────────────────────


def test_border_color_fn_override_still_works():
    """Explicit border_color_fn takes precedence over the default blue."""
    dlg, _, _ = _make(border_color_fn=lambda s: f"[{s}]")
    lines = dlg.render(20)
    assert lines[0].startswith("[")
    assert lines[-1].startswith("[")
