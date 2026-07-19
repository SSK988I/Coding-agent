"""ToolExecutionComponent — renders tool calls and results.

Displays tool invocations as cards in the chat: a pending state during
execution, then a result with preview. Supports expand/collapse and
per-tool output preview (bash: last N lines, read: first N lines).

Uses the built-in text renderer; image previews are handled elsewhere.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from agent_tui import Box, Container, Text
from agent_tui.theme import Theme

# ─── Preview constants ────────────────

BASH_PREVIEW_LINES = 5
READ_PREVIEW_LINES = 10


class ToolExecutionComponent(Container):
    """Renders a tool call and its result."""

    def __init__(
        self,
        tool_name: str,
        tool_call_id: str,
        args: dict,
        *,
        theme: Theme | None = None,
    ) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id
        self.args = args
        self._theme = theme
        self._card = Box(padding_x=1, padding_y=0)
        # Pending state gets a dark blue-grey background while the tool runs;
        # _render_done swaps it to success (green) or error (red). Aligned with
        # the pre-registered theme tokens
        # toolPendingBg / toolSuccessBg / toolErrorBg.
        self._card.set_bg_fn(self._bg("toolPendingBg"))
        self.add_child(self._card)

        # Render the initial "executing…" state.
        self._render_pending()

    def set_result(self, result: Any, is_error: bool = False) -> None:
        """Update the card with the tool execution result."""
        raw = ""
        if hasattr(result, "content") and result.content:
            raw = getattr(result.content[0], "text", "")
        preview = self._preview_output(raw)
        self._render_done(preview, is_error)

    def _render_pending(self) -> None:
        """Render the pending (executing) state."""
        args_str = _format_args(self.args)
        label = self._styled("accent", f"⚙ {self.tool_name}")
        body = self._styled("muted", f"  {args_str}\n  执行中…")
        self._card.clear()
        self._card.add_child(Text(f"{label}\n{body}", padding_x=0, padding_y=0))
        # Dark blue-grey background while executing.
        self._card.set_bg_fn(self._bg("toolPendingBg"))

    def _render_done(self, preview: str, is_error: bool) -> None:
        """Render the completed state (ok or error)."""
        mark = self._styled("error", "✗") if is_error else self._styled("accent", "✓")
        status = "error" if is_error else "ok"
        head = self._styled("muted", f"{mark} {self.tool_name} → {status}")
        if preview:
            body = self._styled("toolOutput", preview)
        else:
            body = self._styled("muted", "  (no output)")
        self._card.clear()
        self._card.add_child(Text(f"{head}\n{body}", padding_x=0, padding_y=0))
        # Dark green on success, dark red on error.
        self._card.set_bg_fn(self._bg("toolErrorBg" if is_error else "toolSuccessBg"))

    def _preview_output(self, raw: str) -> str:
        """Return a line-level output preview."""
        if not raw:
            return ""
        lines = raw.splitlines()
        if self.tool_name == "bash":
            if len(lines) <= BASH_PREVIEW_LINES:
                return "\n".join(lines)
            skipped = len(lines) - BASH_PREVIEW_LINES
            return self._styled("muted", f"... ({skipped} earlier lines)\n") + "\n".join(lines[-BASH_PREVIEW_LINES:])
        if self.tool_name == "read":
            if len(lines) <= READ_PREVIEW_LINES:
                return "\n".join(lines)
            kept = "\n".join(lines[:READ_PREVIEW_LINES])
            return kept + self._styled("muted", f"\n... ({len(lines) - READ_PREVIEW_LINES} more lines)")
        first = lines[0] if lines else ""
        return (first[:120] + "...") if len(first) > 120 else first

    def _styled(self, color: str, text: str) -> str:
        if self._theme is not None:
            return self._theme.fg(color, text)
        return text

    def _bg(self, color: str) -> "Callable[[str], str]":
        """Return a ``bg_fn`` closure for the given theme token.

        Mirrors :meth:`_styled` but yields the background wrapper expected by
        :class:`Box`. Falls back to identity when no theme is configured.
        """
        if self._theme is None:
            return lambda t: t
        return lambda t: self._theme.bg(color, t)


def _format_args(args: dict) -> str:
    """Format tool arguments as a single-line preview."""
    try:
        s = json.dumps(args, ensure_ascii=False)
    except Exception:
        s = str(args)
    if len(s) > 80:
        s = s[:77] + "..."
    return s
