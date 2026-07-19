"""Key matching against raw terminal input.

The TUI reads raw bytes from stdin and components call
``matches_key(data, "ctrl+c")`` to check what was pressed. Legacy escape
sequences and Ctrl/Alt/Shift combinations on printable characters are supported.
Kitty CSI-u and modifyOtherKeys sequences return ``False`` in this module.
"""
from __future__ import annotations

from typing import Final

# ─── modifier bitmask ────────────────────────────────

MOD_SHIFT: Final[int] = 1
MOD_ALT: Final[int] = 2
MOD_CTRL: Final[int] = 4
MOD_SUPER: Final[int] = 8

#: Kitty protocol active flag. Set by ProcessTerminal after negotiation.
#: When True, some legacy sequences are reinterpreted (e.g. \x1b\r).
_kitty_protocol_active: bool = False


def set_kitty_protocol_active(active: bool) -> None:
    """Toggle Kitty protocol interpretation."""
    global _kitty_protocol_active
    _kitty_protocol_active = active


def is_kitty_protocol_active() -> bool:
    """Whether Kitty keyboard protocol is active."""
    return _kitty_protocol_active


# ─── codepoints ───────────────────────────────────

CP_ENTER = ord("\r")
CP_TAB = ord("\t")
CP_BACKSPACE = 127
CP_ESCAPE = 27
CP_SPACE = 32
CP_KP_ENTER = ord("\r")  # numpad enter, same as enter for matching


# ─── legacy escape sequence tables ───────────────────

LEGACY_KEY_SEQUENCES: dict[str, list[str]] = {
    "up": ["\x1b[A", "\x1bOA"],
    "down": ["\x1b[B", "\x1bOB"],
    "right": ["\x1b[C", "\x1bOC"],
    "left": ["\x1b[D", "\x1bOD"],
    "home": ["\x1b[H", "\x1bOH", "\x1b[1~", "\x1b[7~"],
    "end": ["\x1b[F", "\x1bOF", "\x1b[4~", "\x1b[8~"],
    "insert": ["\x1b[2~"],
    "delete": ["\x1b[3~"],
    "pageup": ["\x1b[5~", "\x1b[[5~"],
    "pagedown": ["\x1b[6~", "\x1b[[6~"],
    "clear": ["\x1b[E", "\x1bOE"],
    "f1": ["\x1bOP", "\x1b[11~", "\x1b[[A"],
    "f2": ["\x1bOQ", "\x1b[12~", "\x1b[[B"],
    "f3": ["\x1bOR", "\x1b[13~", "\x1b[[C"],
    "f4": ["\x1bOS", "\x1b[14~", "\x1b[[D"],
    "f5": ["\x1b[15~", "\x1b[[E"],
    "f6": ["\x1b[17~"],
    "f7": ["\x1b[18~"],
    "f8": ["\x1b[19~"],
    "f9": ["\x1b[20~"],
    "f10": ["\x1b[21~"],
    "f11": ["\x1b[23~"],
    "f12": ["\x1b[24~"],
}

LEGACY_SHIFT_SEQUENCES: dict[str, list[str]] = {
    "up": ["\x1b[a"], "down": ["\x1b[b"], "right": ["\x1b[c"], "left": ["\x1b[d"],
    "clear": ["\x1b[e"], "insert": ["\x1b[2$"], "delete": ["\x1b[3$"],
    "pageup": ["\x1b[5$"], "pagedown": ["\x1b[6$"], "home": ["\x1b[7$"], "end": ["\x1b[8$"],
}

LEGACY_CTRL_SEQUENCES: dict[str, list[str]] = {
    "up": ["\x1bOa"], "down": ["\x1bOb"], "right": ["\x1bOc"], "left": ["\x1bOd"],
    "clear": ["\x1bOe"], "insert": ["\x1b[2^"], "delete": ["\x1b[3^"],
    "pageup": ["\x1b[5^"], "pagedown": ["\x1b[6^"], "home": ["\x1b[7^"], "end": ["\x1b[8^"],
}

