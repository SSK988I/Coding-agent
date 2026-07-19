"""ModelSelectorComponent — inline model picker.

When the user submits ``/model``, the editor is swapped out of the TUI tree
and this component takes its place. It draws its own top/bottom ``─`` borders
(matching the editor's border style), a one-line ``>`` filter prompt, and a
:class:`~agent_tui.components.select_list.SelectList` of available models. The
user types to filter, arrow-keys to move, Enter to confirm, Esc to cancel.

Layout (top → bottom)::

    ─── top border ───
      <hint line>
      > <filter text>
        <model row>
        <model row>
    ─── bottom border ───

Unlike the version (which uses a separate ``Input`` + ``DynamicBorder`` +
``Text`` children inside a ``Container``), this is a single :class:`Component`
that renders the whole region itself — the TUI here has no per-child layout
engine, so composing it from many small components would only add ceremony.
"""
from __future__ import annotations

from typing import Any, Callable, List

from agent_tui.components.select_list import SelectItem, SelectList
from agent_tui.keys import matches_key
from agent_tui.theme import Theme
from agent_tui.utils import visible_width

from coding_agent.modes.interactive.components.text_input import TextInput


class ModelSelectorComponent:
    """Inline model selector with filter + list, wrapped in ``─`` borders.

    Args:
        theme: used for the border color and dim hint text.
        models: list of model objects; each must expose ``id`` (str),
            ``name`` (str), ``provider`` (str), and ``reasoning`` (bool).
        current_model_id: the currently-active model id. It is sorted to the
            top of the list and is the initial selection.
        on_select: ``callback(model_id: str)`` fired on Enter.
        on_cancel: ``callback()`` fired on Esc / Ctrl+C.
        initial_filter: optional pre-seeded filter text (e.g. when the user
            typed ``/model flash`` with no exact match).
        max_visible: how many model rows to show before scrolling.
    """

    def __init__(
        self,
        theme: Theme,
        models: List[Any],
        current_model_id: "str | None",
        on_select: Callable[[str], None],
        on_cancel: Callable[[], None],
        *,
        initial_filter: str = "",
        max_visible: int = 6,
        border_color_fn: "Callable[[str], str] | None" = None,
    ) -> None:
        self._theme = theme
        self._on_select = on_select
        self._on_cancel = on_cancel
        # The filter text is owned by a TextInput widget (no submit/cancel
        # callbacks: this component intercepts Enter/Esc itself for select/
        # cancel semantics, and delegates only editing keys to the widget).
        self._input = TextInput(initial=initial_filter or "")
        # Border color: defaults to the theme's "border" token, but the caller
        # passes the current thinking-level color so the selector's frame
        # matches the editor's thinking-level color.
        self._border_color_fn = border_color_fn or (lambda s: theme.fg("border", s))

        # Sort: current model first, then by id (stable, deterministic).
        self._models = sorted(
            models,
            key=lambda m: (0 if m.id == current_model_id else 1, m.id),
        )
        self._current_id = current_model_id

        self._list = SelectList(max_visible=max_visible)
        self.focused = True
        self._apply_filter()

    # ── filtering ──────────────────────────────────────────────────────

    def _apply_filter(self) -> None:
        """Rebuild the SelectList from ``self._models`` + current filter."""
        flt = self._input.value.lower()
        items: list[SelectItem] = []
        for m in self._models:
            if flt and not self._matches(m, flt):
                continue
            desc = m.provider
            if getattr(m, "reasoning", False):
                desc += " · reasoning"
            items.append(SelectItem(value=m.id, label=m.id, description=desc))
        self._list.set_items(items)

    @staticmethod
    def _matches(m: Any, flt: str) -> bool:
        """Case-insensitive substring match over id, name, and provider."""
        hay = " ".join(
            str(getattr(m, f, "") or "")
            for f in ("id", "name", "provider")
        ).lower()
        return flt in hay

    # ── input ──────────────────────────────────────────────────────────

    def handle_input(self, data: str) -> bool:
        """Consume nav/accept/cancel keys; delegate editing to TextInput."""
        # Cancel (intercepted before TextInput so it triggers on_cancel, not
        # the widget's own no-op cancel).
        if matches_key(data, "escape") or matches_key(data, "ctrl+c"):
            self._on_cancel()
            return True
        # Accept current selection.
        if matches_key(data, "enter"):
            sel = self._list.get_selected()
            if sel is not None:
                self._on_select(sel.value)
            else:
                self._on_cancel()
            return True
        # Navigation.
        if matches_key(data, "up"):
            self._list.move_up()
            return True
        if matches_key(data, "down"):
            self._list.move_down()
            return True
        # Editing keys (printable/backspace) → TextInput, then refilter.
        if self._input.handle_input(data):
            self._apply_filter()
            return True
        return False

    # ── render ─────────────────────────────────────────────────────────

    def render(self, width: int) -> List[str]:
        """Render the bordered selector region to lines.

        Width is the full terminal width; borders span it, content is
        indented 2 columns.
        """
        border = self._border_color_fn("─" * width)

        indent = "  "
        content_width = max(1, width - len(indent))

        total = len(self._list.items)
        hint = (
            f"模型选择: 输入过滤, ↑↓ 选择, Enter 确认, Esc 取消"
            f"  ({total} 个)"
        )
        hint = self._truncate(hint, content_width)

        # The filter line is rendered by the TextInput widget ("> " prefix +
        # value + reverse-video cursor block).
        filter_line = self._input.render(content_width)

        lines: list[str] = [border]
        lines.append(indent + hint)
        lines.append(indent + filter_line)
        for row in self._list.render(content_width):
            lines.append(indent + row)
        lines.append(border)
        return lines

    @staticmethod
    def _truncate(text: str, width: int) -> str:
        """Truncate ``text`` to ``width`` visible columns, appending ``…``."""
        if visible_width(text) <= width:
            return text
        out = ""
        for ch in text:
            if visible_width(out + ch) >= width:
                return out + "…"
            out += ch
        return out
