"""ProviderSelectorComponent — inline provider picker for /login and /logout.

The two-step authentication flow asks the user to select a provider before
prompting for an API key. This avoids
picks which provider to authenticate against. Without this step, /login can
only ever configure the current session's provider — so a user who hasn't
yet logged in to GLM (and therefore isn't running a GLM session) can't
reach the GLM login dialog at all. This selector breaks that chicken-and-egg.

Reuses the same shape as :class:`ModelSelectorComponent` (border + filter
prompt + SelectList), but the rows are providers, not models, and each row
shows an auth-state suffix: ``[configured]`` / ``[env]`` / ``[—]``.
"""
from __future__ import annotations

from typing import Callable, List

from agent_tui.components.select_list import SelectItem, SelectList
from agent_tui.keys import matches_key
from agent_tui.theme import Theme
from agent_tui.utils import visible_width

from coding_agent.modes.interactive.components.text_input import TextInput


class ProviderOption:
    """Lightweight row model — exposes ``id`` / ``name`` so the existing
    selector-rendering code path doesn't need to know about Provider."""

    __slots__ = ("id", "name", "auth_state")

    def __init__(self, id: str, name: str, auth_state: str) -> None:
        self.id = id
        self.name = name
        # One of: "configured" (saved in auth.json), "env" (env var set),
        # or "" (nothing). Used purely for the row description.
        self.auth_state = auth_state


class ProviderSelectorComponent:
    """Inline provider selector with filter + list, wrapped in ``─`` borders.

    Args:
        theme: border color + dim hint.
        providers: list of :class:`ProviderOption`. The caller is responsible
            for computing ``auth_state`` for each.
        current_provider_id: the provider currently in use by the session.
            It is sorted to the top and selected initially.
        on_select: ``callback(provider_id: str)`` fired on Enter.
        on_cancel: ``callback()`` fired on Esc / Ctrl+C.
        title: header text shown above the filter prompt.
    """

    def __init__(
        self,
        theme: Theme,
        providers: List[ProviderOption],
        current_provider_id: "str | None",
        on_select: Callable[[str], None],
        on_cancel: Callable[[], None],
        *,
        title: str = "选择 provider",
        border_color_fn: "Callable[[str], str] | None" = None,
    ) -> None:
        self._theme = theme
        self._on_select = on_select
        self._on_cancel = on_cancel
        self._title = title
        self._input = TextInput(initial="")
        self._border_color_fn = border_color_fn or (lambda s: theme.fg("border", s))

        # Sort: current provider first, then alphabetical by id.
        self._providers = sorted(
            providers,
            key=lambda p: (0 if p.id == current_provider_id else 1, p.id),
        )
        self._current_id = current_provider_id

        self._list = SelectList(max_visible=8)
        self.focused = True
        self._apply_filter()

    # ── filtering ──────────────────────────────────────────────────────

    def _apply_filter(self) -> None:
        flt = self._input.value.lower()
        items: list[SelectItem] = []
        for p in self._providers:
            if flt and not self._matches(p, flt):
                continue
            items.append(SelectItem(value=p.id, label=p.name, description=self._describe(p)))
        self._list.set_items(items)

    @staticmethod
    def _matches(p: ProviderOption, flt: str) -> bool:
        hay = f"{p.id} {p.name}".lower()
        return flt in hay

    @staticmethod
    def _describe(p: ProviderOption) -> str:
        if p.auth_state == "configured":
            return f"{p.id} · configured"
        if p.auth_state == "env":
            return f"{p.id} · env"
        return p.id

    # ── input ──────────────────────────────────────────────────────────

    def handle_input(self, data: str) -> bool:
        if matches_key(data, "escape") or matches_key(data, "ctrl+c"):
            self._on_cancel()
            return True
        if matches_key(data, "enter"):
            sel = self._list.get_selected()
            if sel is not None:
                self._on_select(sel.value)
            else:
                self._on_cancel()
            return True
        if matches_key(data, "up"):
            self._list.move_up()
            return True
        if matches_key(data, "down"):
            self._list.move_down()
            return True
        if self._input.handle_input(data):
            self._apply_filter()
            return True
        return False

    # ── render ─────────────────────────────────────────────────────────

    def render(self, width: int) -> List[str]:
        border = self._border_color_fn("─" * width)
        indent = "  "
        content_width = max(1, width - len(indent))

        total = len(self._list.items)
        hint = (
            f"{self._title}: 输入过滤, ↑↓ 选择, Enter 确认, Esc 取消"
            f"  ({total} 个)"
        )
        hint = self._truncate(hint, content_width)
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
        if visible_width(text) <= width:
            return text
        out = ""
        for ch in text:
            if visible_width(out + ch) >= width:
                return out + "…"
            out += ch
        return out