#: Direct sequence → keyId map.
#: Includes common CSI arrow sequences (\x1b[A etc.) so terminals without
#: via LEGACY_KEY_SEQUENCES but omits from the reverse map; parse_key needs them.
LEGACY_SEQUENCE_KEY_IDS: dict[str, str] = {
    "\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left",
    "\x1bOA": "up", "\x1bOB": "down", "\x1bOC": "right", "\x1bOD": "left",
    "\x1b[Z": "shift+tab",
    "\x1b[H": "home", "\x1b[F": "end",
    "\x1bOH": "home", "\x1bOF": "end",
    "\x1b[1~": "home", "\x1b[7~": "home", "\x1b[4~": "end", "\x1b[8~": "end",
    "\x1b[E": "clear", "\x1bOE": "clear", "\x1bOe": "ctrl+clear", "\x1b[e": "shift+clear",
    "\x1b[2~": "insert", "\x1b[2$": "shift+insert", "\x1b[2^": "ctrl+insert",
    "\x1b[3$": "shift+delete", "\x1b[3^": "ctrl+delete",
    "\x1b[[5~": "pageup", "\x1b[[6~": "pagedown",
    "\x1b[a": "shift+up", "\x1b[b": "shift+down", "\x1b[c": "shift+right", "\x1b[d": "shift+left",
    "\x1bOa": "ctrl+up", "\x1bOb": "ctrl+down", "\x1bOc": "ctrl+right", "\x1bOd": "ctrl+left",
    "\x1b[5$": "shift+pageup", "\x1b[6$": "shift+pagedown",
    "\x1b[7$": "shift+home", "\x1b[8$": "shift+end",
    "\x1b[5^": "ctrl+pageup", "\x1b[6^": "ctrl+pagedown",
    "\x1b[7^": "ctrl+home", "\x1b[8^": "ctrl+end",
    "\x1bOP": "f1", "\x1bOQ": "f2", "\x1bOR": "f3", "\x1bOS": "f4",
    "\x1b[11~": "f1", "\x1b[12~": "f2", "\x1b[13~": "f3", "\x1b[14~": "f4",
    "\x1b[[A": "f1", "\x1b[[B": "f2", "\x1b[[C": "f3", "\x1b[[D": "f4", "\x1b[[E": "f5",
    "\x1b[15~": "f5", "\x1b[17~": "f6", "\x1b[18~": "f7", "\x1b[19~": "f8",
    "\x1b[20~": "f9", "\x1b[21~": "f10", "\x1b[23~": "f11", "\x1b[24~": "f12",
    "\x1bb": "alt+left", "\x1bf": "alt+right", "\x1bp": "alt+up", "\x1bn": "alt+down",
}


# ─── KeyId parsing ───────────────────────────────────

def parse_key_id(key_id: str) -> "dict | None":
    """Parse a keyId like ``"ctrl+shift+p"`` into components.

    Returns ``{key, ctrl, shift, alt, super}`` or None if empty.
    """
    parts = key_id.lower().split("+")
    key = parts[-1]
    if not key:
        return None
    return {
        "key": key,
        "ctrl": "ctrl" in parts,
        "shift": "shift" in parts,
        "alt": "alt" in parts,
        "super": "super" in parts,
    }


# ─── Key matching ───────────────────────────────────

