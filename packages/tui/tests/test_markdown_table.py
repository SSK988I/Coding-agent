"""Tests for Markdown table rendering.

Core invariant: **every non-empty line of a rendered table
has the same visible width**. This is what makes the Unicode box borders
(`┌─┬─┐│├─┼─┤└─┴─┘`) line up. A previous implementation broke this invariant
because it scaled column widths by a single float factor and never wrapped
overflowing cells — both fixed by the rewrite.

These tests assert that invariant across simple tables, CJK content,
wrapping, narrow-width fallback, proportional distribution, and empty cells.
"""
from __future__ import annotations

import re

import pytest

from agent_tui.components.markdown import Markdown, _longest_word_width
from agent_tui.theme import get_markdown_theme, load_theme
from agent_tui.utils import visible_width

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip(s: str) -> str:
    return _ANSI_RE.sub("", s)


@pytest.fixture(scope="module")
def md_theme():
    return get_markdown_theme(load_theme("dark"))


def _render(text: str, width: int, md_theme) -> list[str]:
    """Render a markdown string to a list of lines at the given width."""
    md = Markdown(text, 0, 0, md_theme)
    return md.render(width)


def _assert_aligned(lines: list[str]) -> None:
    """Assert the table is properly closed and every row lines up.

    Two invariants:

    1. **Box closed**: top border (``┌``), separators (``├``), and bottom
       border (``└``) must all have the same visible width as the content
       rows (``│``). Before this was enforced, each border column had one
       fewer ``─`` than the matching ``│ ... │`` content segment, so the
       right ``┐``/``┤``/``┘`` corner never lined up — the box never
       closed. ``Markdown.render`` pads every line to ``width`` with
       trailing spaces, so comparing raw widths cannot catch this;
       ``rstrip()`` first to expose the real content width.

    2. **Uniform**: all rstripped non-empty lines share that width.
    """
    stripped = [line.rstrip() for line in lines if line and line.strip()]
    assert stripped, "table produced no non-empty lines"
    widths = [visible_width(line) for line in stripped]
    assert len(set(widths)) == 1, (
        f"table is misaligned; visible widths differ: {widths}\n"
        + "\n".join(repr(line) for line in stripped)
    )


# ── _longest_word_width helper ────────────────────────────────────────


def test_longest_word_width_basic():
    assert _longest_word_width("hello world foo", 30) == 5  # "hello"


def test_longest_word_width_cap():
    """A single long word is capped at max_width."""
    assert _longest_word_width("a" * 100, 30) == 30


def test_longest_word_width_cjk():
    """CJK characters count as 2 cells each."""
    assert _longest_word_width("中文测试", 30) == 8  # 4 chars × 2


def test_longest_word_width_empty():
    assert _longest_word_width("", 30) == 0


# ── basic alignment ───────────────────────────────────────────────────


def test_simple_table_aligned(md_theme):
    """A simple 3-col table: every line has the same visible width."""
    lines = _render(
        "| Name | Age | City |\n"
        "|------|-----|------|\n"
        "| Alice | 30 | Beijing |\n"
        "| Bob | 25 | Shanghai |",
        60,
        md_theme,
    )
    _assert_aligned(lines)


def test_simple_table_has_all_border_chars(md_theme):
    """All 11 Unicode box-drawing glyphs appear in the right rows."""
    lines = _render(
        "| a | b |\n|---|---|\n| 1 | 2 |",
        30,
        md_theme,
    )
    body = "\n".join(lines)
    assert "┌" in body and "┐" in body  # top corners
    assert "└" in body and "┘" in body  # bottom corners
    assert "├" in body and "┤" in body  # header separator tees
    assert "┬" in body  # top tee
    assert "┴" in body  # bottom tee
    assert "┼" in body  # cross
    assert "│" in body  # vertical separator
    assert "─" in body  # horizontal fill


