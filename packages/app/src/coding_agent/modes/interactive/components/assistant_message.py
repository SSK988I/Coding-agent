"""AssistantMessageComponent — renders a complete assistant message.

A single container rebuilds
its content subtree (clear + add) on every ``update_content`` call, keyed off
the *full* partial message. Empty thinking/text blocks render nothing, which
keeps the layout stable during streaming (no empty placeholder lines).

"""
from __future__ import annotations

from typing import Any

from agent_tui import Container, Markdown, Spacer, Text
from agent_tui.components.markdown import DefaultTextStyle, MarkdownTheme

# ─── OSC 133 prompt-zone markers (terminal shell integration) ──────────
#: Marks the start of an assistant prompt zone.
OSC133_ZONE_START = "\x1b]133;A\x07"
#: Marks the end of the prompt (input starts here).
OSC133_ZONE_END = "\x1b]133;B\x07"
#: Marks the end of the prompt output.
OSC133_ZONE_FINAL = "\x1b]133;C\x07"


def _block_text(block: Any) -> str:
    """Return the text payload of a content block (text or thinking)."""
    if getattr(block, "type", None) == "text":
        return getattr(block, "text", "") or ""
    if getattr(block, "type", None) == "thinking":
        return getattr(block, "thinking", "") or ""
    return ""


def _is_visible(block: Any) -> bool:
    """True if a text/thinking block has non-whitespace content."""
    return bool(_block_text(block).strip())


class AssistantMessageComponent(Container):
    """Renders a complete (or streaming) assistant message.

    Call ``update_content(message)`` on every ``message_update`` event; it
    rebuilds the content subtree from scratch. Empty blocks render nothing so
    the layout stays stable while the message streams in.

    Args:
        message: Optional initial AssistantMessage.
        hide_thinking_block: If True, show a static "Thinking..." label instead
            of the raw thinking trace.
        markdown_theme: Theme for the Markdown renderers.
        theme: Theme with ``fg``/``italic`` for the hidden-thinking label and
            error text. May be None if you supply no message and don't render.
        hidden_thinking_label: Label shown when ``hide_thinking_block`` is True.
        output_pad: Horizontal padding for the content blocks.
    """

    def __init__(
        self,
        message: Any | None = None,
        hide_thinking_block: bool = False,
        markdown_theme: MarkdownTheme | None = None,
        theme: Any | None = None,
        hidden_thinking_label: str = "Thinking...",
        output_pad: int = 1,
    ) -> None:
        super().__init__()
        self._hide_thinking_block = hide_thinking_block
        self._markdown_theme = markdown_theme
        self._theme = theme
        self._hidden_thinking_label = hidden_thinking_label
        self._output_pad = output_pad

        # Container for text/thinking content.
        self._content_container = Container()
        self.add_child(self._content_container)

        self._last_message: Any | None = None
        self._has_tool_calls = False

        if message is not None:
            self.update_content(message)

    # ── live-updatable setters ──────────────────────────────────────────

    def set_hide_thinking_block(self, hide: bool) -> None:
        self._hide_thinking_block = hide
        if self._last_message is not None:
            self.update_content(self._last_message)

    def set_hidden_thinking_label(self, label: str) -> None:
        self._hidden_thinking_label = label
        if self._last_message is not None:
            self.update_content(self._last_message)

    def set_output_pad(self, padding: int) -> None:
        self._output_pad = padding
        if self._last_message is not None:
            self.update_content(self._last_message)

    def invalidate(self) -> None:
        super().invalidate()
        if self._last_message is not None:
            self.update_content(self._last_message)

    # ── core rebuild ────────────────────────────────────────────────────

    def update_content(self, message: Any) -> None:
        """Rebuild the content subtree from ``message``."""
        self._last_message = message
        self._content_container.clear()

        content = list(getattr(message, "content", []) or [])
        has_visible_content = any(_is_visible(c) for c in content)

        if has_visible_content:
            self._content_container.add_child(Spacer(1))

        # Render content blocks in order.
        for i, block in enumerate(content):
            btype = getattr(block, "type", None)
            if btype == "text" and _is_visible(block):
                self._content_container.add_child(
                    Markdown(_block_text(block).strip(), self._output_pad, 0, self._markdown_theme)
                )
            elif btype == "thinking" and _is_visible(block):
                # Spacing only when another visible block follows.
                has_visible_after = any(_is_visible(c) for c in content[i + 1:])
                if self._hide_thinking_block:
                    label = self._hidden_thinking_label
                    if self._theme is not None:
                        label = self._theme.italic(self._theme.fg("thinkingText", label))
                    self._content_container.add_child(Text(label, self._output_pad, 0))
                    if has_visible_after:
                        self._content_container.add_child(Spacer(1))
                else:
                    style = None
                    if self._theme is not None:
                        style = DefaultTextStyle(
                            color=lambda t, _th=self._theme: _th.fg("thinkingText", t),
                            italic=True,
                        )
                    self._content_container.add_child(
                        Markdown(
                            _block_text(block).strip(), self._output_pad, 0,
                            self._markdown_theme, default_style=style,
                        )
                    )
                    if has_visible_after:
                        self._content_container.add_child(Spacer(1))

        # Stop-reason handling.
        self._has_tool_calls = any(getattr(c, "type", None) == "toolCall" for c in content)
        stop_reason = getattr(message, "stop_reason", None)
        if stop_reason == "length":
            self._content_container.add_child(Spacer(1))
            self._add_error(
                "错误：模型因达到最大输出 Token 限制而停止，回复内容可能不完整。"
            )
        elif not self._has_tool_calls:
            if stop_reason == "aborted":
                err = getattr(message, "error_message", "") or ""
                abort_msg = err if err and err != "Request was aborted" else "操作已中止"
                self._content_container.add_child(Spacer(1))
                self._add_error(abort_msg)
            elif stop_reason == "error":
                err = getattr(message, "error_message", "") or "未知错误"
                self._content_container.add_child(Spacer(1))
                self._add_error(f"错误：{err}")

    def _add_error(self, text: str) -> None:
        """添加错误文本行；主题可用时应用错误样式。"""
        if self._theme is not None:
            text = self._theme.fg("error", text)
        self._content_container.add_child(Text(text, self._output_pad, 0))

    # ── render with OSC133 wrap ────────────

    def render(self, width: int) -> list[str]:
        lines = super().render(width)
        if self._has_tool_calls or not lines:
            return lines
        lines[0] = OSC133_ZONE_START + lines[0]
        lines[-1] = OSC133_ZONE_END + OSC133_ZONE_FINAL + lines[-1]
        return lines
