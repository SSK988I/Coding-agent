"""TextInput — single-line text field.

A pure-logic input widget used by components that need a one-line text entry
(the model selector's filter box, the login dialog's API-key field). It is
NOT a focusable TUI component and is never added to the component tree — the
parent component owns the focus and dispatches keypresses to it via
:meth:`handle_input`.

Renders ``> <text>`` followed by a one-column reverse-video cursor block,
matching the ``Input`` look.

Callbacks (optional):
  - ``on_submit(value)``: fired on Enter.
  - ``on_cancel()``: fired on Escape / Ctrl+C.

When used purely as a filter (no submit/cancel semantics of its own — e.g.
inside the model selector), leave both callbacks ``None`` and have the parent
intercept Enter/Esc/↑↓ before delegating the remaining keys here.
"""
from __future__ import annotations

from typing import Callable

from agent_tui.keys import matches_key
from agent_tui.utils import visible_width


class TextInput:
    """Single-line text input (pure logic, not a TUI component)."""

    def __init__(
        self,
        *,
        initial: str = "",
        placeholder: str = "",
        on_submit: "Callable[[str], None] | None" = None,
        on_cancel: "Callable[[], None] | None" = None,
    ) -> None:
        self._value = initial
        self._placeholder = placeholder
        self._on_submit = on_submit
        self._on_cancel = on_cancel

    # ── state ──────────────────────────────────────────────────────────

    @property
    def value(self) -> str:
        return self._value

    def set_value(self, v: str) -> None:
        self._value = v

    # ── input ──────────────────────────────────────────────────────────

    def handle_input(self, data: str) -> bool:
        """Consume submit/cancel/edit/printable/paste keys.

        Returns True if handled. Handles both single-char keystrokes and
        multi-character payloads, which arrive in two situations:

          - Bracketed paste: the terminal wraps pasted text as
            ``\\x1b[200~<text>\\x1b[201~``. We detect the markers and unwrap.
          - The OS coalescing several bytes into one read: e.g. typing fast,
            or any IME/paste path that bypasses bracketed paste.

        Without this, pasted API keys (which are long) silently never reach
        ``self._value``, and even quick typing can drop characters.
        """
        # Submit.
        if matches_key(data, "enter"):
            if self._on_submit is not None:
                self._on_submit(self._value)
            return True
        # Cancel.
        if matches_key(data, "escape") or matches_key(data, "ctrl+c"):
            if self._on_cancel is not None:
                self._on_cancel()
            return True
        # Backspace.
        if matches_key(data, "backspace"):
            if self._value:
                self._value = self._value[:-1]
            return True

        # Bracketed paste — strip the markers and treat the inner text as a
        # paste payload (no control chars are kept).
        stripped = _strip_bracketed_paste(data)
        if stripped is not None:
            self._append_text(stripped)
            return True

        # Single printable character.
        if len(data) == 1 and data.isprintable():
            self._value += data
            return True

        # Multi-character run of printable characters (coalesced by the OS,
        # or a paste path without bracketed-paste markers). Reject anything
        # containing control characters so escape sequences (F-keys, etc.)
        # fall through and report "not consumed".
        if len(data) > 1 and all(c.isprintable() for c in data):
            self._append_text(data)
            return True
        return False

    def _append_text(self, text: str) -> None:
        """Append a (possibly multi-line) text run, keeping only the first line.

        A single-line input cannot hold newlines; pasting ``"key\n"`` would
        otherwise store the trailing newline and break submission. We drop
        everything after the first ``\\n``/``\\r`` (matching how a one-line
        field typically handles a paste).
        """
        first_line = text.split("\n", 1)[0].split("\r", 1)[0]
        self._value += first_line

    # ── render ─────────────────────────────────────────────────────────

    def render(self, width: int) -> str:
        """Render ``> <text>`` + trailing cursor block, fitting ``width`` columns.

        When the value is empty, the placeholder is shown (plain, not dimmed).
        The cursor is a one-column inverse-video block appended after the text.

        Emits ``CURSOR_MARKER`` immediately before the visual cursor block so
        the TUI can position the hardware cursor there — this is what Windows
        IME reads to place its candidate window. Without it, IME candidates
        float to the screen bottom-right instead of following the input.
        """
        from agent_tui.tui import CURSOR_MARKER

        prefix = "> "
        # CURSOR_MARKER is a zero-width OSC sequence the TUI strips before
        # flushing, so it doesn't affect layout — but we emit it at the
        # cursor position (just before the reverse-video block) so the
        # hardware cursor lands there.
        cursor = CURSOR_MARKER + "\x1b[7m \x1b[0m"
        cursor_w = 1
        avail = max(0, width - len(prefix) - cursor_w)

        shown = self._value if self._value else self._placeholder
        # Truncate to ``avail`` visible columns.
        shown = _truncate_visible(shown, avail)
        line = prefix + shown + cursor
        # Pad to width so the row fills its column (border-aligned).
        pad = max(0, width - visible_width(line))
        return line + " " * pad


def _strip_bracketed_paste(data: str) -> "str | None":
    """If ``data`` is a bracketed-paste payload, return its inner text.

    Returns ``None`` if ``data`` is not a bracketed paste. The terminal wraps
    pasted content as ``\\x1b[200~<text>\\x1b[201~``. We accept both the full
    wrapped form and a trailing-marker-less prefix (some terminals deliver the
    end-marker as a separate read).
    """
    START = "\x1b[200~"
    END = "\x1b[201~"
    if not data.startswith(START):
        return None
    inner = data[len(START):]
    if inner.endswith(END):
        inner = inner[: -len(END)]
    return inner


def _truncate_visible(text: str, width: int) -> str:
    """Truncate ``text`` to ``width`` visible columns, appending ``…`` if cut."""
    if visible_width(text) <= width:
        return text
    if width <= 0:
        return ""
    if width == 1:
        return "…"
    out = ""
    for ch in text:
        if visible_width(out + ch) >= width:
            return out + "…"
        out += ch
    return out