def matches_key(data: str, key_id: str) -> bool:
    """Match raw input ``data`` against a key identifier.

    Supported key identifiers:
      - Single: ``"escape"``, ``"tab"``, ``"enter"``, ``"backspace"``,
        ``"delete"``, ``"home"``, ``"end"``, ``"space"``
      - Arrows: ``"up"``, ``"down"``, ``"left"``, ``"right"``
      - Ctrl: ``"ctrl+c"``, ``"ctrl+z"``, ...
      - Shift: ``"shift+tab"``, ``"shift+enter"``
      - Alt: ``"alt+enter"``, ``"alt+backspace"``
      - Combined: ``"shift+ctrl+p"``, ``"ctrl+alt+x"``

    Supports legacy escape sequences and Ctrl/Alt/Shift on printable characters.
    Kitty CSI-u and modifyOtherKeys sequences return ``False``.
    """
    parsed = parse_key_id(key_id)
    if not parsed:
        return False

    key = parsed["key"]
    modifier = 0
    if parsed["shift"]:
        modifier |= MOD_SHIFT
    if parsed["alt"]:
        modifier |= MOD_ALT
    if parsed["ctrl"]:
        modifier |= MOD_CTRL
    if parsed["super"]:
        modifier |= MOD_SUPER

    # ── named keys ──
    if key in ("escape", "esc"):
        return modifier == 0 and data == "\x1b"

    if key == "space":
        if modifier == MOD_CTRL:
            return data == "\x00"
        if modifier == MOD_ALT:
            return data == "\x1b "
        return modifier == 0 and data == " "

    if key == "tab":
        if modifier == MOD_SHIFT:
            return data == "\x1b[Z"
        return modifier == 0 and data == "\t"

    if key in ("enter", "return"):
        if modifier == MOD_SHIFT:
            return _kitty_protocol_active and (data == "\x1b\r" or data == "\n")
        if modifier == MOD_ALT:
            return not _kitty_protocol_active and data == "\x1b\r"
        return modifier == 0 and data in ("\r", "\x1bOM")

    if key == "backspace":
        if modifier == MOD_ALT:
            return data in ("\x1b\x7f", "\x1b\b")
        if modifier == MOD_CTRL:
            return data == "\x08"  # Ctrl+Backspace on Windows Terminal
        return modifier == 0 and data in ("\x7f", "\b")

    if key == "delete":
        if modifier == MOD_SHIFT:
            return data == "\x1b[3$"
        if modifier == MOD_CTRL:
            return data == "\x1b[3^"
        return modifier == 0 and data == "\x1b[3~"

    if key == "insert":
        if modifier == MOD_SHIFT:
            return data == "\x1b[2$"
        if modifier == MOD_CTRL:
            return data == "\x1b[2^"
        return modifier == 0 and data == "\x1b[2~"

    # ── arrow keys + home/end/page/clear/f-keys (legacy sequences) ──
    if key in LEGACY_KEY_SEQUENCES:
        if modifier == 0:
            return data in LEGACY_KEY_SEQUENCES[key]
        if modifier == MOD_SHIFT and key in LEGACY_SHIFT_SEQUENCES:
            return data in LEGACY_SHIFT_SEQUENCES[key]
        if modifier == MOD_CTRL and key in LEGACY_CTRL_SEQUENCES:
            return data in LEGACY_CTRL_SEQUENCES[key]

    # ── ctrl + printable char ──
    if parsed["ctrl"] and not parsed["alt"] and not parsed["shift"] and not parsed["super"]:
        # Ctrl+a = 0x01 ... Ctrl+z = 0x1a; also handle Ctrl+[ \ ] ^ _
        cp = ord(key) if len(key) == 1 else -1
        if 0x61 <= cp <= 0x7A:  # a-z
            return data == chr(cp - 0x60)
        if key == "[":
            return data == "\x1b"
        if key == "\\":
            return data == "\x1c"
        if key == "]":
            return data == "\x1d"
        if key == "^":
            return data == "\x1e"
        if key == "_":
            return data == "\x1f"
        if key == " ":
            return data == "\x00"

    # ── alt + printable char ──
    if parsed["alt"] and not parsed["ctrl"] and not parsed["shift"] and not parsed["super"]:
        if len(key) == 1:
            # Alt+x = ESC followed by x.
            return data == "\x1b" + key

    # ── plain printable char ──
    if modifier == 0 and len(key) == 1:
        return data == key

    return False


# ─── parse_key ──────────────────────────────────────────

def parse_key(data: str) -> "str | None":
    """Parse raw ``data`` into a keyId string, or None if unrecognized.

    Inverse of ``matches_key``: given raw bytes,
    return the canonical keyId. Checks legacy sequences first, then printable.
    """
    if not data:
        return None
    # Direct legacy sequence lookup.
    if data in LEGACY_SEQUENCE_KEY_IDS:
        return LEGACY_SEQUENCE_KEY_IDS[data]
    # Named single-byte keys.
    if data == "\x1b":
        return "escape"
    if data == "\t":
        return "tab"
    if data in ("\r", "\x1bOM"):
        return "enter"
    if data in ("\x7f", "\b"):
        return "backspace"
    if data == " ":
        return "space"
    # Ctrl + letter.
    if len(data) == 1:
        cp = ord(data)
        if 0x01 <= cp <= 0x1A:
            return "ctrl+" + chr(cp + 0x60)
        if 0x1B <= cp <= 0x1F:
            ctrl_map = {0x1B: "[", 0x1C: "\\", 0x1D: "]", 0x1E: "^", 0x1F: "_"}
            return "ctrl+" + ctrl_map.get(cp, "?")
    # Plain printable.
    if len(data) == 1 and data.isprintable():
        return data
    return None


