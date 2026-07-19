"""Tests for Windows KEY_EVENT_RECORD → VT sequence translation.

These guard the IME-safety fix: ``_read_loop_windows`` was switched from
``os.read(0)`` (which bypasses IME composition and lets pinyin letters
leak through during preedit) to ``ReadConsoleInputW`` (which delivers
IME-composed CJK characters only after composition completes).

The translation in ``ProcessTerminal._encode_key_event`` produces the
same VT sequences ``keys.py`` already matches, so these tests verify
that contract — they don't touch the real console. Each event is a
``SimpleNamespace`` shaped like a ``KEY_EVENT_RECORD``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_tui.terminal import (
    _ALT_PRESSED,
    _CTRL_PRESSED,
    _ENHANCED_KEY,
    _KEY_EVENT_TYPE,
    _LEFT_ALT_PRESSED,
    _SHIFT_PRESSED,
    _VK_BACK,
    _VK_DELETE,
    _VK_DOWN,
    _VK_END,
    _VK_ESCAPE,
    _VK_F1,
    _VK_F12,
    _VK_HOME,
    _VK_INSERT,
    _VK_LEFT,
    _VK_NEXT,
    _VK_PRIOR,
    _VK_RETURN,
    _VK_RIGHT,
    _VK_TAB,
    _VK_UP,
    ProcessTerminal,
)


def _ev(vk: int, ucs: str = "\x00", cks: int = 0, key_down: bool = True):
    """Build a minimal KEY_EVENT_RECORD-shaped object."""
    return SimpleNamespace(
        wVirtualKeyCode=vk,
        uChar=SimpleNamespace(UnicodeChar=ucs),
        dwControlKeyState=cks,
        bKeyDown=1 if key_down else 0,
    )


def _t():
    """A ProcessTerminal without starting it (we only call _encode_key_event)."""
    return ProcessTerminal()


# ─── Arrow keys ─────────────────────────────────────────────────────────


def test_arrow_keys_emit_csi_letter():
    t = _t()
    assert t._encode_key_event(_ev(_VK_LEFT)) == "\x1b[D"
    assert t._encode_key_event(_ev(_VK_UP)) == "\x1b[A"
    assert t._encode_key_event(_ev(_VK_RIGHT)) == "\x1b[C"
    assert t._encode_key_event(_ev(_VK_DOWN)) == "\x1b[B"


def test_shift_arrow_emits_parameterized_csi():
    """Shift+arrow → \\x1b[1;2X so the modifier survives (xterm convention)."""
    t = _t()
    assert t._encode_key_event(_ev(_VK_UP, cks=_SHIFT_PRESSED)) == "\x1b[1;2A"
    assert t._encode_key_event(_ev(_VK_DOWN, cks=_SHIFT_PRESSED)) == "\x1b[1;2B"


def test_ctrl_arrow_emits_5_modifier():
    t = _t()
    assert t._encode_key_event(_ev(_VK_RIGHT, cks=_CTRL_PRESSED)) == "\x1b[1;5C"
    assert t._encode_key_event(_ev(_VK_LEFT, cks=_CTRL_PRESSED)) == "\x1b[1;5D"


def test_alt_arrow_emits_3_modifier():
    t = _t()
    assert t._encode_key_event(_ev(_VK_UP, cks=_ALT_PRESSED)) == "\x1b[1;3A"


# ─── Function keys ──────────────────────────────────────────────────────


def test_f1_through_f4_use_ss3():
    t = _t()
    assert t._encode_key_event(_ev(_VK_F1)) == "\x1bOP"
    assert t._encode_key_event(_ev(_VK_F1 + 1)) == "\x1bOQ"   # F2
    assert t._encode_key_event(_ev(_VK_F1 + 2)) == "\x1bOR"   # F3
    assert t._encode_key_event(_ev(_VK_F1 + 3)) == "\x1bOS"   # F4


def test_f5_through_f12_use_csi_tilde():
    t = _t()
    assert t._encode_key_event(_ev(_VK_F1 + 4)) == "\x1b[15~"   # F5
    assert t._encode_key_event(_ev(_VK_F1 + 9)) == "\x1b[21~"   # F10
    assert t._encode_key_event(_ev(_VK_F12)) == "\x1b[24~"      # F12


# ─── Navigation keys ────────────────────────────────────────────────────


def test_home_end_on_numpad_vs_extended():
    """Home/End emit different sequences for Enhanced (cursor) vs numeric keys."""
    t = _t()
    # Extended (arrow pad / dedicated Home/End keys)
    assert t._encode_key_event(_ev(_VK_HOME, cks=_ENHANCED_KEY)) == "\x1b[H"
    assert t._encode_key_event(_ev(_VK_END, cks=_ENHANCED_KEY)) == "\x1b[F"
    # Non-enhanced (numpad with NumLock off) — historic rxvt convention
    assert t._encode_key_event(_ev(_VK_HOME)) == "\x1b[1~"
    assert t._encode_key_event(_ev(_VK_END)) == "\x1b[4~"


def test_insert_delete_pageup_pagedown():
    t = _t()
    assert t._encode_key_event(_ev(_VK_INSERT)) == "\x1b[2~"
    assert t._encode_key_event(_ev(_VK_DELETE)) == "\x1b[3~"
    assert t._encode_key_event(_ev(_VK_PRIOR)) == "\x1b[5~"   # PageUp
    assert t._encode_key_event(_ev(_VK_NEXT)) == "\x1b[6~"    # PageDown


# ─── Tab / Enter / Backspace / Escape ───────────────────────────────────


def test_plain_tab_emits_tab_char():
    t = _t()
    assert t._encode_key_event(_ev(_VK_TAB, "\t")) == "\t"


def test_shift_tab_emits_csi_z():
    """Shift+Tab is the canonical use case for VT_INPUT — must be \\x1b[Z."""
    t = _t()
    assert t._encode_key_event(_ev(_VK_TAB, cks=_SHIFT_PRESSED)) == "\x1b[Z"


def test_enter_emits_cr():
    t = _t()
    assert t._encode_key_event(_ev(_VK_RETURN, "\r")) == "\r"


def test_alt_enter_emits_esc_cr():
    """Alt+Enter → \\x1b\\r so keys.py recognizes it as 'alt+enter'."""
    t = _t()
    assert t._encode_key_event(_ev(_VK_RETURN, cks=_ALT_PRESSED)) == "\x1b\r"


def test_backspace_emits_del_modern_convention():
    """Plain Backspace → \\x7f (DEL); matches what agent-tui's keys.py expects."""
    t = _t()
    assert t._encode_key_event(_ev(_VK_BACK)) == "\x7f"


def test_ctrl_backspace_emits_bs():
    t = _t()
    assert t._encode_key_event(_ev(_VK_BACK, cks=_CTRL_PRESSED)) == "\x08"


def test_escape_emits_esc_char():
    t = _t()
    assert t._encode_key_event(_ev(_VK_ESCAPE, "\x1b")) == "\x1b"


# ─── Character input (the IME-safe path) ────────────────────────────────


def test_printable_ascii_passes_through():
    t = _t()
    assert t._encode_key_event(_ev(0x41, "a")) == "a"
    assert t._encode_key_event(_ev(0x5A, "Z")) == "Z"
    assert t._encode_key_event(_ev(0x31, "1")) == "1"


def test_cjk_character_passes_through_unchanged():
    """The whole point of switching to ReadConsoleInputW: IME-composed CJK
    characters arrive as a single KEY_EVENT with uChar.UnicodeChar set to
    the final character. Pinyin letters typed during preedit do NOT fire
    KEY_EVENTs — only the composed CJK character does.
    """
    t = _t()
    assert t._encode_key_event(_ev(0, "你")) == "你"
    assert t._encode_key_event(_ev(0, "好")) == "好"
    assert t._encode_key_event(_ev(0, "🎉")) == "🎉"  # also non-BMP emoji


def test_ctrl_letter_emits_control_char():
    """Windows fills uChar.UnicodeChar with the control character itself
    for Ctrl+letter (Ctrl+C → '\\x03'). We pass it through directly so
    keys.py's ctrl+c / ctrl+d matching works."""
    t = _t()
    # Ctrl+C
    assert t._encode_key_event(_ev(0x43, "\x03", cks=_CTRL_PRESSED)) == "\x03"
    # Ctrl+A
    assert t._encode_key_event(_ev(0x41, "\x01", cks=_CTRL_PRESSED)) == "\x01"
    # Ctrl+D (EOF)
    assert t._encode_key_event(_ev(0x44, "\x04", cks=_CTRL_PRESSED)) == "\x04"
    # Ctrl+Z
    assert t._encode_key_event(_ev(0x5A, "\x1a", cks=_CTRL_PRESSED)) == "\x1a"


def test_alt_plus_printable_emits_esc_then_char():
    """Alt+x → \\x1bx so keys.py matches it as 'alt+x'."""
    t = _t()
    assert t._encode_key_event(_ev(0x58, "x", cks=_ALT_PRESSED)) == "\x1bx"
    assert t._encode_key_event(_ev(0x41, "a", cks=_ALT_PRESSED)) == "\x1ba"


# ─── Empty / dropped events ─────────────────────────────────────────────


def test_modifier_only_key_is_dropped():
    """A lone Shift/Ctrl/Alt press has no character and no mapped VK; drop it.

    These keys' effect is captured via dwControlKeyState on the NEXT real
    key event, so emitting anything here would create phantom inputs.
    """
    t = _t()
    # VK_SHIFT (0x10), no UnicodeChar, no Shift bit set (it IS the shift key)
    assert t._encode_key_event(_ev(0x10)) == ""
    # VK_CONTROL (0x11)
    assert t._encode_key_event(_ev(0x11)) == ""
    # VK_MENU (0x12) — Alt
    assert t._encode_key_event(_ev(0x12)) == ""


def test_null_character_does_not_pass_through():
    """uChar = '\\x00' (unset) must NOT be emitted as a literal null."""
    t = _t()
    # An event with no mapped VK and no character
    assert t._encode_key_event(_ev(0, "\x00")) == ""


# ─── bKeyDown is NOT checked inside _encode_key_event ───────────────────
# (the read loop filters key-up events before calling this). But we
# document the contract by checking that a key-up event's contents would
# still encode the same way — the caller is responsible for filtering.


def test_caller_must_filter_key_up_events():
    """_encode_key_event does not check bKeyDown; the read loop does.

    Verifying the contract: if a caller forgets to filter, they'd get the
    same encoding for key-down and key-up. That's acceptable because the
    read loop is the sole caller and always filters.
    """
    t = _t()
    down = _ev(_VK_LEFT, key_down=True)
    up = _ev(_VK_LEFT, key_down=False)
    assert t._encode_key_event(down) == t._encode_key_event(up) == "\x1b[D"


# ─── Structural constants ───────────────────────────────────────────────


def test_key_event_type_constant():
    """Sanity check the EventType values match Windows SDK."""
    assert _KEY_EVENT_TYPE == 0x0001


def test_modifier_flag_values_match_wincon_h():
    """Guard against typos in the bit-flag constants."""
    assert _SHIFT_PRESSED == 0x0010
    assert _LEFT_ALT_PRESSED == 0x0002
    assert _ALT_PRESSED == 0x0003   # left | right
    assert _CTRL_PRESSED == 0x000C  # left | right
    assert _ENHANCED_KEY == 0x0100
