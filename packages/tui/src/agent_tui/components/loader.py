"""Loader component.

Animated spinner + message that drives its own render cycle via
``tui.request_render()``. Extends Text. Uses an asyncio task for the frame
timer scheduled on the active event loop.

"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable

from agent_tui.components.text import Text

if TYPE_CHECKING:
    from agent_tui.tui import TUI

#: Braille spinner frames.
DEFAULT_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
#: Frame interval in ms.
DEFAULT_INTERVAL_MS = 80


class LoaderIndicatorOptions:
    """Animation config: custom frames and/or interval."""

    def __init__(
        self,
        frames: "list[str] | None" = None,
        interval_ms: "int | None" = None,
    ) -> None:
        self.frames = frames
        self.interval_ms = interval_ms


class Loader(Text):
    """Animated loading spinner + message.

    Extends :class:`Text`, prepending a spinning frame to the message. The
    spinner advances every ``interval_ms`` via an asyncio task that calls
    ``tui.request_render()`` each frame.

    Args:
        tui: The TUI instance (for request_render).
        spinner_color_fn: Styles the spinner frame.
        message_color_fn: Styles the message text.
        message: The status message.
        indicator: Optional custom frames/interval.
    """

    def __init__(
        self,
        tui: "TUI",
        spinner_color_fn: Callable[[str], str],
        message_color_fn: Callable[[str], str],
        message: str = "加载中...",
        indicator: LoaderIndicatorOptions | None = None,
    ) -> None:
        # Text base: padding_x=1, padding_y=0.
        super().__init__("", padding_x=1, padding_y=0)
        self._tui = tui
        self._spinner_color_fn = spinner_color_fn
        self._message_color_fn = message_color_fn
        self._message = message
        self._render_indicator_verbatim = False

        # Indicator config.
        self._frames = list(DEFAULT_FRAMES)
        self._interval_ms = DEFAULT_INTERVAL_MS
        self._current_frame = 0
        self._timer_handle: "asyncio.TimerHandle | None" = None

        self.set_indicator(indicator)

    def render(self, width: int) -> list[str]:
        # Loader prepends a blank line before the spinner line.
        return ["", *super().render(width)]

    def start(self) -> None:
        """Start the loader: initial display + animation."""
        self._update_display()
        self._restart_animation()

    def stop(self) -> None:
        """Stop the animation timer."""
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None

    def set_message(self, message: str) -> None:
        """Update the status message."""
        self._message = message
        self._update_display()

    def set_indicator(self, indicator: LoaderIndicatorOptions | None = None) -> None:
        """Set custom frames/interval."""
        self._render_indicator_verbatim = indicator is not None
        if indicator and indicator.frames is not None:
            self._frames = list(indicator.frames)
        else:
            self._frames = list(DEFAULT_FRAMES)
        if indicator and indicator.interval_ms and indicator.interval_ms > 0:
            self._interval_ms = indicator.interval_ms
        else:
            self._interval_ms = DEFAULT_INTERVAL_MS
        self._current_frame = 0
        self.start()

    def _restart_animation(self) -> None:
        """Cancel existing timer and start a new one."""
        self.stop()
        if len(self._frames) <= 1:
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return  # No running loop (e.g. constructing outside async).
        self._timer_handle = loop.call_later(
            self._interval_ms / 1000.0,
            self._tick,
        )

    def _tick(self) -> None:
        """Advance the frame and reschedule."""
        self._current_frame = (self._current_frame + 1) % len(self._frames)
        self._update_display()
        # Reschedule.
        try:
            loop = asyncio.get_event_loop()
            self._timer_handle = loop.call_later(
                self._interval_ms / 1000.0,
                self._tick,
            )
        except RuntimeError:
            pass

    def _update_display(self) -> None:
        """Rebuild the Text content from frame + message."""
        frame = self._frames[self._current_frame] if self._frames else ""
        rendered_frame = frame if self._render_indicator_verbatim else self._spinner_color_fn(frame)
        indicator = f"{rendered_frame} " if frame else ""
        self.set_text(f"{indicator}{self._message_color_fn(self._message)}")
        if self._tui is not None:
            self._tui.request_render()