def test_border_chars_not_theme_colored(md_theme):
    """Borders are not wrapped in code_block_border; they are emitted as raw strings.

    The previous code wrapped every border in theme.code_block_border(...).
    Only header *content* gets theme.bold. This test confirms borders are
    raw (no ANSI escapes on the top border line).
    """
    lines = _render(
        "| a | b |\n|---|---|\n| 1 | 2 |",
        30,
        md_theme,
    )
    # First line is the top border; it should contain NO ANSI escapes.
    assert _ANSI_RE.search(lines[0]) is None, (
        f"top border should be raw, got: {lines[0]!r}"
    )


def test_single_column_box_closes(md_theme):
    """Regression: a single-column table must close its box.

    Symptom before the fix: each border column carried one fewer ``─`` than
    the matching ``│ ... │`` content segment, so the right corner never
    landed under the right edge of the content rows. For a 1-column table
    with content ``a`` (width 1), the broken output was ::

        ┌──┐        ← 2 dashes
        │ a │       ← content segment width 3
        └──┘        ← 2 dashes  (box doesn't close: 2 ≠ 3)

    After the fix the dash run is ``col_width + 2`` to match the
    ``"<space> <content> <space>"`` interior, so all five lines share
    visible width 5.
    """
    lines = _render("| a |\n|---|\n| b |", 20, md_theme)
    _assert_aligned(lines)
    # Right corners must be the last visible char on their rows (i.e. the
    # box closes flush — no missing dash before the corner).
    assert lines[0].rstrip().endswith("┐")
    assert lines[2].rstrip().endswith("┤")
    assert lines[-1].rstrip().endswith("┘")


# ── CJK alignment ─────────────────────────────────────────────────────


def test_cjk_table_aligned(md_theme):
    """CJK (2-wide) characters don't break alignment — the visible width
    function counts them as 2 cells, so the column widths accommodate them."""
    lines = _render(
        "| 名称 | 数量 | 备注 |\n"
        "|------|------|------|\n"
        "| 苹果 | 10 | 新鲜 |\n"
        "| 香蕉 | 25 | 进口 |",
        50,
        md_theme,
    )
    _assert_aligned(lines)
    # The rendered CJK chars should be present (after stripping ANSI).
    body = _strip("\n".join(lines))
    assert "苹果" in body and "香蕉" in body


def test_mixed_cjk_ascii_aligned(md_theme):
    """A column with both ASCII and CJK content stays aligned."""
    lines = _render(
        "| key | 值 |\n|---|---|\n| foo | 测试值 |",
        30,
        md_theme,
    )
    _assert_aligned(lines)


# ── wrapping ──────────────────────────────────────────────────────────


def test_long_cell_wraps_and_aligns(md_theme):
    """When a cell's content exceeds the column width, it wraps to multiple
    visual lines. Shorter cells in the same row pad with empty strings so
    the borders stay aligned."""
    lines = _render(
        "| Field | Value |\n"
        "|-------|-------|\n"
        "| name | this is a very long value that should wrap |\n"
        "| short | x |",
        40,
        md_theme,
    )
    _assert_aligned(lines)
    # The wrapping row should have produced more than the minimum 5 lines
    # (top border, header, separator, body, bottom border).
    non_blank = [line for line in lines if line.strip()]
    assert len(non_blank) > 5, f"expected wrapping to produce extra lines, got {len(non_blank)}"


def test_wrapped_cell_short_cell_pads_empty(md_theme):
    """When a cell wraps but its neighbor doesn't, the neighbor's extra
    line must be empty (padded to column width), not missing."""
    lines = _render(
        "| a | bbbbbbbbbbbb |\n|---|---|\n| x | y |",
        20,
        md_theme,
    )
    # All lines still aligned.
    _assert_aligned(lines)


# ── narrow-width fallback ─────────────────────────────────────────────


