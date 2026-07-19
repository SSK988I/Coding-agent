"""Tests for TextInput."""
from __future__ import annotations

import re

from coding_agent.modes.interactive.components.text_input import TextInput


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip(s: str) -> str:
    return _ANSI_RE.sub("", s)


# ── value ──────────────────────────────────────────────────────────────


def test_initial_value():
    ti = TextInput(initial="hello")
    assert ti.value == "hello"


def test_set_value():
    ti = TextInput()
    ti.set_value("abc")
    assert ti.value == "abc"


# ── editing ────────────────────────────────────────────────────────────


def test_printable_appends():
    ti = TextInput()
    for ch in "abc":
        assert ti.handle_input(ch) is True
    assert ti.value == "abc"


def test_backspace_removes_last():
    ti = TextInput(initial="hello")
    assert ti.handle_input("\x7f") is True  # backspace
    assert ti.value == "hell"


def test_backspace_on_empty_is_noop():
    ti = TextInput()
    assert ti.handle_input("\x7f") is True
    assert ti.value == ""


def test_unrecognized_key_returns_false():
    ti = TextInput()
    # An escape sequence that isn't a named key (e.g. a function key the
    # TextInput doesn't handle) should not be consumed.
    assert ti.handle_input("\x1b[11~") is False  # F1
    assert ti.value == ""


# ── multi-char / paste ───────────────────────────────────────────────


def test_multichar_printable_run_appends():
    """A coalesced multi-char run (fast typing, or non-bracketed paste)
    should append every character, not be silently dropped."""
    ti = TextInput()
    assert ti.handle_input("sk-test-123") is True
    assert ti.value == "sk-test-123"


def test_bracketed_paste_appends_inner_text():
    """Bracketed-paste payload (``\\x1b[200~<text>\\x1b[201~``) must be
    unwrapped and the inner text appended — this is how pasted API keys
    arrive when bracketed paste is enabled (which it is in terminal.py)."""
    ti = TextInput()
    payload = "\x1b[200~sk-pasted-key-12345\x1b[201~"
    assert ti.handle_input(payload) is True
    assert ti.value == "sk-pasted-key-12345"


def test_bracketed_paste_missing_end_marker_still_handled():
    """Some terminals split the payload across reads; the start marker may
    arrive without the end marker. We accept that prefix and keep the inner
    text."""
    ti = TextInput()
    payload = "\x1b[200~partial-key"
    assert ti.handle_input(payload) is True
    assert ti.value == "partial-key"


def test_paste_with_trailing_newline_strips_newline():
    """Pasting ``"key\\n"`` should not store the newline (single-line field)."""
    ti = TextInput()
    payload = "\x1b[200~key-with-newline\n\x1b[201~"
    assert ti.handle_input(payload) is True
    assert ti.value == "key-with-newline"


def test_paste_with_crlf_keeps_first_line_only():
    ti = TextInput()
    payload = "\x1b[200~first\r\nsecond\x1b[201~"
    assert ti.handle_input(payload) is True
    assert ti.value == "first"


def test_escape_sequence_with_control_char_not_consumed():
    """A run that contains a control character (other than bracketed paste)
    is probably an escape sequence we don't handle — should NOT be appended
    verbatim and should report not-consumed."""
    ti = TextInput()
    assert ti.handle_input("a\x1bb") is False  # ESC in the middle
    assert ti.value == ""


def test_paste_after_typing_appends():
    ti = TextInput(initial="sk-")
    payload = "\x1b[200~rest-of-key\x1b[201~"
    assert ti.handle_input(payload) is True
    assert ti.value == "sk-rest-of-key"


def test_bracketed_paste_empty_inner_is_consumed():
    """A paste of empty text shouldn't break the input (no-op append)."""
    ti = TextInput(initial="x")
    payload = "\x1b[200~\x1b[201~"
    assert ti.handle_input(payload) is True
    assert ti.value == "x"


# ── submit / cancel ────────────────────────────────────────────────────


def test_enter_fires_on_submit_with_value():
    submitted = []
    ti = TextInput(initial="key123", on_submit=submitted.append)
    assert ti.handle_input("\r") is True  # enter
    assert submitted == ["key123"]


def test_enter_with_no_callback_still_consumed():
    ti = TextInput(initial="x")
    assert ti.handle_input("\r") is True


def test_escape_fires_on_cancel():
    cancelled = []
    ti = TextInput(on_cancel=lambda: cancelled.append(True))
    assert ti.handle_input("\x1b") is True
    assert cancelled == [True]


def test_ctrl_c_fires_on_cancel():
    cancelled = []
    ti = TextInput(on_cancel=lambda: cancelled.append(True))
    assert ti.handle_input("\x03") is True
    assert cancelled == [True]


def test_cancel_with_no_callback_still_consumed():
    ti = TextInput()
    assert ti.handle_input("\x1b") is True


# ── render ─────────────────────────────────────────────────────────────


def test_render_has_prompt_and_cursor():
    ti = TextInput(initial="hello")
    line = _strip(ti.render(40))
    assert line.startswith("> hello")
    # cursor block (inverse-video space) becomes a literal space after strip
    assert line.endswith(" ")


def test_render_shows_placeholder_when_empty():
    ti = TextInput(placeholder="enter key...")
    line = _strip(ti.render(40))
    assert "enter key..." in line
    assert line.startswith("> ")


def test_render_shows_value_over_placeholder():
    ti = TextInput(initial="real", placeholder="placeholder")
    line = _strip(ti.render(40))
    assert "real" in line
    assert "placeholder" not in line


def test_render_truncates_long_value():
    ti = TextInput(initial="x" * 100)
    line = ti.render(20)
    assert _strip(line).startswith("> ")
    # Should fit within 20 visible columns (cursor block has width 1).
    from agent_tui.utils import visible_width

    assert visible_width(line) <= 20


def test_render_pads_to_width():
    ti = TextInput(initial="hi")
    line = ti.render(40)
    from agent_tui.utils import visible_width

    assert visible_width(line) == 40


def test_render_zero_width_safe():
    ti = TextInput(initial="abc")
    # Should not crash on absurdly small widths.
    line = ti.render(0)
    assert isinstance(line, str)
