"""LoginDialogComponent — inline API-key input.

When the user submits ``/login``, the editor is swapped out and this
component takes its place. Layout (top → bottom)::

    ─── top border ───              (border token, blue)
      Login to <Provider>           (accent + bold, separate title line)
      <warning-colored hint>        (e.g. "明文显示, Enter 保存, Esc 取消")
      > <api-key input + cursor>
    ─── bottom border ───

The API key is echoed in cleartext as the user types; the hint makes this explicit.

Keys: type to edit, Enter to submit (fires ``on_submit`` with the key),
Esc / Ctrl+C to cancel (fires ``on_cancel``).
"""
from __future__ import annotations

from typing import Callable, List

from agent_tui.theme import Theme
from agent_tui.utils import visible_width

from coding_agent.modes.interactive.components.text_input import TextInput


class LoginDialogComponent:
    """Bordered API-key input dialog, swapped in for the editor."""

    def __init__(
        self,
        theme: Theme,
        provider_name: str,
        on_submit: Callable[[str], None],
        on_cancel: Callable[[], None],
        *,
        border_color_fn: "Callable[[str], str] | None" = None,
    ) -> None:
        self._theme = theme
        # Border color defaults to the static "border" (blue) token.
        # The login dialog does not recolor with thinking level; only the
        # editor does.
        self._border_color_fn = border_color_fn or (lambda s: theme.fg("border", s))
        self._on_submit_outer = on_submit
        self._on_cancel_outer = on_cancel
        # Title: accent + bold, on its own line below the top border
        #.
        self._title = f"Login to {provider_name}"
        # Auth-related hint text uses the warning (yellow) token.
        self._hint = "明文显示 API key, Enter 保存, Esc 取消"
        # TextInput owns the value + handles all keys; its submit/cancel
        # callbacks are routed back out to this dialog's callers.
        self._input = TextInput(
            on_submit=self._on_input_submit,
            on_cancel=self._on_input_cancel,
        )
        self.focused = True

    # ── TextInput → outer callback routing ─────────────────────────────

    def _on_input_submit(self, value: str) -> None:
        if self._on_submit_outer is not None:
            self._on_submit_outer(value)

    def _on_input_cancel(self) -> None:
        if self._on_cancel_outer is not None:
            self._on_cancel_outer()

    # ── input ──────────────────────────────────────────────────────────

    def handle_input(self, data: str) -> bool:
        """All keys (edit/submit/cancel) go to the TextInput."""
        return self._input.handle_input(data)

    # ── render ─────────────────────────────────────────────────────────

    def render(self, width: int) -> List[str]:
        """Render the bordered dialog.

        Order: top border, accent+bold title, warning hint, ``>`` input,
        bottom border.
        """
        border = self._border_color_fn("─" * width)
        indent = "  "
        content_width = max(1, width - len(indent))
        title_line = self._theme.fg(
            "accent", self._theme.bold(_truncate(self._title, content_width))
        )
        hint_line = self._theme.fg("warning", _truncate(self._hint, content_width))
        input_line = self._input.render(content_width)
        return [
            border,
            indent + title_line,
            indent + hint_line,
            indent + input_line,
            border,
        ]


def _truncate(text: str, width: int) -> str:
    """Truncate ``text`` to ``width`` visible columns, appending ``…``."""
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
