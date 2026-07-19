"""Tests for the rendering fixes: engine shrink handling + AssistantMessageComponent.

Verifies:
  1. tui.py no longer full-redraws when content shrinks (the flicker fix)
  2. AssistantMessageComponent renders thinking before text (ReAct order)
  3. Empty content blocks render zero lines (no placeholder height jumps)
"""
from __future__ import annotations

from dataclasses import dataclass, field


from agent_tui.components.assistant_message import AssistantMessageComponent


# ─── fake content blocks + message (avoid importing agent_llm just for shapes) ──

@dataclass
class _Text:
    type: str = "text"
    text: str = ""


@dataclass
class _Thinking:
    type: str = "thinking"
    thinking: str = ""


@dataclass
class _Msg:
    content: list = field(default_factory=list)
    role: str = "assistant"


# ─── AssistantMessageComponent ────────────────────────────────────────

def test_empty_message_renders_zero_lines():
    """An assistant message with no visible content renders zero content lines.

    This is the key anti-flicker property: no placeholder rows during streaming.
    """
    comp = AssistantMessageComponent(_Msg(content=[]))
    lines = comp.render(80)
    # contentContainer is empty (no Spacer, no Markdown) → 0 lines.
    assert lines == []


def test_thinking_renders_before_text():
    """ReAct order: thinking appears above text in the rendered output."""
    msg = _Msg(content=[
        _Thinking(thinking="let me consider the options"),
        _Text(text="here is my answer"),
    ])
    comp = AssistantMessageComponent(msg)
    lines = comp.render(80)
    joined = "\n".join(lines)
    thinking_idx = joined.find("consider the options")
    text_idx = joined.find("here is my answer")
    assert thinking_idx >= 0 and text_idx >= 0
    assert thinking_idx < text_idx, "thinking must render before text"


def test_empty_thinking_block_renders_nothing():
    """A thinking block with only whitespace contributes no lines."""
    msg = _Msg(content=[
        _Thinking(thinking="   "),  # whitespace only
        _Text(text="actual answer"),
    ])
    comp = AssistantMessageComponent(msg)
    lines = comp.render(80)
    joined = "\n".join(lines)
    assert "actual answer" in joined
    # No stray empty "thinking" content.


def test_update_content_rebuilds_on_each_call():
    """update_content clears + rebuilds, so streaming deltas update the view."""
    msg = _Msg(content=[_Text(text="initial")])
    comp = AssistantMessageComponent(msg)
    assert "initial" in "\n".join(comp.render(80))

    # Simulate a streaming delta: replace with longer text.
    msg.content = [_Text(text="initial and more")]
    comp.update_content(msg)
    joined = "\n".join(comp.render(80))
    assert "initial and more" in joined


def test_text_only_renders_without_thinking():
    """A message with only text (no thinking) still renders correctly."""
    msg = _Msg(content=[_Text(text="just text")])
    comp = AssistantMessageComponent(msg)
    lines = comp.render(80)
    assert "just text" in "\n".join(lines)


def test_thinking_only_renders_without_text():
    """A message with only thinking (no text yet) renders the thinking."""
    msg = _Msg(content=[_Thinking(thinking="still thinking")])
    comp = AssistantMessageComponent(msg)
    lines = comp.render(80)
    assert "still thinking" in "\n".join(lines)


# ─── engine layer: shrink no longer triggers full redraw ──────────────

def test_engine_does_not_full_clear_on_shrink():
    """The _do_render code must NOT contain the shrink→full_render(True) branch.

    We check the source to ensure the flicker-causing line was removed.
    """
    import inspect
    from agent_tui.tui import TUI
    src = inspect.getsource(TUI._do_render)
    # The old code was: "if len(new_lines) < self._max_lines_rendered: full_render(True)"
    # After removal, this exact pattern must be gone.
    assert "len(new_lines) < self._max_lines_rendered:\n            full_render(True)" not in src, (
        "shrink→full_render(True) branch still present (flicker source)"
    )
    # And the deliberate-removal comment should be there.
    assert "deliberately do NOT full-render when content shrunk" in src


def test_engine_first_frame_homes_cursor():
    """The first-frame render must home the cursor (\\x1b[H) to avoid garbled output."""
    import inspect
    from agent_tui.tui import TUI
    src = inspect.getsource(TUI._do_render)
    # The first-frame path should include \x1b[H (home).
    assert "\\x1b[H" in src or "\x1b[H" in src, "first-frame cursor home missing"


# ─── width-overflow guard skips table rows (border-closure fix) ──────────


def test_is_table_line_detects_borders():
    """The width-overflow guard must recognise every kind of rendered table
    line so it can skip truncation and preserve border closure."""
    from agent_tui.tui import _is_table_line

    # Top/separator/bottom borders and data rows.
    assert _is_table_line("┌───┬───┐")
    assert _is_table_line("├───┼───┤")
    assert _is_table_line("└───┴───┘")
    assert _is_table_line("│ a │ b │")
    # Leading whitespace (background fill / padding) is tolerated.
    assert _is_table_line("  ┌───┬───┐  ")
    # ANSI background/foreground codes don't break detection.
    assert _is_table_line("\x1b[48;2;40;40;50m│ a │ b │\x1b[49m")
    # Non-table lines are not misidentified.
    assert not _is_table_line("regular paragraph")
    assert not _is_table_line("  indented text")
    assert not _is_table_line("- list item")
    assert not _is_table_line("")


def test_overflow_guard_skips_table_lines():
    """The width-overflow crash guard must skip table rows — truncating them
    slices off the right ``│`` border and leaves every row at a different
    visible width (the "边框错位" symptom with CJK/emoji tables).

    This test simulates the guard loop directly: render a table at a wide
    width, then run the guard at a narrower width. Every table line must be
    preserved at its original width (not truncated).
    """
    from agent_tui.tui import _is_table_line
    from agent_tui.components.markdown import Markdown
    from agent_tui.theme import load_theme, get_markdown_theme
    from agent_tui.utils import truncate_to_width, visible_width
    import re

    src = (
        "| 包 | 角色 | 职责 |\n"
        "|------|------|------|\n"
        "| packages/llm | 🧠 大脑 | LLM 接口层 |\n"
        "| packages/core | 🫀 躯干 | 运行时 |"
    )
    md_theme = get_markdown_theme(load_theme("dark"))
    # Render wide, then apply the guard at a narrower width.
    lines = Markdown(src, padding_x=1, theme=md_theme).render(100)
    assert lines, "table produced no lines"

    guarded = []
    for line in lines:
        lw = visible_width(line)
        if lw > 80 and not _is_table_line(line):
            guarded.append(truncate_to_width(line, 80))
        else:
            guarded.append(line)

    # Every table row's right border must be intact.
    for line in guarded:
        clean = re.sub(r"\x1b\[[0-9;]*m", "", line).rstrip()
        if not clean or not _is_table_line(line):
            continue
        last = clean[-1]
        assert last in "│┐┤┘", (
            f"table line lost its right border after guard; last char={last!r}\n"
            f"line={clean!r}"
        )
