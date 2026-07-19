"""Tests for core/keybindings.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coding_agent.core.keybindings import (
    DEFAULT_APP_KEYBINDINGS,
    DEFAULT_EDITOR_KEYBINDINGS,
    all_hotkeys,
    get_keybinding,
    key_text,
    _pretty,
)


# ─── get_keybinding ───────────────────────────────────────────────────


def test_app_interrupt_is_escape():
    assert get_keybinding("app.interrupt") == "escape"


def test_app_clear_is_ctrl_c():
    assert get_keybinding("app.clear") == "ctrl+c"


def test_editor_submit_is_enter():
    assert get_keybinding("editor.submit") == "enter"


def test_unknown_action_raises():
    try:
        get_keybinding("app.nonexistent")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_unbound_action_returns_empty_list():
    # app.session.new intentionally has no default key.
    # Our reduced table doesn't include it; verify suspend is [] on win32.
    if sys.platform == "win32":
        assert get_keybinding("app.suspend") == []
    else:
        assert get_keybinding("app.suspend") == "ctrl+z"


# ─── key_text (pretty rendering) ──────────────────────────────────────


def test_key_text_interrupt_is_esc():
    assert key_text("app.interrupt") == "Esc"


def test_key_text_clear_is_ctrl_c():
    assert key_text("app.clear") == "Ctrl+C"


def test_pretty_single_keys():
    assert _pretty("escape") == "Esc"
    assert _pretty("enter") == "Enter"
    assert _pretty("up") == "Up"


def test_pretty_modified_keys():
    assert _pretty("ctrl+c") == "Ctrl+C"
    assert _pretty("shift+tab") == "Shift+Tab"
    assert _pretty("alt+v") == "Alt+V"


def test_pretty_chained_modifiers():
    assert _pretty("shift+ctrl+p") == "Shift+Ctrl+P"


# ─── all_hotkeys ──────────────────────────────────────────────────────


def test_all_hotkeys_includes_app_and_editor():
    rows = all_hotkeys()
    categories = {r[0] for r in rows}
    assert "App" in categories
    assert "Editor" in categories


def test_all_hotkeys_rows_well_formed():
    for category, keys, desc in all_hotkeys():
        assert category in ("App", "Editor")
        assert isinstance(keys, str)
        assert isinstance(desc, str) and desc


def test_all_hotkeys_contains_interrupt_and_clear():
    rows = all_hotkeys()
    descs = {r[2] for r in rows}
    assert any("abort" in d.lower() or "interrupt" in d.lower() for d in descs)
    assert any("clear editor" in d.lower() for d in descs)


# ─── no hardcoded duplicates ──────────────────────────────────────────


def test_no_duplicate_actions_between_tables():
    app_actions = set(DEFAULT_APP_KEYBINDINGS)
    editor_actions = set(DEFAULT_EDITOR_KEYBINDINGS)
    assert app_actions.isdisjoint(editor_actions)
