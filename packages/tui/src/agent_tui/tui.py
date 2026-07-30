"""TUI core with differential rendering.

The heart of the TUI: a retain-mode component tree where every component
implements ``render(width) -> list[str]``, and a differential renderer that
only writes the lines that changed since the last frame.

Render model: ``request_render()`` coalesces via a flag and schedules a render
on the event loop tick, throttled to MIN_RENDER_INTERVAL_MS (16ms). ``do_render``
walks the whole tree to get ``new_lines``, diffs against ``_previous_lines``,
finds first_changed/last_changed, and writes only those lines wrapped in
synchronized output (``\x1b[?2026h ... \x1b[?2026l``).

The core covers differential rendering, focus management, and input dispatch.
Overlay composition is intentionally outside the renderer API.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Callable, List, Protocol

from agent_tui.keys import is_key_release
from agent_tui.terminal import Terminal
from agent_tui.utils import (
    extract_ansi_code,
    normalize_terminal_output,
    truncate_to_width,
    visible_width,
)


# ─── Component interface ────────────────────────────────

class Component(ABC):
    """Abstract base for all TUI components.

    Each component implements ``render(width)`` returning a list of lines
    (one per terminal row). Components optionally handle keyboard input and
    invalidate cached rendering state.
    """

    @abstractmethod
    def render(self, width: int) -> List[str]:
        """Render to lines for the given viewport width.

        Lines should not exceed ``width`` visible columns; the renderer guards
        against overflow by crashing with a diagnostic.
        """
        ...

    def handle_input(self, data: str) -> bool:
        """Handle keyboard input when this component has focus.

        Return True if consumed. Default: not consumed.
        """
        return False

    @property
    def wants_key_release(self) -> bool:
        """If True, the component receives Kitty key-release events.

        Default False — release events are filtered out.
        """
        return False

    def invalidate(self) -> None:
        """Invalidate cached rendering state.

        Called on theme change or when the component needs a full re-render.
        """
        pass


class Focusable(Protocol):
    """Components that can receive focus and display a hardware cursor.

    When focused, the TUI sets ``focused = True``; the component should then
    emit CURSOR_MARKER at the cursor position in its render output.
    """
    focused: bool


def is_focusable(component: "Component | None") -> bool:
    """Type guard: does component implement Focusable?."""
    return component is not None and hasattr(component, "focused")


#: Box-drawing chars that begin a rendered table line (after stripping ANSI
#: and leading background fill). Used by the width-overflow guard to preserve
#: the correct closing border while clipping an overwide row.
_TABLE_LINE_PREFIXES = frozenset("┌├└│")
_TABLE_LINE_CLOSERS = {
    "┌": "┐",
    "├": "┤",
    "└": "┘",
    "│": "│",
}


def _first_visible_non_space(line: str) -> str | None:
    """Return the first visible non-space character in an ANSI string."""
    i = 0
    n = len(line)
    while i < n:
        ansi = extract_ansi_code(line, i)
        if ansi:
            i += ansi[1]
            continue
        ch = line[i]
        if ch.isspace():
            i += 1
            continue
        return ch
    return None


def _is_table_line(line: str) -> bool:
    """True if ``line`` looks like a rendered markdown table row/border."""
    return _first_visible_non_space(line) in _TABLE_LINE_PREFIXES


def _fit_line_to_width(line: str, width: int) -> str:
    """Clip a rendered line without ever allowing a physical terminal wrap.

    Markdown table rows retain a closing border by reserving their last column;
    every returned line is nevertheless guaranteed to fit ``width``.
    """
    if visible_width(line) <= width:
        return line
    first = _first_visible_non_space(line)
    closer = _TABLE_LINE_CLOSERS.get(first or "")
    if closer and width > 0:
        return truncate_to_width(
            line,
            max(0, width - 1),
            pad=True,
        ) + closer
    return truncate_to_width(line, width)


#: Zero-width cursor position marker (APC sequence).
#: Components emit this at the cursor position when focused; the TUI finds
#: it, strips it, and positions the hardware cursor there for IME.
CURSOR_MARKER = "\x1b_agent:c\x07"


# ─── Container ────────────────────────────────────────

class Container(Component):
    """A component that holds and renders children.

    Children render in order; their lines are concatenated vertically.
    Layout is purely vertical stacking — no horizontal/flex/grid.
    """

    def __init__(self) -> None:
        self.children: list[Component] = []

    def add_child(self, component: Component) -> None:
        self.children.append(component)

    def remove_child(self, component: Component) -> None:
        if component in self.children:
            self.children.remove(component)

    def clear(self) -> None:
        self.children.clear()

    def insert_before(self, reference: Component, component: Component) -> None:
        try:
            idx = self.children.index(reference)
            self.children.insert(idx, component)
        except ValueError:
            self.children.append(component)

    def insert_after(self, reference: Component, component: Component) -> None:
        """Insert ``component`` immediately after ``reference`` (or append)."""
        try:
            idx = self.children.index(reference)
            self.children.insert(idx + 1, component)
        except ValueError:
            self.children.append(component)

    def invalidate(self) -> None:
        for child in self.children:
            child.invalidate()

    def render(self, width: int) -> List[str]:
        lines: list[str] = []
        for child in self.children:
            lines.extend(child.render(width))
        return lines


# ─── Input listener ─────────────────────────────────────

InputListenerResult = dict  # {"consume": bool, "data": str} or None
InputListener = Callable[[str], "InputListenerResult | None"]


# ─── TUI ─────────────────────────────────────────────

#: Minimum interval between renders. Throttle coalesces bursts.
MIN_RENDER_INTERVAL_MS = 16

#: Line reset appended to every non-image line.
SEGMENT_RESET = "\x1b[0m\x1b]8;;\x07"


class TUI(Container):
    """Main TUI orchestrator with differential rendering.

    Manages the component tree, input dispatch, focus, and the render loop.
    The differential renderer only writes lines that changed since the last
    frame, wrapped in synchronized output to prevent tearing.
    """

    def __init__(self, terminal: Terminal, show_hardware_cursor: bool = False) -> None:
        super().__init__()
        self.terminal = terminal
        self._show_hardware_cursor = show_hardware_cursor

        # The main event loop. Captured in start() so cross-thread callers
        # (the stdin reader thread) can schedule renders safely.
        self._loop: "asyncio.AbstractEventLoop | None" = None

        # Render state.
        self._previous_lines: list[str] = []
        self._previous_width = 0
        self._previous_height = 0
        self._render_requested = False
        self._render_timer: "asyncio.TimerHandle | None" = None
        self._last_render_at = 0.0
        self._stopped = True
        self._full_redraw_count = 0
        self._max_lines_rendered = 0

        # Focus + input.
        self._focused: "Component | None" = None
        self._input_listeners: list[InputListener] = []
        self._cursor_row = 0
        self._hardware_cursor_row = 0
        # Logical row at the top of the terminal viewport after the previous
        # frame.  Once content is taller than the terminal, logical row
        # differences are no longer the same thing as physical cursor moves.
        self._previous_viewport_top = 0

        # Debug.
        self.on_debug: "Callable[[], None] | None" = None

    # ── public properties ──────────────────────────────────────────────

    @property
    def full_redraws(self) -> int:
        return self._full_redraw_count

    @property
    def show_hardware_cursor(self) -> bool:
        return self._show_hardware_cursor

    def set_show_hardware_cursor(self, enabled: bool) -> None:
        if self._show_hardware_cursor == enabled:
            return
        self._show_hardware_cursor = enabled
        if not enabled:
            self.terminal.hide_cursor()
        self.request_render()

    # ── focus ─────────────────────────────────────────

    def set_focus(self, component: "Component | None") -> None:
        """Set keyboard focus to a component."""
        if is_focusable(self._focused):
            self._focused.focused = False  # type: ignore[attr-defined]
        self._focused = component
        if is_focusable(component):
            component.focused = True  # type: ignore[attr-defined]
        self.request_render()

    def clear_focus(self) -> None:
        self.set_focus(None)

    @property
    def focused(self) -> "Component | None":
        return self._focused

    # ── input listeners ───────────────────────────────

    def add_input_listener(self, listener: InputListener) -> Callable[[], None]:
        """Add a pre-focus input hook. Returns an unsubscribe fn.

        Listeners can consume (return ``{"consume": True}``) or transform
        (return ``{"data": new_data}``) input before it reaches the focused
        component.
        """
        self._input_listeners.append(listener)
        return lambda: self._input_listeners.remove(listener) if listener in self._input_listeners else None

    def remove_input_listener(self, listener: InputListener) -> None:
        if listener in self._input_listeners:
            self._input_listeners.remove(listener)

    # ── lifecycle ─────────────────────────────────────

    def start(self) -> None:
        """Start the TUI: enter raw mode, hide cursor, render first frame."""
        self._stopped = False
        # Capture the running event loop so the stdin reader thread (which
        # calls _handle_input → request_render) can schedule safely.
        self._loop = asyncio.get_event_loop()
        self.terminal.start(self._handle_input, self.request_render)
        self.terminal.hide_cursor()
        # Force a render: request_render may have been called before start()
        # (while _stopped was True), leaving _render_requested=True but no
        # timer scheduled. force=True clears that stale state and schedules
        # a fresh render on the next event-loop tick.
        self.request_render(force=True)

    def stop(self) -> None:
        """Stop the TUI: move cursor to end, show cursor, restore terminal."""
        self._stopped = True
        if self._render_timer:
            self._render_timer.cancel()
            self._render_timer = None
        # Move cursor to end of content to avoid overwriting on exit.
        if self._previous_lines:
            line_diff = len(self._previous_lines) - self._hardware_cursor_row
            if line_diff > 0:
                self.terminal.write(f"\x1b[{line_diff}B")
            elif line_diff < 0:
                self.terminal.write(f"\x1b[{-line_diff}A")
            self.terminal.write("\r\n")
        self.terminal.show_cursor()
        self.terminal.stop()

    # ── render scheduling ─────────────────────────────

    def request_render(self, force: bool = False) -> None:
        """Request a re-render, throttled to MIN_RENDER_INTERVAL_MS.

        Coalesces via ``_render_requested``: a burst of calls collapses into
        one render on the next event-loop tick. ``force=True`` clears all
        state and triggers a full redraw.

        Thread-safe: may be called from the stdin reader thread. Detects the
        calling thread and uses ``call_soon_threadsafe`` when off-loop.
        """
        # If we're not on the loop thread, hop over before touching state.
        # In a non-loop thread asyncio.get_event_loop() raises RuntimeError,
        # which also means we're off-loop — treat that as "needs hop".
        if self._loop is not None:
            try:
                current = asyncio.get_event_loop()
            except RuntimeError:
                current = None
            if current is not self._loop:
                self._loop.call_soon_threadsafe(self.request_render, force)
                return

        if force:
            self._previous_lines = []
            self._previous_width = -1
            self._previous_height = -1
            self._cursor_row = 0
            self._hardware_cursor_row = 0
            self._previous_viewport_top = 0
            self._max_lines_rendered = 0
            if self._render_timer:
                self._render_timer.cancel()
                self._render_timer = None
            self._render_requested = True
            self._schedule_render_now()
            return
        if self._render_requested:
            return
        self._render_requested = True
        self._schedule_render()

    def _schedule_render_now(self) -> None:
        loop = self._loop or asyncio.get_event_loop()
        loop.call_soon(self._do_scheduled_render)

    def _schedule_render(self) -> None:
        if self._stopped or self._render_timer or not self._render_requested:
            return
        loop = self._loop or asyncio.get_event_loop()
        elapsed = (time.monotonic() - self._last_render_at) * 1000
        delay = max(0.0, MIN_RENDER_INTERVAL_MS - elapsed) / 1000.0
        self._render_timer = loop.call_later(delay, self._do_scheduled_render)

    def _do_scheduled_render(self) -> None:
        self._render_timer = None
        if self._stopped or not self._render_requested:
            return
        self._render_requested = False
        self._last_render_at = time.monotonic()
        self._do_render()
        if self._render_requested:
            self._schedule_render()

    def render_now(self) -> None:
        """Render synchronously, ignoring the throttle.

        Used by callers that need each token batch to appear immediately (e.g.
        streaming assistant deltas) where waiting for the throttled
        ``call_later(16ms)`` timer would coalesce a whole burst into one frame.

        Cancels any pending throttled render and runs one immediately, then
        updates ``_last_render_at`` so the next ``request_render`` still
        respects the throttle.
        """
        if self._loop is not None:
            try:
                current = asyncio.get_event_loop()
            except RuntimeError:
                current = None
            if current is not self._loop:
                self._loop.call_soon_threadsafe(self.render_now)
                return
        if self._stopped:
            return
        if self._render_timer:
            self._render_timer.cancel()
            self._render_timer = None
        self._render_requested = False
        self._last_render_at = time.monotonic()
        self._do_render()

    # ─── input handling ───────────────────────────────

    def _handle_input(self, data: str) -> None:
        """Dispatch raw input through listeners then to the focused component."""
        # Run input listeners (consume/transform).
        if self._input_listeners:
            current = data
            for listener in self._input_listeners:
                result = listener(current)
                if result and result.get("consume"):
                    return
                if result and "data" in result:
                    current = result["data"]
            if not current:
                return
            data = current

        # Pass to focused component (filter release events unless opted in).
        if self._focused is not None:
            if is_key_release(data) and not getattr(self._focused, "wants_key_release", False):
                return
            self._focused.handle_input(data)
            self.request_render()

    # ─── differential rendering ─────────────────────

    def _do_render(self) -> None:
        """Render the tree and write only changed lines.

        The core differential algorithm:
        1. Render all children → new_lines.
        2. Apply line resets (normalize + SEGMENT_RESET).
        3. Find first_changed/last_changed by comparing to _previous_lines.
        4. If no changes: just reposition hardware cursor.
        5. Else: move cursor to first_changed, write changed lines, wrap in
           synchronized output (\x1b[?2026h ... \x1b[?2026l).
        6. Full redraw triggers: first frame, width change, height change.
        7. Width-overflow crash guard.
        """
        if self._stopped:
            return
        terminal_width = self.terminal.columns
        delayed_wrap_supported = bool(
            getattr(self.terminal, "delayed_wrap_supported", True)
        )
        # A terminal that immediately wraps on the last column advances one
        # physical row before our following CRLF, desynchronising the renderer.
        # Reserve the final column when Windows could not enable delayed wrap.
        width = (
            terminal_width
            if delayed_wrap_supported
            else max(1, terminal_width - 1)
        )
        height = self.terminal.rows

        width_changed = self._previous_width != 0 and self._previous_width != width
        height_changed = self._previous_height != 0 and self._previous_height != height
        previous_buffer_length = (
            self._previous_viewport_top + self._previous_height
            if self._previous_height > 0
            else height
        )
        prev_viewport_top = (
            max(0, previous_buffer_length - height)
            if height_changed
            else self._previous_viewport_top
        )
        viewport_top = prev_viewport_top
        hardware_cursor_row = self._hardware_cursor_row

        def compute_line_diff(target_row: int) -> int:
            """Return the physical screen-row delta for a logical target."""
            current_screen_row = hardware_cursor_row - prev_viewport_top
            target_screen_row = target_row - viewport_top
            return target_screen_row - current_screen_row

        # Render the tree.
        new_lines = self.render(width)

        # Extract CURSOR_MARKER before applying line resets (the marker must
        # be found before _apply_line_resets appends SEGMENT_RESET, which
        # would shift its visible column). Strips the marker in place.
        theoretical_viewport_top = max(0, len(new_lines) - height)
        cursor_viewport_top = (
            theoretical_viewport_top
            if not self._previous_lines or width_changed or height_changed
            else max(prev_viewport_top, theoretical_viewport_top)
        )
        cursor_pos = self._extract_cursor_position(
            new_lines,
            height,
            viewport_top=cursor_viewport_top,
        )

        # Width-overflow guard. Components are expected to honor the supplied
        # width, but clip defensively before *every* output path, including the
        # first/full render. Even with delayed wrap, width+1 would cause a real
        # wrap followed by our CRLF and desynchronise physical/logical rows.
        for i, line in enumerate(new_lines):
            new_lines[i] = _fit_line_to_width(line, width)

        # Apply line resets after clipping so truncated hyperlinks/styles are
        # always closed by the same reset sequence as normal lines.
        new_lines = self._apply_line_resets(new_lines)

        # Full render helper.
        def full_render(clear: bool) -> None:
            self._full_redraw_count += 1
            buf = "\x1b[?2026h"  # Begin synchronized output.
            if clear:
                buf += "\x1b[2J\x1b[H\x1b[3J"
            for i, line in enumerate(new_lines):
                if i > 0:
                    buf += "\r\n"
                buf += line
            buf += "\x1b[?2026l"  # End synchronized output.
            self.terminal.write(buf)
            self.terminal.flush()
            self._cursor_row = max(0, len(new_lines) - 1)
            self._hardware_cursor_row = self._cursor_row
            self._max_lines_rendered = max(self._max_lines_rendered, len(new_lines))
            buffer_length = max(height, len(new_lines))
            self._previous_viewport_top = max(0, buffer_length - height)
            self._previous_lines = new_lines
            self._previous_width = width
            self._previous_height = height
            # Reposition hardware cursor for IME (must run AFTER the buffer write).
            self._position_hardware_cursor(cursor_pos, len(new_lines))

        # First render: output everything, homing the cursor first so the
        # first frame always starts at row 0 (fixes "initial render garbled"
        # where the welcome banner would write from whatever column the
        # terminal cursor happened to be on).
        if not self._previous_lines and not width_changed and not height_changed:
            buf = "\x1b[?2026h\x1b[H"  # Begin synchronized output + home cursor.
            for i, line in enumerate(new_lines):
                if i > 0:
                    buf += "\r\n"
                buf += line
            buf += "\x1b[?2026l"
            self.terminal.write(buf)
            self.terminal.flush()
            self._full_redraw_count += 1
            self._cursor_row = max(0, len(new_lines) - 1)
            self._hardware_cursor_row = self._cursor_row
            self._max_lines_rendered = max(self._max_lines_rendered, len(new_lines))
            buffer_length = max(height, len(new_lines))
            self._previous_viewport_top = max(0, buffer_length - height)
            self._previous_lines = new_lines
            self._previous_width = width
            self._previous_height = height
            # Reposition hardware cursor for IME (after the buffer write).
            self._position_hardware_cursor(cursor_pos, len(new_lines))
            return

        # Width/height changes need a full re-render.
        if width_changed or height_changed:
            full_render(True)
            return

        # NOTE: We deliberately do NOT full-render when content shrunk below
        # _max_lines_rendered. The incremental path below already handles
        # trailing-line cleanup via _write_deleted_lines / the shrink branch.
        # A full clear (\x1b[2J\x1b[3J) here was the primary
        # source of streaming flicker — Markdown height fluctuates per delta,
        # and any frame with fewer lines than the high-water mark triggered an
        # entire screen erase-and-redraw. Only
        # full-redraws on width/height change, not on content shrink.

        # Find first and last changed lines.
        first_changed = -1
        last_changed = -1
        max_lines = max(len(new_lines), len(self._previous_lines))
        for i in range(max_lines):
            old = self._previous_lines[i] if i < len(self._previous_lines) else ""
            new = new_lines[i] if i < len(new_lines) else ""
            if old != new:
                if first_changed == -1:
                    first_changed = i
                last_changed = i

        # Appended lines.
        appended = len(new_lines) > len(self._previous_lines)
        if appended:
            if first_changed == -1:
                first_changed = len(self._previous_lines)
            last_changed = len(new_lines) - 1
        append_start = (
            appended
            and first_changed == len(self._previous_lines)
            and first_changed > 0
        )

        # No changes.
        if first_changed == -1:
            self._previous_viewport_top = prev_viewport_top
            self._previous_height = height
            # Even with no content change, the IME cursor position may have
            # moved within the editor (e.g. arrow keys) — reposition it.
            self._position_hardware_cursor(cursor_pos, len(new_lines))
            return

        # All changes in deleted lines (content shrunk): clear extra lines.
        if first_changed >= len(new_lines):
            if not self._write_deleted_lines(
                new_lines,
                height=height,
                viewport_top=prev_viewport_top,
            ):
                full_render(True)
                return
            self._previous_lines = new_lines
            self._previous_width = width
            self._previous_height = height
            self._previous_viewport_top = prev_viewport_top
            # Reposition hardware cursor for IME.
            self._position_hardware_cursor(cursor_pos, len(new_lines))
            return

        # Differential rendering can only touch rows that are still visible.
        # A logical line above the previous viewport cannot be reached with a
        # relative cursor sequence.
        if first_changed < prev_viewport_top:
            full_render(True)
            return

        render_end = min(last_changed, len(new_lines) - 1)

        # Differential: move cursor to first_changed, write changed lines.
        buf = "\x1b[?2026h"
        prev_viewport_bottom = prev_viewport_top + height - 1
        move_target_row = first_changed - 1 if append_start else first_changed

        # CSI-B is clamped at the bottom of the viewport; it cannot enter a
        # not-yet-existing screen row.  Move to the bottom and use CRLF to
        # perform the real terminal scroll before continuing the diff.
        if move_target_row > prev_viewport_bottom:
            current_screen_row = max(
                0,
                min(height - 1, hardware_cursor_row - prev_viewport_top),
            )
            move_to_bottom = height - 1 - current_screen_row
            if move_to_bottom > 0:
                buf += f"\x1b[{move_to_bottom}B"
            scroll = move_target_row - prev_viewport_bottom
            buf += "\r\n" * scroll
            prev_viewport_top += scroll
            viewport_top += scroll
            hardware_cursor_row = move_target_row

        line_diff = compute_line_diff(move_target_row)
        if line_diff > 0:
            buf += f"\x1b[{line_diff}B"
        elif line_diff < 0:
            buf += f"\x1b[{-line_diff}A"
        # A pure append enters the new row with CRLF.  Moving down with CSI-B
        # would be clamped at the bottom and overwrite the previous last line.
        buf += "\r\n" if append_start else "\r"

        for i in range(first_changed, render_end + 1):
            if i > first_changed:
                buf += "\r\n"
            buf += "\x1b[2K"  # Clear line.
            buf += new_lines[i]

        final_cursor_row = render_end

        # Clear extra lines if content shrunk.
        if len(self._previous_lines) > len(new_lines):
            move_down = len(new_lines) - 1 - render_end
            if move_down > 0:
                buf += f"\x1b[{move_down}B"
                final_cursor_row = len(new_lines) - 1
            extra = len(self._previous_lines) - len(new_lines)
            for _ in range(extra):
                buf += "\r\n\x1b[2K"
            buf += f"\x1b[{extra}A"

        # Append hardware-cursor repositioning INTO the synchronized output
        # block (before ?2026l). Emitting it outside the synced block caused
        # races on Windows cmd: the next frame's relative cursor move
        # (\x1b[NB) was computed from _hardware_cursor_row assuming the prior
        # reposition landed, but if the terminal hadn't processed it yet the
        # move started from the wrong row — pushing popup content down and
        # triggering spurious scrolls. Folding it in guarantees atomicity.
        if cursor_pos and len(new_lines) > 0:
            tgt_row = max(0, min(cursor_pos[0], len(new_lines) - 1))
            tgt_col = max(0, cursor_pos[1])
            _row_delta = tgt_row - final_cursor_row
            if _row_delta > 0:
                buf += f"\x1b[{_row_delta}B"
            elif _row_delta < 0:
                buf += f"\x1b[{-_row_delta}A"
            buf += f"\x1b[{tgt_col + 1}G"
            self._hardware_cursor_row = tgt_row
        else:
            # 无光标组件（例如会话树选择器）会让终端光标停在变化区域末尾。
            # 同步记录实际行号，确保下一次方向键渲染回到现有选择器进行覆盖，
            # 而不是在下方继续追加一份。
            self._hardware_cursor_row = final_cursor_row
            buf += "\x1b[?25l"
        # hide_cursor (\x1b[?25l) by default; show only if explicitly enabled.
        if not self._show_hardware_cursor:
            buf += "\x1b[?25l"

        buf += "\x1b[?2026l"
        self.terminal.write(buf)
        self.terminal.flush()

        self._cursor_row = max(0, len(new_lines) - 1)
        self._max_lines_rendered = max(self._max_lines_rendered, len(new_lines))
        self._previous_viewport_top = max(
            prev_viewport_top,
            final_cursor_row - height + 1,
        )
        self._previous_lines = new_lines
        self._previous_width = width
        self._previous_height = height

    def _apply_line_resets(self, lines: list[str]) -> list[str]:
        """Append a reset + OSC 8 close to every line."""
        reset = SEGMENT_RESET
        for i in range(len(lines)):
            lines[i] = normalize_terminal_output(lines[i]) + reset
        return lines

    def _extract_cursor_position(
        self,
        lines: list[str],
        height: int,
        *,
        viewport_top: int | None = None,
    ) -> "tuple[int, int] | None":
        """Find CURSOR_MARKER in rendered lines, strip it, return (row, col).

        the extractCursorPosition. The focused
        component (Editor / TextInput) emits ``CURSOR_MARKER`` at the cursor
        position in its render output; this method finds it, computes the
        visible column (width of text before the marker), strips the marker
        from the line in place, and returns the position.

        The returned (row, col) is then used by :meth:`_position_hardware_cursor`
        to move the terminal's real cursor there — which is what Windows IME
        reads to position its candidate window. Without this, IME candidates
        float to a default position (screen bottom-right) regardless of where
        the editor cursor actually is.

        ``viewport_top`` is the renderer's actual logical viewport origin.
        Markers outside that viewport are stripped but ignored so a shrink
        cannot make cursor bookkeeping claim an unreachable row.
        """
        if not lines:
            return None
        actual_viewport_top = (
            max(0, len(lines) - height)
            if viewport_top is None
            else max(0, viewport_top)
        )
        viewport_bottom = actual_viewport_top + max(0, height)
        selected: tuple[int, int] | None = None
        for row in range(len(lines) - 1, -1, -1):
            line = lines[row]
            idx = line.find(CURSOR_MARKER)
            if idx != -1:
                before = line[:idx]
                col = visible_width(before)
                # Strip the marker from the line in place.
                lines[row] = before + line[idx + len(CURSOR_MARKER):]
                if (
                    selected is None
                    and actual_viewport_top <= row < viewport_bottom
                ):
                    selected = (row, col)
        return selected

    def _position_hardware_cursor(
        self, cursor_pos: "tuple[int, int] | None", total_lines: int
    ) -> None:
        """Move the terminal hardware cursor to ``cursor_pos`` for IME.

        the positionHardwareCursor. The cursor
        sequences here are emitted AFTER the content write — so by the time
        we run, the hardware cursor is at the end of the last changed line.
        We then reposition it to the (row, col) extracted from CURSOR_MARKER.

        Windows IME (and other platform IMEs that read the hardware cursor
        for candidate-window placement) will follow this position. If there's
        no marker, we hide the cursor entirely (so it doesn't sit somewhere
        arbitrary on screen).
        """
        if not cursor_pos or total_lines <= 0:
            # No focused cursor → hide it so it doesn't visually pollute.
            self.terminal.hide_cursor()
            return

        target_row = max(0, min(cursor_pos[0], total_lines - 1))
        target_col = max(0, cursor_pos[1])

        # Move cursor from the current hardware-cursor row to the target row.
        row_delta = target_row - self._hardware_cursor_row
        buf = ""
        if row_delta > 0:
            buf += f"\x1b[{row_delta}B"
        elif row_delta < 0:
            buf += f"\x1b[{-row_delta}A"
        # Move to absolute column (1-indexed via CHA — Cursor Horizontal Absolute).
        buf += f"\x1b[{target_col + 1}G"
        if buf:
            self.terminal.write(buf)
            self.terminal.flush()
        self._hardware_cursor_row = target_row
        if self._show_hardware_cursor:
            self.terminal.show_cursor()
        else:
            # Hidden by default but still positioned for IME —
            # Windows IME reads the cursor position even when it's not
            # visually shown.
            self.terminal.hide_cursor()

    def _write_deleted_lines(
        self,
        new_lines: list[str],
        *,
        height: int,
        viewport_top: int,
    ) -> bool:
        """Clear visible trailing rows after a shrink.

        Returns ``False`` when the rows to clear are outside the current
        viewport and the caller must use a full redraw instead.
        """
        target_row = max(0, len(new_lines) - 1)
        extra = len(self._previous_lines) - len(new_lines)
        if target_row < viewport_top or extra > height:
            return False

        current_screen_row = self._hardware_cursor_row - viewport_top
        target_screen_row = target_row - viewport_top
        line_diff = target_screen_row - current_screen_row
        buf = "\x1b[?2026h"
        if line_diff > 0:
            buf += f"\x1b[{line_diff}B"
        elif line_diff < 0:
            buf += f"\x1b[{-line_diff}A"
        buf += "\r"

        clear_start_offset = 0 if not new_lines else 1
        if extra > 0 and clear_start_offset:
            buf += f"\x1b[{clear_start_offset}B"
        for i in range(extra):
            buf += "\r\x1b[2K"
            if i < extra - 1:
                buf += "\x1b[1B"
        move_back = max(0, extra - 1 + clear_start_offset)
        if move_back:
            buf += f"\x1b[{move_back}A"
        buf += "\x1b[?2026l"
        self.terminal.write(buf)
        self.terminal.flush()
        self._cursor_row = target_row
        self._hardware_cursor_row = target_row
        return True
