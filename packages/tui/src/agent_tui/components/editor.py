"""Editor component: multi-line text input.

A retain-mode text editor that renders into the TUI component tree (unlike
prompt_toolkit which owns its own screen). Supports grapheme-aware editing,
Emacs keybindings (kill ring, word navigation), history, and multi-line
word-wrap with scrolling.

Autocomplete and selector popups are composed around the editor by the application.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, List

from agent_tui.keys import matches_key
from agent_tui.kill_ring import KillRing
from agent_tui.tui import CURSOR_MARKER, Component
from agent_tui.undo_stack import UndoStack
from agent_tui.utils import segment_graphemes, truncate_to_width, visible_width, wrap_text_with_ansi

if TYPE_CHECKING:
    from agent_tui.tui import TUI


@dataclass
class _EditorState:
    """Mutable editor state. Cloned for undo."""
    lines: list[str] = field(default_factory=lambda: [""])
    cursor_line: int = 0
    cursor_col: int = 0


@dataclass
class _LayoutLine:
    """One visual line after word-wrap."""
    text: str = ""
    has_cursor: bool = False
    cursor_pos: int = 0


class Editor(Component):
    """Multi-line text editor with Emacs keybindings.

    Implements :class:`Component` and :class:`Focusable`. When focused, emits
    ``CURSOR_MARKER`` at the cursor position so the TUI can place the hardware
    cursor for IME.

    Key bindings use the standard editor defaults:
      - Enter: submit (or newline if line ends with ``\\``)
      - Ctrl+A/E: line start/end
      - Ctrl+B/F / arrows: char left/right
      - Up/Down: navigate (history at first/last line)
      - Ctrl+W: delete word backward
      - Ctrl+K/U: delete to line end/start
      - Ctrl+Y: yank (paste from kill ring)
      - Backspace/Delete: char delete
    """

    def __init__(
        self,
        tui: "TUI | None" = None,
        *,
        border_color_fn: "Callable[[str], str] | None" = None,
        padding_x: int = 0,
    ) -> None:
        self._tui = tui
        self._state = _EditorState()
        self.focused: bool = False  # Focusable

        self._border_color_fn = border_color_fn or (lambda s: s)
        self._padding_x = padding_x
        self._last_width = 80
        self._scroll_offset = 0

        # Undo + kill ring.
        self._undo_stack: UndoStack[_EditorState] = UndoStack()
        self._kill_ring = KillRing()
        self._last_action: "str | None" = None  # for fish-style undo coalescing

        # History.
        self._history: list[str] = []
        self._history_index = -1  # -1 = not browsing
        self._draft = ""

        # Callbacks.
        self.on_submit: "Callable[[str], None] | None" = None
        self.on_change: "Callable[[str], None] | None" = None
        #: Called on Escape when the editor is focused; Escape acts as the
        #: interrupt key). If set and returns True, the event is consumed.
        self.on_escape: "Callable[[], bool | None] | None" = None
        self.disable_submit = False

    # ── public API ─────────────────────────────────────────────────────

    def get_text(self) -> str:
        return "\n".join(self._state.lines)

    def get_lines(self) -> list[str]:
        return list(self._state.lines)

    def get_cursor(self) -> "tuple[int, int]":
        return self._state.cursor_line, self._state.cursor_col

    def set_text(self, text: str) -> None:
        self._last_action = None
        self._exit_history_browsing()
        normalized = self._normalize(text)
        if self.get_text() != normalized:
            self._push_undo()
        self._set_text_internal(normalized)

    def add_to_history(self, text: str) -> None:
        if text:
            self._history.append(text)

    def set_text_and_cursor(self, text: str, line: int, col: int) -> None:
        """Replace text and place the cursor at ``(line, col)``.

        Used by autocomplete to apply a completion. Bypasses the
        history/undo bookkeeping of :meth:`set_text` so accepting a
        suggestion is one atomic visual update.
        """
        self._last_action = None
        self._exit_history_browsing()
        lines = text.split("\n") if text else [""]
        if not lines:
            lines = [""]
        self._state.lines = lines
        self._state.cursor_line = max(0, min(line, len(lines) - 1))
        cur_line = self._state.lines[self._state.cursor_line]
        self._state.cursor_col = max(0, min(col, len(cur_line)))
        if self.on_change:
            self.on_change(self.get_text())

    def set_autocomplete_provider(self, provider: Any) -> None:
        """Store an autocomplete provider (used by the host to drive popups).

        The editor itself does not consult the provider; the host owns the
        popup lifecycle. Kept on the editor for symmetry with the
        Editor.setAutocompleteProvider and so hosts
        that swap editors can find it.
        """
        self._autocomplete_provider = provider

    @property
    def autocomplete_provider(self) -> Any:
        return getattr(self, "_autocomplete_provider", None)

    def invalidate(self) -> None:
        pass  # No render cache (state is the source of truth).

    # ── render ─────────────────────────────────────

    def render(self, width: int) -> List[str]:
        max_padding = max(0, (width - 1) // 2)
        padding_x = min(self._padding_x, max_padding)
        content_width = max(1, width - padding_x * 2)
        # Reserve 1 column for cursor when no padding.
        layout_width = max(1, content_width - (0 if padding_x else 1))
        self._last_width = layout_width

        horizontal = self._border_color_fn("─")
        layout_lines = self._layout_text(layout_width)

        # Max visible lines: 30% of terminal height, min 5.
        terminal_rows = self._tui.terminal.rows if self._tui else 24
        max_visible = max(5, int(terminal_rows * 0.3))

        # Scroll to keep cursor visible.
        cursor_idx = -1
        for i, ll in enumerate(layout_lines):
            if ll.has_cursor:
                cursor_idx = i
                break
        if cursor_idx == -1:
            cursor_idx = 0
        if cursor_idx < self._scroll_offset:
            self._scroll_offset = cursor_idx
        elif cursor_idx >= self._scroll_offset + max_visible:
            self._scroll_offset = cursor_idx - max_visible + 1
        max_scroll = max(0, len(layout_lines) - max_visible)
        self._scroll_offset = max(0, min(self._scroll_offset, max_scroll))

        visible = layout_lines[self._scroll_offset : self._scroll_offset + max_visible]
        result: list[str] = []
        left_pad = " " * padding_x
        right_pad = left_pad

        # Top border with scroll indicator.
        if self._scroll_offset > 0:
            indicator = f"─── ↑ {self._scroll_offset} more "
            remaining = width - visible_width(indicator)
            if remaining >= 0:
                result.append(self._border_color_fn(indicator + "─" * remaining))
            else:
                result.append(self._border_color_fn(truncate_to_width(indicator, width)))
        else:
            result.append(horizontal * width)

        # Render visible layout lines.
        emit_marker = self.focused
        for ll in visible:
            display = ll.text
            line_w = visible_width(display)

            if ll.has_cursor:
                before = display[: ll.cursor_pos]
                after = display[ll.cursor_pos :]
                marker = CURSOR_MARKER if emit_marker else ""
                if after:
                    # Cursor on a character: reverse-video it (grapheme-aware).
                    after_graphemes = segment_graphemes(after)
                    first = after_graphemes[0] if after_graphemes else " "
                    rest = after[len(first):]
                    cursor = f"\x1b[7m{first}\x1b[0m"
                    display = before + marker + cursor + rest
                else:
                    # Cursor at end: reverse-video space.
                    cursor = "\x1b[7m \x1b[0m"
                    display = before + marker + cursor
                    line_w += 1

            pad = " " * max(0, content_width - line_w)
            result.append(f"{left_pad}{display}{pad}{right_pad}")

        # Bottom border with scroll indicator.
        below = len(layout_lines) - (self._scroll_offset + len(visible))
        if below > 0:
            indicator = f"─── ↓ {below} more "
            remaining = width - visible_width(indicator)
            result.append(self._border_color_fn(indicator + "─" * max(0, remaining)))
        else:
            result.append(horizontal * width)

        return result

    # ── layout ─────────────────────────────────────

    def _layout_text(self, content_width: int) -> list[_LayoutLine]:
        layout_lines: list[_LayoutLine] = []
        if not self._state.lines or (len(self._state.lines) == 1 and self._state.lines[0] == ""):
            layout_lines.append(_LayoutLine("", has_cursor=True, cursor_pos=0))
            return layout_lines

        for i, line in enumerate(self._state.lines):
            is_current = i == self._state.cursor_line
            vw = visible_width(line)
            if vw <= content_width:
                if is_current:
                    layout_lines.append(_LayoutLine(line, has_cursor=True, cursor_pos=self._state.cursor_col))
                else:
                    layout_lines.append(_LayoutLine(line))
            else:
                # Word-wrap the line.
                wrapped = wrap_text_with_ansi(line, content_width) if "\x1b" in line else self._wrap_plain(line, content_width)
                # Map cursor col to the right wrapped chunk.
                start = 0
                for chunk in wrapped:
                    end = start + visible_width(chunk)
                    if is_current:
                        is_last = chunk is wrapped[-1]
                        if (is_last and self._state.cursor_col >= start) or (
                            not is_last and start <= self._state.cursor_col < end
                        ):
                            pos = min(self._state.cursor_col - start, len(chunk))
                            layout_lines.append(_LayoutLine(chunk, has_cursor=True, cursor_pos=max(0, pos)))
                        else:
                            layout_lines.append(_LayoutLine(chunk))
                    else:
                        layout_lines.append(_LayoutLine(chunk))
                    start = end
        return layout_lines

    def _wrap_plain(self, line: str, width: int) -> list[str]:
        """Word-wrap a plain (no-ANSI) line by visible width."""
        if visible_width(line) <= width:
            return [line]
        result: list[str] = []
        current = ""
        cur_w = 0
        for word in line.split(" " if " " in line else ""):
            ww = visible_width(word)
            sep = 1 if current else 0
            if cur_w + sep + ww > width and current:
                result.append(current)
                current = word
                cur_w = ww
            else:
                current = current + (" " if current else "") + word
                cur_w += sep + ww
        if current:
            result.append(current)
        return result if result else [line]

    # ── handle_input ───────────────────────────────

    def handle_input(self, data: str) -> bool:
        # Ctrl+C — let parent handle.
        if matches_key(data, "ctrl+c"):
            return False

        # Escape interrupts streaming or compaction.
        # Delegate to on_escape when wired by the host mode.
        if matches_key(data, "escape") and self.on_escape is not None:
            result = self.on_escape()
            if result is not False:
                return True

        # Undo.
        if matches_key(data, "ctrl+z"):
            self._undo()
            return True

        # Deletion.
        if matches_key(data, "ctrl+k"):
            self._delete_to_end_of_line()
            return True
        if matches_key(data, "ctrl+u"):
            self._delete_to_start_of_line()
            return True
        if matches_key(data, "ctrl+w"):
            self._delete_word_backwards()
            return True
        if matches_key(data, "backspace") or matches_key(data, "shift+backspace"):
            self._handle_backspace()
            return True
        if matches_key(data, "delete") or matches_key(data, "shift+delete"):
            self._handle_forward_delete()
            return True
        # Ctrl+D: forward-delete one char.
        # When the editor is empty, an upstream app.exit listener intercepts
        # Ctrl+D to quit; this branch only runs when the editor has text.
        if matches_key(data, "ctrl+d"):
            self._handle_forward_delete()
            return True

        # Kill ring yank.
        if matches_key(data, "ctrl+y"):
            self._yank()
            return True

        # Cursor movement.
        if matches_key(data, "ctrl+a") or matches_key(data, "home"):
            self._move_to_line_start()
            return True
        if matches_key(data, "ctrl+e") or matches_key(data, "end"):
            self._move_to_line_end()
            return True
        if matches_key(data, "ctrl+b") or matches_key(data, "left"):
            self._move_cursor(0, -1)
            return True
        if matches_key(data, "ctrl+f") or matches_key(data, "right"):
            self._move_cursor(0, 1)
            return True
        if matches_key(data, "ctrl+left"):
            self._move_word_backwards()
            return True
        if matches_key(data, "ctrl+right"):
            self._move_word_forwards()
            return True

        # New line (alt+enter / shift+enter).
        if matches_key(data, "alt+enter") or data == "\n":
            self._add_new_line()
            return True

        # Submit (Enter).
        if matches_key(data, "enter"):
            if self.disable_submit:
                return True
            # `\` + Enter → newline.
            current = self._state.lines[self._state.cursor_line] or ""
            if self._state.cursor_col > 0 and current[self._state.cursor_col - 1] == "\\":
                self._handle_backspace()
                self._add_new_line()
                return True
            self._submit_value()
            return True

        # Arrow up/down (with history).
        if matches_key(data, "up"):
            if self._is_on_first_visual_line():
                self._navigate_history(-1)
            else:
                self._move_cursor(-1, 0)
            return True
        if matches_key(data, "down"):
            if self._is_on_last_visual_line():
                self._move_to_line_end()
            else:
                self._move_cursor(1, 0)
            return True

        # Printable character.
        if len(data) == 1 and ord(data) >= 32:
            self._insert_character(data)
            return True
        # Multi-char printable (e.g. CJK that arrived together).
        if data and all(ord(c) >= 32 for c in data):
            self._insert_character(data)
            return True

        return False

    # ── editing operations ─────────────────────────────────────────────

    def _normalize(self, text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")

    def _set_text_internal(self, text: str) -> None:
        lines = text.split("\n") if text else [""]
        self._state.lines = lines if lines else [""]
        self._state.cursor_line = min(self._state.cursor_line, len(self._state.lines) - 1)
        self._state.cursor_col = min(self._state.cursor_col, len(self._state.lines[self._state.cursor_line]))
        if self.on_change:
            self.on_change(self.get_text())

    def _insert_character(self, char: str) -> None:
        self._exit_history_browsing()
        # Undo coalescing: word chars group, whitespace starts new group.
        is_ws = char.strip() == ""
        if is_ws or self._last_action != "type-word":
            self._push_undo()
        self._last_action = "type-word"

        line = self._state.lines[self._state.cursor_line] or ""
        before = line[: self._state.cursor_col]
        after = line[self._state.cursor_col:]
        self._state.lines[self._state.cursor_line] = before + char + after
        self._state.cursor_col += len(char)
        if self.on_change:
            self.on_change(self.get_text())

    def _insert_text_at_cursor(self, text: str) -> None:
        """Multi-line insert."""
        if not text:
            return
        normalized = self._normalize(text)
        inserted = normalized.split("\n")
        current = self._state.lines[self._state.cursor_line] or ""
        before = current[: self._state.cursor_col]
        after = current[self._state.cursor_col:]
        if len(inserted) == 1:
            self._state.lines[self._state.cursor_line] = before + normalized + after
            self._state.cursor_col += len(normalized)
        else:
            self._state.lines = (
                self._state.lines[: self._state.cursor_line]
                + [before + inserted[0]]
                + inserted[1:-1]
                + [inserted[-1] + after]
                + self._state.lines[self._state.cursor_line + 1:]
            )
            self._state.cursor_line += len(inserted) - 1
            self._state.cursor_col = len(inserted[-1])
        if self.on_change:
            self.on_change(self.get_text())

    def _handle_backspace(self) -> None:
        """Grapheme-aware backspace."""
        self._exit_history_browsing()
        self._last_action = None
        if self._state.cursor_col > 0:
            self._push_undo()
            line = self._state.lines[self._state.cursor_line] or ""
            before = line[: self._state.cursor_col]
            graphemes = segment_graphemes(before)
            last = graphemes[-1] if graphemes else ""
            glen = len(last)
            self._state.lines[self._state.cursor_line] = line[: self._state.cursor_col - glen] + line[self._state.cursor_col:]
            self._state.cursor_col -= glen
        elif self._state.cursor_line > 0:
            self._push_undo()
            current = self._state.lines[self._state.cursor_line] or ""
            prev = self._state.lines[self._state.cursor_line - 1] or ""
            self._state.lines[self._state.cursor_line - 1] = prev + current
            del self._state.lines[self._state.cursor_line]
            self._state.cursor_line -= 1
            self._state.cursor_col = len(prev)
        if self.on_change:
            self.on_change(self.get_text())

    def _handle_forward_delete(self) -> None:
        self._exit_history_browsing()
        self._last_action = None
        line = self._state.lines[self._state.cursor_line] or ""
        if self._state.cursor_col < len(line):
            self._push_undo()
            after = line[self._state.cursor_col:]
            graphemes = segment_graphemes(after)
            first = graphemes[0] if graphemes else ""
            glen = len(first)
            self._state.lines[self._state.cursor_line] = line[: self._state.cursor_col] + line[self._state.cursor_col + glen:]
        elif self._state.cursor_line < len(self._state.lines) - 1:
            self._push_undo()
            current = self._state.lines[self._state.cursor_line] or ""
            nxt = self._state.lines[self._state.cursor_line + 1] or ""
            self._state.lines[self._state.cursor_line] = current + nxt
            del self._state.lines[self._state.cursor_line + 1]
        if self.on_change:
            self.on_change(self.get_text())

    def _add_new_line(self) -> None:
        self._exit_history_browsing()
        self._push_undo()
        self._last_action = None
        line = self._state.lines[self._state.cursor_line] or ""
        before = line[: self._state.cursor_col]
        after = line[self._state.cursor_col:]
        self._state.lines[self._state.cursor_line] = before
        self._state.lines.insert(self._state.cursor_line + 1, after)
        self._state.cursor_line += 1
        self._state.cursor_col = 0
        if self.on_change:
            self.on_change(self.get_text())

    def _delete_to_end_of_line(self) -> None:
        """Ctrl+K: kill from cursor to end of line."""
        self._exit_history_browsing()
        line = self._state.lines[self._state.cursor_line] or ""
        killed = line[self._state.cursor_col:]
        if killed:
            self._push_undo()
            self._kill_ring.push(killed, prepend=False, accumulate=(self._last_action == "kill-forward"))
            self._last_action = "kill-forward"
            self._state.lines[self._state.cursor_line] = line[: self._state.cursor_col]
            if self.on_change:
                self.on_change(self.get_text())

    def _delete_to_start_of_line(self) -> None:
        """Ctrl+U: kill from start to cursor."""
        self._exit_history_browsing()
        line = self._state.lines[self._state.cursor_line] or ""
        killed = line[: self._state.cursor_col]
        if killed:
            self._push_undo()
            self._kill_ring.push(killed, prepend=True, accumulate=(self._last_action == "kill-backward"))
            self._last_action = "kill-backward"
            self._state.lines[self._state.cursor_line] = line[self._state.cursor_col:]
            self._state.cursor_col = 0
            if self.on_change:
                self.on_change(self.get_text())

    def _delete_word_backwards(self) -> None:
        """Ctrl+W: delete word before cursor."""
        self._exit_history_browsing()
        line = self._state.lines[self._state.cursor_line] or ""
        if self._state.cursor_col == 0:
            return
        self._push_undo()
        before = line[: self._state.cursor_col]
        # Find word boundary: skip trailing space, then word chars.
        stripped = before.rstrip()
        if len(stripped) < len(before):
            start = len(stripped)
        else:
            idx = stripped.rfind(" ")
            start = idx + 1 if idx != -1 else 0
        killed = line[start: self._state.cursor_col]
        self._kill_ring.push(killed, prepend=True)
        self._state.lines[self._state.cursor_line] = line[:start] + line[self._state.cursor_col:]
        self._state.cursor_col = start
        if self.on_change:
            self.on_change(self.get_text())

    def _yank(self) -> None:
        """Ctrl+Y: paste most recent kill ring entry."""
        text = self._kill_ring.peek()
        if text:
            self._push_undo()
            self._insert_text_at_cursor(text)

    # ── cursor movement ────────────────────────────────────────────────

    def _move_cursor(self, delta_line: int, delta_col: int) -> None:
        self._exit_history_browsing()
        if delta_col != 0:
            line = self._state.lines[self._state.cursor_line] or ""
            if delta_col < 0:
                self._state.cursor_col = max(0, self._state.cursor_col + delta_col)
            else:
                self._state.cursor_col = min(len(line), self._state.cursor_col + delta_col)
        if delta_line != 0:
            self._state.cursor_line = max(0, min(len(self._state.lines) - 1, self._state.cursor_line + delta_line))
            line = self._state.lines[self._state.cursor_line] or ""
            self._state.cursor_col = min(self._state.cursor_col, len(line))

    def _move_to_line_start(self) -> None:
        self._exit_history_browsing()
        self._state.cursor_col = 0

    def _move_to_line_end(self) -> None:
        self._exit_history_browsing()
        line = self._state.lines[self._state.cursor_line] or ""
        self._state.cursor_col = len(line)

    def _move_word_backwards(self) -> None:
        self._exit_history_browsing()
        line = self._state.lines[self._state.cursor_line] or ""
        if self._state.cursor_col == 0:
            return
        before = line[: self._state.cursor_col].rstrip()
        if len(before) < self._state.cursor_col:
            self._state.cursor_col = len(before)
            return
        idx = before.rfind(" ")
        self._state.cursor_col = idx + 1 if idx != -1 else 0

    def _move_word_forwards(self) -> None:
        self._exit_history_browsing()
        line = self._state.lines[self._state.cursor_line] or ""
        after = line[self._state.cursor_col:]
        stripped = after.lstrip()
        skip = len(after) - len(stripped)
        rest = stripped[skip:] if skip < len(stripped) else stripped
        idx = rest.find(" ")
        if idx == -1:
            self._state.cursor_col = len(line)
        else:
            self._state.cursor_col += skip + idx

    # ── history ────────────────────────────────────

    def _navigate_history(self, direction: int) -> None:
        if not self._history:
            return
        if self._history_index == -1:
            # Entering history mode: save current as draft.
            self._draft = self.get_text()
            self._history_index = len(self._history)
        self._history_index = max(-1, min(len(self._history) - 1, self._history_index + direction))
        if self._history_index == -1:
            text = self._draft
        else:
            text = self._history[self._history_index]
        self._set_text_internal(text)

    def _exit_history_browsing(self) -> None:
        self._history_index = -1

    # ── submit ───────────────────────────────────

    def _submit_value(self) -> None:
        result = self.get_text().strip()
        self._state = _EditorState()
        self._exit_history_browsing()
        self._scroll_offset = 0
        self._undo_stack.clear()
        self._last_action = None
        if self.on_change:
            self.on_change("")
        if self.on_submit:
            self.on_submit(result)

    # ── undo ───────────────────────────────────────────────────────────

    def _push_undo(self) -> None:
        self._undo_stack.push(self._state)

    def _undo(self) -> None:
        self._last_action = None
        snapshot = self._undo_stack.pop()
        if snapshot is not None:
            self._state = snapshot
            if self.on_change:
                self.on_change(self.get_text())

    # ── helpers ────────────────────────────────────────────────────────

    def _is_on_first_visual_line(self) -> bool:
        return self._state.cursor_line == 0

    def _is_on_last_visual_line(self) -> bool:
        return self._state.cursor_line == len(self._state.lines) - 1