def test_too_narrow_falls_back_no_box(md_theme):
    """When the terminal is too narrow to fit even 1 char per column
    (available_for_cells < num_cols), fall back to rendering raw markdown.
    Critically: NO box-drawing chars should be emitted (that was the bug —
    the old code drew a broken misaligned box)."""
    lines = _render(
        "| a | b | c |\n|---|---|---|\n| 1 | 2 | 3 |",
        5,
        md_theme,
    )
    body = "\n".join(lines)
    # No box-drawing characters at all.
    for ch in "┌┐└┘├┤┬┴┼│─":
        assert ch not in body, f"fallback should not draw box, found {ch!r}"


# ── proportional column-width distribution ────────────────────────────


def test_wide_table_compresses_and_aligns(md_theme):
    """A table whose natural width exceeds the terminal compresses columns
    (proportional shrink from min-word baseline) and still aligns."""
    lines = _render(
        "| short | this column has a very long header name |\n"
        "|-------|-------|\n"
        "| a | b |",
        40,
        md_theme,
    )
    _assert_aligned(lines)


def test_columns_fit_within_width(md_theme):
    """No line should exceed the requested width (the whole point of the
    width-distribution algorithm)."""
    width = 45
    lines = _render(
        "| name | description | status |\n"
        "|------|-------------|--------|\n"
        "| alpha | first option | active |\n"
        "| beta | second option with extra detail | pending |",
        width,
        md_theme,
    )
    for line in lines:
        if line:
            assert visible_width(line) <= width, (
                f"line {visible_width(line)} > {width}: {line!r}"
            )


# ── empty / missing cells ─────────────────────────────────────────────


def test_empty_cell_aligned(md_theme):
    """A missing cell value (empty between pipes) still aligns."""
    lines = _render(
        "| a | b | c |\n|---|---|---|\n| 1 | | 3 |",
        30,
        md_theme,
    )
    _assert_aligned(lines)


def test_ragged_rows_padded(md_theme):
    """A row with fewer cells than the header gets padded with empty cells
    and still aligns.

    Note: the row pipe must be present even for trailing empty cells
    (``| 1 | 2 | |``) — mistune treats ``| 1 | 2 |`` (no trailing pipe) as
    a paragraph, not a table row, so it never reaches the table renderer.
    The empty third cell exercises the padding path.
    """
    lines = _render(
        "| a | b | c |\n|---|---|---|\n| 1 | 2 | |",  # third cell empty
        30,
        md_theme,
    )
    _assert_aligned(lines)


# ── header bolding ────────────────────────────────────────────────────


def test_header_is_bold_body_is_not(md_theme):
    """The header row cells get theme.bold applied; body cells do not.
    Bold is applied to the *padded* text (so width isn't affected)."""
    lines = _render(
        "| h |\n|---|\n| body |",
        20,
        md_theme,
    )
    # Find the header line (first line containing the bold marker that's a
    # data row, i.e. starts with │). Body row also starts with │ but has no bold.
    data_rows = [line for line in lines if _strip(line).startswith("│")]
    assert len(data_rows) >= 2, f"expected header + body rows, got {data_rows}"
    header, body = data_rows[0], data_rows[1]
    # theme.bold wraps text in \x1b[1m ... \x1b[22m.
    assert "\x1b[1m" in header, f"header should be bold: {header!r}"
    assert "\x1b[1m" not in body, f"body should NOT be bold: {body!r}"


def test_bold_applied_after_padding(md_theme):
    """The bold ANSI codes wrap the already-padded cell, so visible_width
    of the header line equals visible_width of the body line."""
    lines = _render(
        "| h |\n|---|\n| body |",
        20,
        md_theme,
    )
    _assert_aligned(lines)  # this implicitly verifies bold adds 0 width


# ── trailing blank line (next_type spacing) ───────────────────────────


