"""Tests for Ctrl+D (app.exit) and editor forward-delete behavior.

Expected behavior:
  - Ctrl+C (app.clear): first tap clears editor, second tap within 500ms quits.
  - Ctrl+D (app.exit): when the editor is EMPTY, exits immediately (no
    double-tap). When the editor has text, Ctrl+D falls through to the editor
    which forward-deletes one character.

This covers the second half (Ctrl+D), which was previously defined in
keybindings.py but never wired into interactive_mode.py.
"""
from __future__ import annotations

from types import SimpleNamespace

from agent_tui.components.editor import Editor


# ── editor: Ctrl+D forward-deletes ──


def _editor_with(text: str) -> Editor:
    e = Editor()
    e.set_text(text)
    e.disable_submit = True
    return e


def test_ctrl_d_forward_deletes_one_char():
    """Ctrl+D on a non-empty editor forward-deletes one character."""
    e = _editor_with("abc")
    assert e.handle_input("\x04") is True  # ctrl+d
    assert e.get_text() == "bc"


def test_ctrl_d_at_cursor_mid_line():
    """Forward-delete removes the char to the right of the cursor."""
    e = _editor_with("abc")
    # Move cursor to position 1 (between 'a' and 'b').
    e.set_text_and_cursor("abc", 0, 1)
    e.disable_submit = True
    assert e.handle_input("\x04") is True
    assert e.get_text() == "ac"


def test_ctrl_d_at_end_of_line_is_noop():
    """Forward-delete at the end of the line changes nothing but is consumed."""
    e = _editor_with("abc")
    e.set_text_and_cursor("abc", 0, 3)  # cursor at end
    e.disable_submit = True
    assert e.handle_input("\x04") is True
    assert e.get_text() == "abc"  # unchanged


def test_ctrl_d_on_empty_editor_consumed_but_no_change():
    """Ctrl+D on an empty editor is consumed by forward-delete (no-op), but
    the app.exit LISTENER (tested below) intercepts before the editor sees
    it — so this just verifies the editor itself doesn't crash on empty."""
    e = _editor_with("")
    assert e.handle_input("\x04") is True
    assert e.get_text() == ""


# ── app.exit listener: empty editor → exit, non-empty → fall through ──


class _CapturingTUI:
    """Minimal TUI stub that captures registered input listeners."""

    def __init__(self):
        self._loop = None
        self.listeners = []

    def request_render(self):
        pass

    def add_input_listener(self, fn):
        self.listeners.append(fn)


def _make_mode_with_listeners():
    """Build a minimal InteractiveMode with the input listeners registered.

    We call only the listener-registration portion of run(), bypassing the
    full TUI/agent setup. The listeners close over ``self``, so we need a
    real-ish InteractiveMode instance with the attributes they touch:
    ``editor``, ``_last_sigint_time``, ``_running``, ``_clear_editor``,
    ``_add_system_message``, ``_cycle_thinking``, ``_current_selector``.
    """
    from agent_tui import load_theme
    from coding_agent.modes.interactive.interactive_mode import InteractiveMode

    obj = SimpleNamespace()
    obj.theme = load_theme("dark")
    obj.tui = _CapturingTUI()
    obj.editor = Editor()
    obj.editor.disable_submit = True
    obj._last_sigint_time = 0.0
    obj._running = True
    obj._current_selector = None
    obj._cycle_thinking = lambda: None
    messages = []
    obj._add_system_message = lambda msg: messages.append(msg)
    obj.messages = messages

    def _clear_editor():
        obj.editor.set_text("")

    obj._clear_editor = _clear_editor

    # Bind the real _register_input_listeners if it exists, else inline.
    if hasattr(InteractiveMode, "_register_input_listeners"):
        InteractiveMode._register_input_listeners.__get__(obj, SimpleNamespace)()
    else:
        # Fall back: call the registration block from run() directly.
        raise RuntimeError(
            "expected _register_input_listeners to be extracted for testing"
        )
    return obj


def test_app_exit_listener_empty_editor_quits(monkeypatch):
    """Ctrl+D on an empty editor sets _running = False (exit)."""
    obj = _make_mode_with_listeners()
    # Find the exit listener (the one that reacts to app.exit / ctrl+d).
    # Dispatch Ctrl+D through all listeners in order, mimicking _handle_input.
    data = "\x04"
    consumed = False
    for listener in obj.tui.listeners:
        result = listener(data)
        if result and result.get("consume"):
            consumed = True
            break
    assert consumed, "Ctrl+D on empty editor should be consumed by app.exit listener"
    assert obj._running is False, "Ctrl+D on empty editor should exit"
    assert any("再见" in m for m in obj.messages)


def test_app_exit_listener_non_empty_falls_through(monkeypatch):
    """Ctrl+D on a non-empty editor is NOT consumed by the app.exit listener —
    it falls through so the editor can forward-delete."""
    obj = _make_mode_with_listeners()
    obj.editor.set_text("hello")
    obj.editor.disable_submit = True

    data = "\x04"
    consumed = False
    for listener in obj.tui.listeners:
        result = listener(data)
        if result and result.get("consume"):
            consumed = True
            break
    # app.exit listener must NOT consume when editor has text.
    assert not consumed, "Ctrl+D on non-empty editor must fall through to editor"
    assert obj._running is True, "must not exit when editor has text"


def test_ctrl_c_listener_still_clears():
    """Sanity: the Ctrl+C listener (app.clear) still clears the editor on
    single tap and exits on double-tap. Ensures the new Ctrl+D listener
    didn't break the existing Ctrl+C path."""
    obj = _make_mode_with_listeners()
    obj.editor.set_text("some text")
    obj._last_sigint_time = 0.0  # ensure not within 500ms of "now"

    # Single Ctrl+C → clear editor, stay running.
    consumed = False
    for listener in obj.tui.listeners:
        result = listener("\x03")  # ctrl+c
        if result and result.get("consume"):
            consumed = True
            break
    assert consumed
    assert obj.editor.get_text() == ""
    assert obj._running is True

    # Immediate second Ctrl+C → exit.
    for listener in obj.tui.listeners:
        result = listener("\x03")
        if result and result.get("consume"):
            break
    assert obj._running is False
