"""Status footer using the session's shared cache/context metrics.

Wide terminals retain an inline status bar. Narrow terminals give metrics
additional rows so neither Plan recovery nor permission indicators disappear.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from agent_tui import Component
from agent_tui.theme import Theme
from agent_tui.utils import truncate_to_width, visible_width, wrap_text_with_ansi


class FooterComponent(Component):
    """Responsive status bar at the bottom of the TUI."""

    def __init__(self, session: Any, theme: Theme) -> None:
        self._session = session
        self._theme = theme
        self._git_branch: "str | None" = None
        self._stats_snapshot: Any | None = None
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
        self._stats_snapshot = None

    # ── render ──────────────────────────────────────────────────────────

    def render(self, width: int) -> list[str]:
        if width <= 0:
            return [""]
        stats = self._stats_snapshot
        if stats is None:
            stats = self._session.get_stats()
            self._stats_snapshot = stats
        metrics = stats.metrics
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
        mode_part = self._plan_label()
        permission_mode = getattr(self._session, "permission_mode", None)
        permission_part = (
            "bypass permissions on" if permission_mode == "full_access"
            else "permissions auto" if permission_mode == "auto"
            else "permissions ask" if permission_mode == "ask" else ""
        )

        # Ratios and current context are projected centrally. In particular,
        # cumulative input includes cache reads/writes; context is not a sum.
        cache_rate = metrics.cache_hit_rate
        cache_part = f"cache {cache_rate:.1f}%" if cache_rate is not None else "cache —"
        context_part = _context_label(metrics)
        token_part = (
            f"in={_fmt_num(metrics.input_tokens)} "
            f"out={_fmt_num(stats.tokens.output)}"
        )
        cost_part = f"${stats.cost:.4f}" if stats.cost > 0 else ""
        if visible_width(" | ".join(p for p in (mode_part, permission_part) if p)) > width:
            permission_part = "bypass on" if permission_mode == "full_access" else permission_part
        header_parts = [p for p in (left, mode_part, permission_part, model_part) if p]
        metric_parts = [p for p in (cache_part, context_part, token_part, cost_part) if p]
        full_line = " | ".join(header_parts + metric_parts)
        if visible_width(full_line) <= width:
            rows = [full_line]
        else:
            header = " | ".join(header_parts)
            status = " | ".join(p for p in (mode_part, permission_part) if p)
            if visible_width(header) <= width:
                rows = [header]
            elif status and visible_width(status) <= width:
                available = width - visible_width(status) - 3
                rows = [
                    f"{truncate_to_width(left, available)} | {status}"
                    if available > 0 else status
                ]
            elif status:
                rows = _pack_parts([p for p in (mode_part, permission_part) if p], width)
            else:
                rows = [truncate_to_width(header, width)]
            rows.extend(_pack_parts(metric_parts, width))

        rendered = []
        for row in rows:
            # Color only after layout; ANSI escape sequences are never sliced
            # as text and CJK project paths occupy their real terminal width.
            row = self._colorize_context(row, metrics, context_part)
            if permission_mode == "full_access" and permission_part:
                row = row.replace(permission_part, self._theme.fg("warning", permission_part))
            rendered.append(self._theme.fg("dim", truncate_to_width(row, width, pad=True)))
        return rendered

    # ── helpers ─────────────────────────────────────────────────────────

    def _plan_label(self) -> str:
        """Return the compact, state-aware Plan indicator.

        ``uncertain`` and ``recovery_error`` remain visible even though the
        runtime is fail-closed in Default mode, so reopening a session never
        hides the fact that user action is required.
        """
        state = getattr(self._session, "plan_state", None)
        phase = getattr(state, "phase", None)
        if phase == "ready":
            return "plan ready"
        if phase == "uncertain":
            return "plan uncertain"
        if phase == "recovery_error":
            return "plan recovery"
        if phase in {"drafting", "awaiting_answer", "executing"}:
            return "plan"
        if getattr(self._session, "collaboration_mode", "default") == "plan":
            return "plan"
        return ""

    def _colorize_context(self, line: str, metrics: Any, raw: str) -> str:
        """Highlight context capacity at 70% (warning) / 90% (error)."""
        pct = metrics.context_percent
        if pct is None:
            return line
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


def _context_label(metrics: Any) -> str:
    """Format, without recomputing, the shared context occupancy snapshot."""
    used = metrics.context_tokens
    limit = metrics.context_window
    pct = metrics.context_percent
    estimated = "~" if metrics.context_estimated and used is not None else ""
    capacity = f"{_fmt_num(used) if used is not None else '—'}/{_fmt_num(limit) if limit else '—'}"
    percentage = f" ({pct:.1f}%)" if pct is not None else ""
    return f"ctx {estimated}{capacity}{percentage}"


def _pack_parts(parts: list[str], width: int) -> list[str]:
    """Wrap complete status groups where possible, without dropping metrics."""
    rows: list[str] = []
    current = ""
    for part in parts:
        candidate = f"{current} | {part}" if current else part
        if visible_width(candidate) <= width:
            current = candidate
            continue
        if current:
            rows.append(current)
            current = ""
        wrapped = wrap_text_with_ansi(part, width)
        rows.extend(wrapped[:-1])
        current = wrapped[-1]
    if current:
        rows.append(current)
    return rows
