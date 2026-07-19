"""Keybinding defaults.

Centralizes the default key assignments so they are not hardcoded across the
codebase (AGENTS.md: "Never hardcode key checks... Add defaults to
DEFAULT_EDITOR_KEYBINDINGS or DEFAULT_APP_KEYBINDINGS"). editor support will let
settings.json override these.

Each entry maps an action name to its default key(s) and a human-readable
description (used by ``/hotkeys``). A key may be a string or a list of strings
(alternatives). An empty list means the action has no default key (unbound).

"""
from __future__ import annotations

import sys
from typing import Union

#: A key spec: a single key string, a list of alternatives, or empty (unbound).
KeySpec = Union[str, list[str]]


# ─── App-level keybindings (interactive mode) ─────────────────────────

DEFAULT_APP_KEYBINDINGS: dict[str, dict] = {
    "app.interrupt": {
        "keys": "escape",
        "description": "Cancel or abort (interrupt streaming / compaction)",
    },
    "app.clear": {
        "keys": "ctrl+c",
        "description": "Clear editor (press twice quickly to quit)",
    },
    "app.exit": {
        "keys": "ctrl+d",
        "description": "Exit when the editor is empty",
    },
    "app.suspend": {
        # Ctrl+Z suspends on Unix; no default on Windows (it's undo there).
        "keys": [] if sys.platform == "win32" else "ctrl+z",
        "description": "Suspend to background",
    },
    "app.thinking.cycle": {
        "keys": "shift+tab",
        "description": "Cycle thinking level",
    },
    "app.thinking.toggle": {
        "keys": "ctrl+t",
        "description": "Toggle thinking block visibility",
    },
    "app.tools.expand": {
        "keys": "ctrl+o",
        "description": "Toggle tool output expansion",
    },
}


# ─── Editor-level keybindings (for /hotkeys display) ──────────────────
# The Editor component implements these internally; this table documents its defaults.

DEFAULT_EDITOR_KEYBINDINGS: dict[str, dict] = {
    "editor.submit": {"keys": "enter", "description": "Send message"},
    "editor.newline": {"keys": "shift+enter", "description": "Insert newline"},
    "editor.historyUp": {"keys": "up", "description": "Previous history entry"},
    "editor.historyDown": {"keys": "down", "description": "Next history entry"},
    "editor.deleteToEndOfLine": {"keys": "ctrl+k", "description": "Delete to end of line"},
    "editor.deleteToStartOfLine": {"keys": "ctrl+u", "description": "Delete to start of line"},
    "editor.deleteWordBackwards": {"keys": "ctrl+w", "description": "Delete previous word"},
    "editor.yank": {"keys": "ctrl+y", "description": "Paste (yank from kill ring)"},
    "editor.lineStart": {"keys": "ctrl+a", "description": "Move to line start"},
    "editor.lineEnd": {"keys": "ctrl+e", "description": "Move to line end"},
}


def get_keybinding(action: str) -> KeySpec:
    """Return the default key(s) for an action.

    Looks up ``action`` in the app table, then the editor table. Returns the
    key spec (string, list, or empty list if unbound). Raises ``KeyError`` if
    the action is unknown.
    """
    if action in DEFAULT_APP_KEYBINDINGS:
        return DEFAULT_APP_KEYBINDINGS[action]["keys"]
    if action in DEFAULT_EDITOR_KEYBINDINGS:
        return DEFAULT_EDITOR_KEYBINDINGS[action]["keys"]
    raise KeyError(f"Unknown keybinding action: {action}")


def key_text(action: str) -> str:
    """Return a human-readable key hint for an action (e.g. ``"Esc"``).

    Used in cancel hints and ``/hotkeys``. Returns ``""`` if the action is
    unbound.
    """
    spec = get_keybinding(action)
    if not spec:
        return ""
    if isinstance(spec, list):
        return " / ".join(_pretty(k) for k in spec)
    return _pretty(spec)


_PRETTY_MAP = {
    "escape": "Esc", "enter": "Enter", "tab": "Tab", "backspace": "Backspace",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "delete": "Delete", "home": "Home", "end": "End",
}


def _pretty(key: str) -> str:
    """Render a key spec fragment for display (e.g. ``ctrl+c`` → ``Ctrl+C``)."""
    if key in _PRETTY_MAP:
        return _PRETTY_MAP[key]
    # Split on '+' so modifiers in any order are recognized.
    fragments = key.split("+")
    parts: list[str] = []
    for frag in fragments:
        if frag in ("ctrl", "shift", "alt", "meta"):
            parts.append(frag.capitalize())
        elif frag in _PRETTY_MAP:
            parts.append(_PRETTY_MAP[frag])
        else:
            parts.append(frag.upper() if len(frag) == 1 else frag.capitalize())
    return "+".join(parts)


def all_hotkeys() -> list[tuple[str, str, str]]:
    """Return (category, keys-display, description) for /hotkeys, sorted.

    Category is ``"App"`` or ``"Editor"``.
    """
    rows: list[tuple[str, str, str]] = []
    for action, info in DEFAULT_APP_KEYBINDINGS.items():
        keys = info["keys"]
        label = " / ".join(_pretty(k) for k in keys) if isinstance(keys, list) else (_pretty(keys) if keys else "—")
        rows.append(("App", label, info["description"]))
    for action, info in DEFAULT_EDITOR_KEYBINDINGS.items():
        keys = info["keys"]
        label = " / ".join(_pretty(k) for k in keys) if isinstance(keys, list) else (_pretty(keys) if keys else "—")
        rows.append(("Editor", label, info["description"]))
    return rows