def test_trailing_blank_line_when_next_token(md_theme):
    """When a table is followed by another block (next_type set), a blank
    line is appended."""
    table_then_para = (
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "Some paragraph after the table."
    )
    lines = _render(table_then_para, 30, md_theme)
    # There should be at least one blank line between the table bottom
    # border and the paragraph text.
    border_idx = None
    for i, line in enumerate(lines):
        if _strip(line).startswith("└"):
            border_idx = i
            break
    assert border_idx is not None, "no bottom border found"
    # At least one empty line after the border before the paragraph.
    after_border = lines[border_idx + 1:]
    assert any(not line.strip() for line in after_border), (
        f"expected a blank line after table, got {after_border!r}"
    )


# ── CJK physical-floor guard (borders-close-at-narrow-width fix) ──────


_CJK_TABLE = (
    "| 项目 | 语言 | 流式 |\n"
    "|---|---|---|\n"
    "| agent | Python | 是 |"
)


@pytest.mark.parametrize("width", [16, 18, 20, 25, 30, 40, 60, 80])
def test_cjk_table_borders_close(md_theme, width):
    """A CJK-heavy table must render with closed borders at every width
    that can fit one wide glyph per column.

    Regression for the symptom "线条没闭合": when the narrow-width reflow
    allocated a column width of 1 but its smallest cell glyph was a
    width-2 CJK char, the cell overflowed, the right ``│`` border wrapped
    to the next visual line, and ``Markdown.render``'s subsequent
    ``wrap_text_with_ansi(line, content_width)`` pass truncated it — so
    every data row was missing its closing ``│`` and the box didn't close.
    """
    lines = _render(_CJK_TABLE, width, md_theme)
    # Every rendered line must share one visible width (the core invariant).
    _assert_aligned(lines)
    # Every border/data line that starts with a box char must end with one
    # of the matching closing box chars (no orphaned │ on the next line).
    open_chars = "┌├└│"
    close_chars = "┐┤┘│"
    for i, line in enumerate(lines):
        s = _strip(line)
        if not s or not s.strip():
            continue
        first = s.lstrip()[0]
        if first in open_chars:
            last = s.rstrip()[-1]
            assert last in close_chars, (
                f"width={width} line {i} starts with {first!r} ({open_chars!r}) "
                f"but ends with {last!r}; expected one of {close_chars!r} — "
                f"the right border is missing.\nline={s!r}"
            )


def test_cjk_table_narrow_falls_back_to_raw(md_theme):
    """Below the minimum width needed for one wide glyph per column, the
    table degrades to wrapped raw markdown instead of producing a broken
    box."""
    # 3 cols × 2 (CJK floor) + 3*3+1 (border overhead) = 16 is the minimum.
    # At width 10 we cannot possibly fit, so no box chars should appear.
    lines = _render(_CJK_TABLE, 10, md_theme)
    for line in lines:
        s = _strip(line)
        assert not any(c in s for c in "┌┐└┘├┤┬┴┼│─"), (
            f"narrow width should fall back to raw markdown, but found "
            f"box-drawing chars in line: {s!r}"
        )


def test_ascii_table_unchanged_after_cjk_fix(md_theme):
    """Pure-ASCII tables must render identically to before the CJK fix —
    the physical-floor clamp is a no-op when all glyphs are width 1."""
    lines = _render(
        "| name | value |\n|---|---|\n| foo | 1 |\n| bar | 22 |",
        40,
        md_theme,
    )
    _assert_aligned(lines)
    # Natural widths: name=4, value=5 → col_widths=[4,5] (natural, since the
    # table fits in 40 cols). Physical floor for ASCII is 1, so max(4,1)=4
    # and max(5,1)=5 — the clamp is a no-op and the layout is unchanged.
    # Border dash run per column is col_width + 2 (matches the
    # ``"<space> <content> <space>"`` interior), so 6 dashes for name and
    # 7 for value.
    expected_top = "┌──────┬───────┐"  # ┌ + ─×6 + ┬ + ─×7 + ┐
    assert _strip(lines[0]).rstrip() == expected_top, (
        f"ASCII table layout changed unexpectedly.\n"
        f"expected first line: {expected_top!r}\n"
        f"got: {_strip(lines[0]).rstrip()!r}"
    )
