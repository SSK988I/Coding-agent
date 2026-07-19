"""Status indicators for the interactive mode.

Loader subclasses that distinguish working / retry / compaction / branchSummary
states via a ``kind`` tag, plus an ``IdleStatus`` placeholder that renders two
blank lines so the status row's height stays stable.

"""
from __future__ import annotations

from typing import Any, Literal

from agent_tui import Component, Loader

StatusIndicatorKind = Literal["working", "retry", "compaction", "branchSummary"]
CompactionStatusReason = Literal["manual", "threshold", "overflow"]

#: Hint shown next to cancellable operations. Resolved from the keybinding
#: table so it stays in sync with the configured interrupt key.
def _interrupt_hint() -> str:
    try:
        from coding_agent.core.keybindings import key_text
        key = key_text("app.interrupt")
        return f"({key} to cancel)" if key else "(interrupt to cancel)"
    except Exception:
        return "(Esc to cancel)"


_INTERRUPT_HINT = _interrupt_hint()


class StatusIndicator(Loader):
    """A Loader tagged with a ``kind`` for kind-guarded clear logic."""

    def __init__(
        self,
        kind: StatusIndicatorKind,
        tui: Any,
        spinner_color_fn,
        message_color_fn,
        message: str,
        indicator=None,
    ) -> None:
        super().__init__(tui, spinner_color_fn, message_color_fn, message, indicator)
        self.kind = kind

    def dispose(self) -> None:
        self.stop()


class WorkingStatusIndicator(StatusIndicator):
    """Spinner shown while the agent is actively working."""

    def __init__(self, tui: Any, message: str = "Working...", theme: Any | None = None) -> None:
        spinner = (lambda s, _t=theme: _t.fg("accent", s)) if theme is not None else (lambda s: s)
        text = (lambda s, _t=theme: _t.fg("muted", s)) if theme is not None else (lambda s: s)
        super().__init__("working", tui, spinner, text, message)


class CompactionStatusIndicator(StatusIndicator):
    """Spinner shown while the session is being compacted."""

    def __init__(self, tui: Any, reason: CompactionStatusReason, theme: Any | None = None) -> None:
        if reason == "manual":
            label = f"Compacting context... {_INTERRUPT_HINT}"
        else:
            prefix = "Context overflow detected, " if reason == "overflow" else ""
            label = f"{prefix}Auto-compacting... {_INTERRUPT_HINT}"
        spinner = (lambda s, _t=theme: _t.fg("accent", s)) if theme is not None else (lambda s: s)
        text = (lambda s, _t=theme: _t.fg("muted", s)) if theme is not None else (lambda s: s)
        super().__init__("compaction", tui, spinner, text, label)


class RetryStatusIndicator(StatusIndicator):
    """Spinner shown while waiting to retry a transient provider failure."""

    def __init__(
        self,
        tui: Any,
        attempt: int,
        max_retries: int,
        delay: float,
        theme: Any | None = None,
    ) -> None:
        label = f"Request failed temporarily; retry {attempt}/{max_retries} in {delay:g}s... {_INTERRUPT_HINT}"
        spinner = (lambda s, _t=theme: _t.fg("warning", s)) if theme is not None else (lambda s: s)
        text = (lambda s, _t=theme: _t.fg("muted", s)) if theme is not None else (lambda s: s)
        super().__init__("retry", tui, spinner, text, label)


# BranchSummaryStatusIndicator is available to callers that enable branch summarization.


class BranchSummaryStatusIndicator(StatusIndicator):
    """Spinner shown while summarizing an abandoned branch."""

    def __init__(self, tui: Any, theme: Any | None = None) -> None:
        spinner = (lambda s, _t=theme: _t.fg("accent", s)) if theme is not None else (lambda s: s)
        text = (lambda s, _t=theme: _t.fg("muted", s)) if theme is not None else (lambda s: s)
        super().__init__(
            "branchSummary", tui, spinner, text,
            f"Summarizing branch... {_INTERRUPT_HINT}",
        )


class IdleStatus(Component):
    """Renders two blank lines to preserve the status row's height.

    This prevents a shrinking chat from pulling the editor upward after an
    indicator is cleared with ``clearOnShrink`` enabled.
    """

    def invalidate(self) -> None:  # noqa: D401 - no cached state
        pass

    def render(self, width: int) -> list[str]:
        empty = " " * width
        return [empty, empty]
