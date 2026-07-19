"""Responsive welcome card for the interactive terminal UI."""
from __future__ import annotations

from typing import Any, Callable

from agent_tui import Component
from agent_tui.theme import Theme
from agent_tui.utils import truncate_to_width, visible_width


class WelcomeComponent(Component):
    """Render startup guidance without relying on Markdown wrapping."""

    _MAX_CARD_WIDTH = 78
    _WIDE_LAYOUT_WIDTH = 58
    _BOX_LAYOUT_WIDTH = 36

    def __init__(self, session: Any, theme: Theme, version: str) -> None:
        self._session = session
        self._theme = theme
        self._version = version

    def invalidate(self) -> None:
        pass

    def render(self, width: int) -> list[str]:
        width = max(1, width)
        if width < self._BOX_LAYOUT_WIDTH:
            return self._render_minimal(width)

        card_width = min(width - 2, self._MAX_CARD_WIDTH) if width >= 40 else width
        card_width = max(self._BOX_LAYOUT_WIDTH, card_width)
        indent = max(0, (width - card_width) // 2)
        inner_width = card_width - 2

        lines = [self._top_border(card_width)]
        lines.append(
            self._content_line(
                "理解代码 · 修改项目 · 运行验证",
                inner_width,
                lambda text: self._theme.fg("text", text),
            )
        )
        lines.append(self._content_line("", inner_width))

        if card_width >= self._WIDE_LAYOUT_WIDTH:
            metadata = (
                f"MODEL  {self._model_id()}   "
                f"THINKING  {self._thinking_level()}   "
                f"TOOLS  {self._tool_count()}"
            )
            lines.append(self._content_line(metadata, inner_width, self._style_metadata))
        else:
            lines.append(
                self._content_line(
                    f"MODEL     {self._model_id()}", inner_width, self._style_metadata
                )
            )
            lines.append(
                self._content_line(
                    f"THINKING  {self._thinking_level()}   TOOLS  {self._tool_count()}",
                    inner_width,
                    self._style_metadata,
                )
            )

        lines.append(self._content_line("", inner_width))
        lines.append(
            self._content_line(
                "输入任务并按 Enter 发送", inner_width, self._style_primary_hint
            )
        )
        lines.append(
            self._content_line(
                "/help 命令  ·  /model 模型  ·  ! shell  ·  Esc 中断",
                inner_width,
                self._style_shortcuts,
            )
        )
        lines.append(self._bottom_border(card_width))

        placed = [self._place(line, width, indent) for line in lines]
        placed.append(" " * width)
        return placed

    def _render_minimal(self, width: int) -> list[str]:
        title = self._theme.bold(self._theme.fg("accent", "CODING AGENT"))
        version = self._theme.fg("dim", f" v{self._version}")
        metadata = f"{self._model_id()} · {self._thinking_level()} · {self._tool_count()} tools"
        raw_lines = [
            title + version,
            self._theme.fg("text", "理解代码 · 修改项目 · 运行验证"),
            self._theme.fg("dim", metadata),
            self._style_primary_hint("输入任务并按 Enter 发送"),
            self._style_shortcuts("/help · /model · ! shell"),
            "",
        ]
        return [self._fit(line, width) for line in raw_lines]

    def _top_border(self, card_width: int) -> str:
        inner_width = card_width - 2
        title = " CODING AGENT "
        version = f" v{self._version} "
        fill = max(0, inner_width - visible_width(title) - visible_width(version))
        raw = "╭" + title + "─" * fill + version + "╮"
        return self._theme.fg("accent", raw)

    def _bottom_border(self, card_width: int) -> str:
        return self._theme.fg("borderMuted", "╰" + "─" * (card_width - 2) + "╯")

    def _content_line(
        self,
        text: str,
        inner_width: int,
        style: "Callable[[str], str] | None" = None,
    ) -> str:
        available = max(0, inner_width - 2)
        content = truncate_to_width(text, available, ellipsis="…", pad=True)
        if style is not None:
            content = style(content)
        border = self._theme.fg("borderMuted", "│")
        return f"{border} {content} {border}"

    def _style_metadata(self, text: str) -> str:
        for label in ("MODEL", "THINKING", "TOOLS"):
            text = text.replace(label, self._theme.fg("muted", label))
        return text

    def _style_primary_hint(self, text: str) -> str:
        return text.replace("Enter", self._theme.bold(self._theme.fg("accent", "Enter")))

    def _style_shortcuts(self, text: str) -> str:
        shortcuts = ("/help", "/model", "! shell", "Esc")
        parts: list[str] = []
        remaining = text
        while remaining:
            matches = [
                (remaining.find(shortcut), shortcut)
                for shortcut in shortcuts
                if shortcut in remaining
            ]
            if not matches:
                parts.append(self._theme.fg("dim", remaining))
                break
            index, shortcut = min(matches, key=lambda match: match[0])
            if index:
                parts.append(self._theme.fg("dim", remaining[:index]))
            parts.append(self._theme.fg("accent", shortcut))
            remaining = remaining[index + len(shortcut):]
        return "".join(parts)

    def _fit(self, line: str, width: int) -> str:
        return truncate_to_width(line, width, ellipsis="…", pad=True)

    def _place(self, line: str, width: int, indent: int) -> str:
        line = " " * indent + line
        return self._fit(line, width)

    def _model_id(self) -> str:
        model = getattr(self._session, "model", None)
        return str(getattr(model, "id", None) or "not selected")

    def _thinking_level(self) -> str:
        return str(getattr(self._session, "thinking_level", None) or "off")

    def _tool_count(self) -> int:
        return len(getattr(self._session, "tools", None) or [])