# ─── key release detection ───────────────────────────────

def is_key_release(data: str) -> bool:
    """Whether ``data`` is a Kitty key-release event.

    Kitty release events have the form ``CSI <cp> : <event> u`` where event=2
    (release) or event=3 (repeat). Basic detection is sufficient here; full Kitty parsing
    in advanced terminal support.
    """
    # Kitty CSI-u with event type in the :event field.
    # Format: ESC [ <params> [;<mod>] [:<event>] u
    if data.startswith("\x1b[") and data.endswith("u") and ":" in data:
        # Has a :event field — check if it's a release (2) or repeat (3).
        parts = data[2:-1]  # strip ESC [ and u
        if ":" in parts:
            event_part = parts.rsplit(":", 1)[-1]
            if event_part in ("2", "3"):
                return True
    return False


def is_key_repeat(data: str) -> bool:
    """Whether ``data`` is a Kitty key-repeat event."""
    if data.startswith("\x1b[") and data.endswith("u") and ":" in data:
        parts = data[2:-1]
        if ":" in parts:
            event_part = parts.rsplit(":", 1)[-1]
            return event_part == "3"
    return False


# ─── legacy prompt_toolkit compat (removed in editor support Editor rewrite) ──
#
# chat_simple.py still uses prompt_toolkit's KeyBindings via create_key_bindings().
# This is the OLD input path (prompt_toolkit owns the buffer); the NEW TUI uses
# raw-byte matches_key() above. This compatibility shim remains while chat_simple uses
# to the new Editor component in editor support. Do not use in new code.


def create_key_bindings():  # pragma: no cover - legacy compat, removed in editor support
    """Build prompt_toolkit Emacs keybindings (LEGACY — use matches_key instead).

    Kept only for chat_simple.py compatibility until editor support Editor rewrite.
    New code should use the raw-byte ``matches_key(data, key_id)`` API above.
    """
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()

    @kb.add("c-a")
    def _(event):
        event.current_buffer.cursor_position = 0

    @kb.add("c-e")
    def _(event):
        event.current_buffer.cursor_position = len(event.current_buffer.text)

    @kb.add("c-b")
    def _(event):
        if event.current_buffer.cursor_position > 0:
            event.current_buffer.cursor_position -= 1

    @kb.add("c-f")
    def _(event):
        if event.current_buffer.cursor_position < len(event.current_buffer.text):
            event.current_buffer.cursor_position += 1

    @kb.add("c-w")
    def _(event):
        buf = event.current_buffer
        text_before = buf.text[: buf.cursor_position]
        last_space = text_before.rstrip().rfind(" ")
        if last_space == -1:
            start = len(text_before) - len(text_before.lstrip())
            buf.delete_before_cursor(buf.cursor_position - start)
        else:
            buf.delete_before_cursor(buf.cursor_position - last_space - 1)

    @kb.add("c-k")
    def _(event):
        buf = event.current_buffer
        if buf.cursor_position < len(buf.text):
            buf.delete(len(buf.text) - buf.cursor_position)

    @kb.add("c-u")
    def _(event):
        buf = event.current_buffer
        if buf.cursor_position > 0:
            buf.delete_before_cursor(buf.cursor_position)

    @kb.add("c-h", "backspace")
    def _(event):
        buf = event.current_buffer
        if buf.cursor_position > 0:
            buf.delete_before_cursor(1)

    @kb.add("home")
    def _(event):
        event.current_buffer.cursor_position = 0

    @kb.add("end")
    def _(event):
        event.current_buffer.cursor_position = len(event.current_buffer.text)

    return kb
