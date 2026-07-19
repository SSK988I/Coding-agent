"""FooterComponent — status bar showing cwd, git branch, model, thinking, tokens.

Renders a compact single-line status bar at the bottom of the TUI with session
statistics:

    ``<cwd> (<branch>) | <model> • thinking <level> | in/out/ctx% | $cost``

Layout choices (see plan):
  - git branch is resolved via a subprocess and cached on the instance; the
    caller calls ``refresh()`` on meaningful events (model/thinking change,
    new message) rather than every frame.
  - context-window usage is the *last* assistant message's ``usage.input``
    divided by ``model.context_window`` (NOT the cumulative session total,
    which would only ever grow past 100%). Color-coded: <70% normal,
    <90% warning, >=90% error.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from agent_tui import Component
from agent_tui.theme import Theme


class FooterComponent(Component):
    """One-line status bar at the bottom of the TUI."""

    def __init__(self, session: Any, theme: Theme) -> None:
        self._session = session
        self._theme = theme
        self._git_branch: "str | None" = None
        # Refresh once at construction so the first render has a branch.
        self.refresh_git_branch()

    # ── public ──────────────────────────────────────────────────────────

    def refresh_git_branch(self) -> None:
        """Re-resolve the current git branch (call on cwd/turn-change events).

        Cheap enough to call per-event, but NOT per-frame (it spawns git).
        Failure (not a repo, git missing) leaves ``_git_branch`` as None.
        """
        cwd = getattr(self._session, "cwd", None)
        if not cwd:
            self._git_branch = None
            return
        try:
            result = subprocess.run(
                ["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=1.0,
            )
            if result.returncode == 0:
                self._git_branch = result.stdout.strip() or None
            else:
                self._git_branch = None
        except (OSError, subprocess.TimeoutExpired):
            self._git_branch = None

    def invalidate(self) -> None:
        pass  # Stateless render (reads session fresh each frame).

    # ── render ──────────────────────────────────────────────────────────

    def render(self, width: int) -> list[str]:
        stats = self._session.get_stats()
        model = self._session.model

        cwd = self._format_cwd(getattr(self._session, "cwd", "."))
        # Append git branch in parens if present.
        if self._git_branch:
            left = f"{cwd} ({self._git_branch})"
        else:
            left = cwd

        # Model + thinking level.
        model_id = model.id if model else "?"
        model_part = model_id
        thinking_level = self._session.thinking_level
        is_reasoning = bool(getattr(model, "reasoning", False)) if model else False
        if is_reasoning:
            lvl = thinking_level or "off"
            model_part = f"{model_id} • thinking {lvl}"

        # Token stats + context usage.
        token_part = ""
        if stats.tokens.total > 0:
            token_part = (
                f"in={_fmt_num(stats.tokens.input)} "
                f"out={_fmt_num(stats.tokens.output)}"
            )
            ctx = self._context_percent(model, stats)
            if ctx is not None:
                token_part += f" · ctx {ctx:.0f}%"

        cost_part = f"${stats.cost:.4f}" if stats.cost > 0 else ""

        # Join with separators, then color and truncate.
        parts = [p for p in (left, model_part, token_part, cost_part) if p]
        line = " | ".join(parts)

        # Colorize the context marker if present.
        line = self._colorize_context(line, model, stats)

        if len(line) > width:
            # Plain truncation (post-color is messy; truncate the joined text
            # before colorize instead — but ctx coloring is applied above on
            # the full line, so fall back to a simple width cut).
            line = line[:width]
        else:
            line = line.ljust(width)
        return [self._theme.fg("dim", line)]

    # ── helpers ─────────────────────────────────────────────────────────

    def _context_percent(self, model: Any, stats: Any) -> "float | None":
        """Current context-window occupancy as a percentage.

        Uses the *last* assistant message's input token count as the
        numerator (that reflects the context size at the most recent call),
        NOT the cumulative session total. Falls back to None when unknown.
        """
        cw = getattr(model, "context_window", 0) if model else 0
        if not cw:
            return None
        last_input = self._last_input_tokens()
        if last_input is None:
            return None
        return (last_input / cw) * 100.0

    def _last_input_tokens(self) -> "int | None":
        """Input tokens of the most recent assistant message, or None."""
        try:
            entries = self._session.session_manager.get_branch()
        except Exception:
            return None

        for entry in reversed(entries):
            message = getattr(entry, "message", None)
            if message is None:
                continue
            if getattr(message, "role", None) != "assistant":
                continue
            usage = getattr(message, "usage", None)
            if usage is not None:
                return int(getattr(usage, "input", 0) or 0)
        return None

    def _colorize_context(self, line: str, model: Any, stats: Any) -> str:
        """Wrap the ``ctx NN%`` token in warning/error color by threshold."""
        pct = self._context_percent(model, stats)
        if pct is None:
            return line
        # Build the raw token (matches what render() wrote) and recolor.
        raw = f"ctx {pct:.0f}%"
        if raw not in line:
            return line
        if pct >= 90:
            colored = self._theme.fg("error", raw)
        elif pct >= 70:
            colored = self._theme.fg("warning", raw)
        else:
            # Already inside a dim line; leave as-is.
            return line
        return line.replace(raw, colored, 1)

    @staticmethod
    def _format_cwd(cwd: str) -> str:
        """Shorten the home directory to ``~``."""
        try:
            home = str(Path.home())
            if cwd.startswith(home):
                return "~" + cwd[len(home):]
        except Exception:
            pass
        return cwd or "."


def _fmt_num(n: int) -> str:
    """Format a number with k/m suffix for readability."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)
