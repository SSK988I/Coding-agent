"""AssistantMessageComponent.

Renders a complete assistant message — thinking blocks and text blocks — in
content-array order. Thinking renders first (italic, thinkingText color), then
text, matching the model's natural ReAct order (Reason → Act).

Key design:
  - Single component wrapping one contentContainer.
  - update_content() clears + rebuilds children each call, but only adds a
    child when the content block has visible text. Empty thinking/text
    renders zero lines — no placeholder rows that would change height mid-
    stream and cause flicker.
  - Spacer(1) only when there's visible content, and between thinking and
    text only when text follows.

"""
from __future__ import annotations

from typing import Any

from agent_tui import Container, Markdown, Spacer
from agent_tui.components.markdown import DefaultTextStyle, MarkdownTheme


class AssistantMessageComponent(Container):
    """Renders an assistant message's thinking + text blocks in order.

    Call :meth:`update_content` with the current (possibly partial)
    AssistantMessage whenever it changes. The component rebuilds its children
    from ``message.content``, rendering only blocks with visible text.
    """

    def __init__(
        self,
        message: Any | None = None,
        *,
        markdown_theme: MarkdownTheme | None = None,
        thinking_color: Any = None,
        output_pad: int = 1,
    ) -> None:
        super().__init__()
        self._markdown_theme = markdown_theme
        self._thinking_color = thinking_color  # callable: str -> styled str
        self._output_pad = output_pad
        self._content_container = Container()
        self.add_child(self._content_container)
        self._last_message: Any | None = None
        if message is not None:
            self.update_content(message)

    def update_content(self, message: Any) -> None:
        """Rebuild children from message.content.

        Only content blocks with visible text are rendered, so an empty
        thinking or text block contributes zero lines — avoiding height
        jumps during streaming.
        """
        self._last_message = message
        self._content_container.clear()

        content = getattr(message, "content", None) or []
        # Only text/thinking blocks render here; toolCalls are shown as
        # separate tool cards by the app.
        def _visible(block: Any) -> bool:
            btype = getattr(block, "type", None)
            if btype == "text":
                return bool(getattr(block, "text", "").strip())
            if btype == "thinking":
                return bool(getattr(block, "thinking", "").strip())
            return False

        has_visible = any(_visible(c) for c in content)
        if has_visible:
            self._content_container.add_child(Spacer(1))

        for i, block in enumerate(content):
            btype = getattr(block, "type", None)
            if btype == "text" and getattr(block, "text", "").strip():
                self._content_container.add_child(
                    Markdown(
                        block.text.strip(),
                        padding_x=self._output_pad,
                        padding_y=0,
                        theme=self._markdown_theme,
                    )
                )
            elif btype == "thinking" and getattr(block, "thinking", "").strip():
                # Thinking renders before any text (ReAct order). Add a Spacer
                # only when another visible block follows, avoiding a stray
                # blank line at the end.
                has_visible_after = any(
                    _visible(c) for c in content[i + 1:]
                )
                default_style = None
                if self._thinking_color is not None:
                    default_style = DefaultTextStyle(
                        color=self._thinking_color,
                        italic=True,
                    )
                self._content_container.add_child(
                    Markdown(
                        block.thinking.strip(),
                        padding_x=self._output_pad,
                        padding_y=0,
                        theme=self._markdown_theme,
                        default_style=default_style,
                    )
                )
                if has_visible_after:
                    self._content_container.add_child(Spacer(1))
