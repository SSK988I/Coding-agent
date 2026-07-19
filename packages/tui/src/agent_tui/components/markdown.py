"""Markdown rendering component.

Parses markdown via **mistune** (Markdown parser) into an
AST, then renders each token to ANSI-styled terminal lines using a
``MarkdownTheme`` of style functions.

Supported tokens: heading, paragraph, code (fenced), list (nested/ordered/
unordered/task), blockquote, hr, table, inline (strong/em/codespan/link/del/br).
Streaming-aware: trims partial closing code fences to avoid flicker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import mistune

from agent_tui.tui import Component
from agent_tui.utils import apply_background_to_line, visible_width, wrap_text_with_ansi


# ─── theme + style types ──────────────────────────

@dataclass
class DefaultTextStyle:
    """Base styling applied to all markdown text."""
    color: "Callable[[str], str] | None" = None
    bg_color: "Callable[[str], str] | None" = None
    bold: bool = False
    italic: bool = False
    strikethrough: bool = False
    underline: bool = False


@dataclass
class MarkdownTheme:
    """Style functions for markdown elements.

    Each function takes text and returns ANSI-styled text.
    """
    heading: Callable[[str], str] = field(default=lambda t: t)
    link: Callable[[str], str] = field(default=lambda t: t)
    link_url: Callable[[str], str] = field(default=lambda t: t)
    code: Callable[[str], str] = field(default=lambda t: t)
    code_block: Callable[[str], str] = field(default=lambda t: t)
    code_block_border: Callable[[str], str] = field(default=lambda t: t)
    quote: Callable[[str], str] = field(default=lambda t: t)
    quote_border: Callable[[str], str] = field(default=lambda t: t)
    hr: Callable[[str], str] = field(default=lambda t: t)
    list_bullet: Callable[[str], str] = field(default=lambda t: t)
    bold: Callable[[str], str] = field(default=lambda t: t)
    italic: Callable[[str], str] = field(default=lambda t: t)
    strikethrough: Callable[[str], str] = field(default=lambda t: t)
    underline: Callable[[str], str] = field(default=lambda t: t)
    #: Optional syntax highlighter: (code, lang) -> list of styled lines.
    highlight_code: "Callable[[str, str | None], list[str]] | None" = None
    #: Prefix applied to each code block line (default "  ").
    code_block_indent: str = "  "


@dataclass
class MarkdownOptions:
    """Markdown rendering options."""
    preserve_ordered_list_markers: bool = False
    preserve_backslash_escapes: bool = False


# ─── shared mistune parser ─────────────────────────

_md_parser = mistune.create_markdown(
    renderer="ast",
    plugins=["strikethrough", "table", "task_lists"],
)


# ─── Markdown component ──────────────────────────

class Markdown(Component):
    """Renders markdown text to styled terminal lines.

    Args:
        text: The markdown source.
        padding_x: Left/right padding (default 0).
        padding_y: Top/bottom padding (default 0).
        theme: A MarkdownTheme of style functions.
        default_style: Optional base styling for all text.
        options: Rendering options.
    """

    def __init__(
        self,
        text: str = "",
        padding_x: int = 0,
        padding_y: int = 0,
        theme: MarkdownTheme | None = None,
        default_style: DefaultTextStyle | None = None,
        options: MarkdownOptions | None = None,
    ) -> None:
        self._text = text
        self._padding_x = padding_x
        self._padding_y = padding_y
        self._theme = theme or MarkdownTheme()
        self._default_style = default_style
        self._options = options or MarkdownOptions()

        # Cache.
        self._cached_text: "str | None" = None
        self._cached_width: int = -1
        self._cached_lines: "list[str] | None" = None

    @property
    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text
        self.invalidate()

    def invalidate(self) -> None:
        self._cached_text = None
        self._cached_width = -1
        self._cached_lines = None

    def render(self, width: int) -> list[str]:
        # Cache check.
        if (
            self._cached_lines is not None
            and self._cached_text == self._text
            and self._cached_width == width
        ):
            return self._cached_lines

        content_width = max(1, width - self._padding_x * 2)

        # Empty text → no lines.
        if not self._text or not self._text.strip():
            result: list[str] = []
            self._cached_text = self._text
            self._cached_width = width
            self._cached_lines = result
            return result

        # Normalize tabs → 3 spaces.
        normalized = self._text.replace("\t", "   ")

        # Parse.
        # ``_md_parser`` uses mistune's AST renderer, so it returns a list of
        # token dicts. mistune's stubs type the return as ``str | list[dict]``
        # (because they don't specialize on ``renderer="ast"``), so narrow
        # explicitly — otherwise Pylance flags every ``token.get(...)`` below.
        parsed = _md_parser(normalized)
        tokens: list[dict] = parsed if isinstance(parsed, list) else []
        _trim_partial_closing_fences(tokens)

        # Render tokens to styled lines.
        rendered_lines: list[str] = []
        for i, token in enumerate(tokens):
            next_type = tokens[i + 1].get("type") if i + 1 < len(tokens) else None
            rendered_lines.extend(self._render_token(token, content_width, next_type))

        # Wrap lines.
        wrapped_lines: list[str] = []
        for line in rendered_lines:
            wrapped_lines.extend(wrap_text_with_ansi(line, content_width))

        # Add margins + background.
        left_margin = " " * self._padding_x
        right_margin = " " * self._padding_x
        bg_fn = self._default_style.bg_color if self._default_style else None
        content_lines: list[str] = []
        for line in wrapped_lines:
            line_with_margins = left_margin + line + right_margin
            if bg_fn:
                content_lines.append(apply_background_to_line(line_with_margins, width, bg_fn))
            else:
                vis_len = visible_width(line_with_margins)
                padding_needed = max(0, width - vis_len)
                content_lines.append(line_with_margins + " " * padding_needed)

        # Top/bottom padding.
        empty_line = " " * width
        empty_lines: list[str] = []
        for _ in range(self._padding_y):
            empty_lines.append(apply_background_to_line(empty_line, width, bg_fn) if bg_fn else empty_line)

        result = [*empty_lines, *content_lines, *empty_lines]

        # Update cache.
        self._cached_text = self._text
        self._cached_width = width
        self._cached_lines = result

        return result if result else [""]

    # ─── default style application ───────────────

    def _apply_default_style(self, text: str) -> str:
        if not self._default_style:
            return text
        styled = text
        if self._default_style.color:
            styled = self._default_style.color(styled)
        if self._default_style.bold:
            styled = self._theme.bold(styled)
        if self._default_style.italic:
            styled = self._theme.italic(styled)
        if self._default_style.strikethrough:
            styled = self._theme.strikethrough(styled)
        if self._default_style.underline:
            styled = self._theme.underline(styled)
        return styled

    # ─── block token rendering ───────────────────

    def _render_token(
        self,
        token: dict,
        width: int,
        next_type: "str | None" = None,
        style_prefix: str = "",
    ) -> list[str]:
        ttype = token.get("type", "")
        lines: list[str] = []

        if ttype == "heading":
            level = token.get("attrs", {}).get("level", 1)
            heading_text = self._render_inline(token.get("children", []))
            if level == 1:
                styled = self._theme.heading(self._theme.bold(self._theme.underline(heading_text)))
            else:
                styled = self._theme.heading(self._theme.bold(heading_text))
            if level >= 3:
                styled = self._theme.heading("# " * 0) + styled  # no prefix for >=3
            lines.append(styled)
            if next_type and next_type not in ("blank_line", "space"):
                lines.append("")

        elif ttype == "paragraph":
            text = self._render_inline(token.get("children", []))
            lines.append(text)
            if next_type and next_type not in ("list", "blank_line", "space"):
                lines.append("")

        elif ttype == "block_code":
            lang = token.get("attrs", {}).get("info") or None
            raw = token.get("raw", "")
            indent = self._theme.code_block_indent
            lines.append(self._theme.code_block_border(f"```{lang or ''}"))
            if self._theme.highlight_code:
                for hl_line in self._theme.highlight_code(raw, lang):
                    lines.append(f"{indent}{hl_line}")
            else:
                for code_line in raw.rstrip("\n").split("\n"):
                    lines.append(f"{indent}{self._theme.code_block(code_line)}")
            lines.append(self._theme.code_block_border("```"))
            if next_type and next_type not in ("blank_line", "space"):
                lines.append("")

        elif ttype == "list":
            lines.extend(self._render_list(token, 0, width))

        elif ttype == "block_quote":
            quote_content_width = max(1, width - 2)
            quote_lines: list[str] = []
            for child in token.get("children", []):
                child_type = None
                quote_lines.extend(self._render_token(child, quote_content_width, child_type))
            # Strip trailing empty lines.
            while quote_lines and quote_lines[-1] == "":
                quote_lines.pop()
            for qline in quote_lines:
                styled = self._theme.quote(self._theme.italic(qline))
                for wrapped in wrap_text_with_ansi(styled, quote_content_width):
                    lines.append(self._theme.quote_border("│ ") + wrapped)
            if next_type and next_type not in ("blank_line", "space"):
                lines.append("")

        elif ttype == "thematic_break" or ttype == "hr":
            lines.append(self._theme.hr("─" * min(width, 80)))
            if next_type and next_type not in ("blank_line", "space"):
                lines.append("")

        elif ttype == "table":
            lines.extend(self._render_table(token, width, next_type))

        elif ttype in ("blank_line", "space"):
            lines.append("")

        elif ttype == "block_html":
            raw = token.get("raw", "").strip()
            if raw:
                lines.append(self._apply_default_style(raw))

        return lines

    # ─── list rendering ──────────────────────────

    def _render_list(self, token: dict, depth: int, width: int) -> list[str]:
        lines: list[str] = []
        ordered = token.get("attrs", {}).get("ordered", False)
        children = token.get("children", [])
        indent = "  " * depth

        for idx, item in enumerate(children):
            if ordered:
                marker = f"{idx + 1}. "
            else:
                marker = self._theme.list_bullet("• ")
            item_lines = self._render_list_item(item, width - len(indent) - 2)
            for i, line in enumerate(item_lines):
                if i == 0:
                    lines.append(f"{indent}{marker}{line}")
                else:
                    lines.append(f"{indent}  {line}")
        return lines

    def _render_list_item(self, item: dict, width: int) -> list[str]:
        lines: list[str] = []
        for child in item.get("children", []):
            child_type = child.get("type", "")
            # mistune 3.x wraps list item text as "block_text" (not "paragraph").
            if child_type in ("paragraph", "block_text"):
                lines.append(self._render_inline(child.get("children", [])))
            elif child_type == "block_code":
                lines.extend(self._render_token(child, width))
            elif child_type == "list":
                lines.extend(self._render_list(child, 1, width))
            elif child_type == "block_quote":
                lines.extend(self._render_token(child, width))
            elif child_type not in ("blank_line", "space"):
                lines.extend(self._render_token(child, width))
        return lines

    # ─── table rendering ─────────────────────────

    def _render_table(self, token: dict, width: int, next_type: "str | None" = None) -> list[str]:
        """Render a markdown table as a Unicode box-drawing grid.

        Uses a two-tier width model
        (natural vs. min-word), wraps overflowing cells to multiple lines, and
        emits normalized borders so the ``│`` separators line up across every
        row. Borders are NOT theme-colored (only header content is bolded),
        while preserving the requested indentation.

        Args:
            token: mistune table token.
            width: available terminal columns.
            next_type: type of the following token, used to decide whether to
                emit a trailing blank line.
        """
        # ── 1. flatten rows (header + body) ─────────────────────────────
        # mistune 3.x table structure:
        #   token["children"] = [table_head, table_body]
        #   table_head["children"]  = [table_cell, ...]      (cells directly)
        #   table_body["children"]  = [table_row, ...]
        #   table_row["children"]   = [table_cell, ...]
        #   table_cell["children"]  = [inline tokens...]
        rows_text: list[list[str]] = []

        def _extract_cells(row_node: dict) -> list[str]:
            cells: list[str] = []
            for cell in row_node.get("children", []):
                ct = cell.get("type")
                if ct == "table_row":  # body rows wrap cells in a row node
                    return _extract_cells(cell)
                if ct == "table_cell":
                    cells.append(self._render_inline(cell.get("children", [])))
            return cells

        for section in token.get("children", []):
            stype = section.get("type")
            if stype == "table_head":
                row = _extract_cells(section)
                if row:
                    rows_text.append(row)
            elif stype == "table_body":
                for row_node in section.get("children", []):
                    row = _extract_cells(row_node)
                    if row:
                        rows_text.append(row)
            elif stype == "table_row":  # fallback: flat rows (older mistune)
                row = _extract_cells(section)
                if row:
                    rows_text.append(row)

        if not rows_text:
            return []

        num_cols = max(len(r) for r in rows_text)
        # Pad short rows with empty cells so column indexing is safe.
        for r in rows_text:
            while len(r) < num_cols:
                r.append("")

        # ── 2. border overhead & available width ──
        # "│ " + (n-1) * " │ " + " │" = 2 + 3(n-1) + 2 = 3n + 1
        border_overhead = 3 * num_cols + 1
        available_for_cells = width - border_overhead

        # ── 3. too narrow → fall back to raw markdown
        if available_for_cells < num_cols:
            raw = _table_to_raw(token)
            fallback = wrap_text_with_ansi(raw, width) if raw else []
            if next_type and next_type not in ("blank_line", "space"):
                fallback.append("")
            return fallback

        # ── 4. two-tier widths ────────────────────
        max_unbroken = 30
        natural_widths = [0] * num_cols
        min_word_widths = [1] * num_cols
        for row in rows_text:
            for i, cell in enumerate(row):
                natural_widths[i] = max(natural_widths[i], visible_width(cell))
                min_word_widths[i] = max(
                    min_word_widths[i],
                    _longest_word_width(cell, max_unbroken),
                )

        # ── 5. clamp min widths if they exceed available
        min_col_widths = list(min_word_widths)
        min_cells_width = sum(min_col_widths)
        if min_cells_width > available_for_cells:
            min_col_widths = [1] * num_cols
            remaining = available_for_cells - num_cols
            if remaining > 0:
                total_weight = sum(max(0, w - 1) for w in min_word_widths)
                for i in range(num_cols):
                    weight = max(0, min_word_widths[i] - 1)
                    if total_weight > 0:
                        min_col_widths[i] += (weight * remaining) // total_weight
                # distribute leftover (rounding) left-to-right
                allocated = sum(min_col_widths) - num_cols
                leftover = remaining - allocated
                i = 0
                while leftover > 0 and i < num_cols:
                    min_col_widths[i] += 1
                    leftover -= 1
                    i += 1
            min_cells_width = sum(min_col_widths)

        # ── 6. final column widths ────────────────
        total_natural = sum(natural_widths) + border_overhead
        if total_natural <= width:
            col_widths = [
                max(natural_widths[i], min_col_widths[i]) for i in range(num_cols)
            ]
        else:
            total_grow = sum(
                max(0, natural_widths[i] - min_col_widths[i]) for i in range(num_cols)
            )
            extra = max(0, available_for_cells - min_cells_width)
            col_widths = []
            for i in range(num_cols):
                grow_potential = max(0, natural_widths[i] - min_col_widths[i])
                grow = (grow_potential * extra) // total_grow if total_grow > 0 else 0
                col_widths.append(min_col_widths[i] + grow)
            # distribute leftover left-to-right to columns that can still grow
            #
            remaining = available_for_cells - sum(col_widths)
            while remaining > 0:
                grew = False
                for i in range(num_cols):
                    if remaining <= 0:
                        break
                    if col_widths[i] < natural_widths[i]:
                        col_widths[i] += 1
                        remaining -= 1
                        grew = True
                if not grew:
                    break

        # ── 6.5 enforce physical floor per column (CJK overflow guard) ──
        # A column whose cells contain wide glyphs (CJK width=2, emoji, etc.)
        # cannot actually be rendered narrower than that glyph —
        # ``wrap_text_with_ansi(text, 1)`` still yields a width-2 line for a
        # CJK char. The narrow-width reflow at step 5 may have allocated
        # ``col_widths[i] = 1`` for such a column, but the cell would still
        # render at width 2, overflowing the column. The right ``│`` border
        # then wraps to the next visual line and ``Markdown.render``'s
        # subsequent ``wrap_text_with_ansi(line, content_width)`` pass
        # truncates it, producing the symptom "线条没闭合" (borders don't
        # close).
        #
        # Strategy: compute each column's physical floor (the width of its
        # smallest unbreakable glyph). When the floors alone already consume
        # all available space, the word-level reflow from step 5 is moot —
        # use the floors directly as ``col_widths`` so long words (e.g.
        # long ASCII words) wrap into multiple width-2 fragments instead of
        # forcing the column wider and breaking the layout. If the floors
        # still don't fit even one-per-column, fall back to raw markdown
        # text (same escape hatch as ``available_for_cells < num_cols``).
        physical_floors = [0] * num_cols
        for row in rows_text:
            for i in range(min(num_cols, len(row))):
                physical_floors[i] = max(
                    physical_floors[i], _min_renderable_width(row[i])
                )
        if sum(physical_floors) >= available_for_cells:
            # Word-level reflow can't help: every column is already at its
            # physical minimum. Use the floors verbatim (they may equal or
            # slightly exceed available_for_cells; the border + cells stay
            # aligned because both read the same col_widths).
            col_widths = physical_floors[:]
            if sum(col_widths) > available_for_cells:
                # Floors don't even fit one-per-column — raw markdown.
                raw = _table_to_raw(token)
                fallback = wrap_text_with_ansi(raw, width) if raw else []
                if next_type and next_type not in ("blank_line", "space"):
                    fallback.append("")
                return fallback
        else:
            col_widths = [
                max(col_widths[i], physical_floors[i]) for i in range(num_cols)
            ]

        # ── 7. render ─────────────────────────────
        # Borders are emitted without theme coloring.
        #
        # 每列 dash 段长度 = col_widths[i] + 2，对齐内容行 ``│ ... │``
        # 里 ``1 空格 + content(w) + 1 空格`` 的内段宽度。否则边框每列
        # 比内容少 1 个 ``─``，N 列就少 N，右边框 ``┐`` 永远收不上来。
        # 用 ``"─" * (w + 2)`` 而不是 ``"─" * w`` 就能让盒子真正闭合。
        def _border(left: str, mid: str, right: str) -> str:
            return left + mid.join("─" * (w + 2) for w in col_widths) + right

        # Top border: ┌──┬──┬──┐
        lines: list[str] = [_border("┌", "┬", "┐")]

        def _render_row(cells: list[str], bold: bool) -> None:
            """Wrap each cell to its column width and emit as many visual
            lines as the tallest cell needs. Short cells are padded with
            empty strings on their extra lines."""
            cell_lines = [
                wrap_text_with_ansi(cells[i], max(1, col_widths[i]))
                for i in range(num_cols)
            ]
            line_count = max(len(c) for c in cell_lines)
            for line_idx in range(line_count):
                parts = []
                for i in range(num_cols):
                    text = cell_lines[i][line_idx] if line_idx < len(cell_lines[i]) else ""
                    pad = max(0, col_widths[i] - visible_width(text))
                    padded = text + " " * pad
                    # Bold the padded header text so the whole cell is emphasized.
                    parts.append(self._theme.bold(padded) if bold else padded)
                lines.append("│ " + " │ ".join(parts) + " │")

        # Header row + separator.
        _render_row(rows_text[0], bold=True)
        lines.append(_border("├", "┼", "┤"))
        # Body rows.
        for row in rows_text[1:]:
            _render_row(row, bold=False)

        # Bottom border: └──┴──┴──┘
        lines.append(_border("└", "┴", "┘"))

        # ── 8. trailing blank line ────────────────
        if next_type and next_type not in ("blank_line", "space"):
            lines.append("")
        return lines

    # ─── inline token rendering ──────────────────

    def _render_inline(self, tokens: list[dict], style_prefix: str = "") -> str:
        result = ""
        for token in tokens:
            ttype = token.get("type", "")
            if ttype == "text":
                result += self._apply_default_style(token.get("raw", ""))
            elif ttype == "strong":
                inner = self._render_inline(token.get("children", []))
                result += self._theme.bold(inner)
            elif ttype == "emphasis":
                inner = self._render_inline(token.get("children", []))
                result += self._theme.italic(inner)
            elif ttype == "codespan":
                result += self._theme.code(token.get("raw", ""))
            elif ttype == "link":
                inner = self._render_inline(token.get("children", []))
                url = token.get("attrs", {}).get("url", "")
                styled = self._theme.link(self._theme.underline(inner))
                # Fallback: show URL in parens if text differs from href.
                text_raw = "".join(c.get("raw", "") for c in token.get("children", []))
                href_clean = url[7:] if url.startswith("mailto:") else url
                if text_raw == url or text_raw == href_clean:
                    result += styled
                else:
                    result += styled + self._theme.link_url(f" ({url})")
            elif ttype == "strikethrough" or ttype == "delete":
                inner = self._render_inline(token.get("children", []))
                result += self._theme.strikethrough(inner)
            elif ttype == "linebreak":
                result += "\n"
            elif ttype == "image":
                alt = "".join(c.get("raw", "") for c in token.get("children", []))
                result += self._theme.link_url(f"[{alt}]")
            elif ttype == "inline_html":
                result += self._apply_default_style(token.get("raw", ""))
            else:
                raw = token.get("raw", "")
                if raw:
                    result += self._apply_default_style(raw)
        return result


# ─── table helpers ──────────────────────────────


def _longest_word_width(text: str, max_width: int) -> int:
    """Visible width of the longest whitespace-delimited word in ``text``,
    capped at ``max_width``.

    This is the floor below which a table column cannot shrink without
    breaking a word; the cap prevents one giant unbreakable token (e.g. a
    URL) from forcing an absurdly wide column.
    """
    longest = 0
    for word in text.split():
        longest = max(longest, visible_width(word))
    return min(longest, max_width)


def _min_renderable_width(cell_text: str) -> int:
    """Smallest physical width a table cell can occupy.

    A CJK glyph has visible width 2 and cannot be split — even when
    :func:`wrap_text_with_ansi` is asked for width 1 it still emits the
    whole glyph on a width-2 line. This is the hard physical floor for a
    column, distinct from :func:`_longest_word_width` (word-level, and
    force-compressed by the narrow-width reflow below).

    Without honoring this floor, a column allocated width 1 for CJK
    content overflows by 1 column on every row, the right border ``│``
    lands on the next visual line, and the table's borders no longer
    close — the symptom "线条没闭合".
    """
    if not cell_text:
        return 0
    first = wrap_text_with_ansi(cell_text, 1)
    return visible_width(first[0]) if first else 0


def _table_to_raw(token: dict) -> str:
    """Rebuild plain-text markdown from a mistune ``table`` AST node.

    Used by the narrow-width fallback paths in :meth:`_render_table` —
    unlike marked, mistune's AST mode does not populate ``token["raw"]``,
    so we synthesize a pipe-table string from the parsed cells so the
    fallback can hand it to :func:`wrap_text_with_ansi`.
    """
    rows: list[list[str]] = []

    def _collect(row_node: dict) -> list[str]:
        cells: list[str] = []
        for cell in row_node.get("children", []):
            ct = cell.get("type")
            if ct == "table_row":
                return _collect(cell)
            if ct == "table_cell":
                # Inline tokens → plain text (strip styling; fallback is raw).
                inner = Markdown()
                cells.append(inner._render_inline(cell.get("children", [])))
        return cells

    for section in token.get("children", []):
        stype = section.get("type")
        if stype == "table_head":
            row = _collect(section)
            if row:
                rows.append(row)
        elif stype == "table_body":
            for row_node in section.get("children", []):
                row = _collect(row_node)
                if row:
                    rows.append(row)
        elif stype == "table_row":
            row = _collect(section)
            if row:
                rows.append(row)

    if not rows:
        return ""
    num_cols = max(len(r) for r in rows)
    lines = ["| " + " | ".join(r + [""] * (num_cols - len(r))) + " |" for r in rows]
    # Insert a separator after the (first) header row.
    sep = "| " + " | ".join(["---"] * num_cols) + " |"
    lines.insert(1, sep)
    # Strip ANSI from the rebuilt text (cells may carry inline styling).
    return _strip_ansi_codes("\n".join(lines))


def _strip_ansi_codes(text: str) -> str:
    """Remove CSI/OSC/APC escape sequences (used by :func:`_table_to_raw`)."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\x1b" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "[":
                # CSI: terminated by 0x40-0x7E
                j = i + 2
                while j < n and not (0x40 <= ord(text[j]) <= 0x7E):
                    j += 1
                i = j + 1
                continue
            if nxt == "]" or nxt == "_":
                # OSC/APC: terminated by BEL or ST (ESC \)
                j = i + 2
                while j < n:
                    if text[j] == "\x07":
                        j += 1
                        break
                    if text[j] == "\x1b" and j + 1 < n and text[j + 1] == "\\":
                        j += 2
                        break
                    j += 1
                i = j
                continue
        out.append(ch)
        i += 1
    return "".join(out)


# ─── streaming-aware fence trimming ────────────────

def _trim_partial_closing_fences(tokens: list[dict]) -> None:
    """Trim partial closing code fences to avoid flicker during streaming.

    When code is streamed token-by-token, the closing ```
    may arrive incomplete (e.g. just ``). This trims such partial closers so
    the code block doesn't shrink/grow flickeringly as the final chars arrive.
    """
    if not tokens:
        return
    token = tokens[-1]
    if token.get("type") == "list":
        items = token.get("children", [])
        if items:
            _trim_partial_closing_fences(items[-1].get("children", []))
        return
    if token.get("type") == "block_quote":
        _trim_partial_closing_fences(token.get("children", []))
        return
    if token.get("type") != "block_code":
        return

    marker = token.get("marker", "")
    raw = token.get("raw", "")
    lines = raw.split("\n")
    last_line = lines[-1] if lines else ""
    if not marker or not last_line:
        return
    if len(last_line) >= len(marker):
        return
    if last_line != marker[0] * len(last_line):
        return
    # Trim the partial closer.
    trimmed = raw[: -len(last_line)].rstrip("\n")
    token["raw"] = trimmed
