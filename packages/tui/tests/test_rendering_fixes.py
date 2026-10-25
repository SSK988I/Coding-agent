"""Tests for the rendering fixes: engine shrink handling + AssistantMessageComponent.

Verifies:
  1. tui.py no longer full-redraws when content shrinks (the flicker fix)
  2. AssistantMessageComponent renders thinking before text (ReAct order)
  3. Empty content blocks render zero lines (no placeholder height jumps)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

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


# ─── width-overflow guard preserves table closure without wrapping ──────


def test_is_table_line_detects_borders():
    """The width-overflow guard must recognise every kind of rendered table
    line so it can preserve the correct closing border while clipping."""
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


def test_overflow_guard_clips_tables_and_preserves_closing_border():
    """Overwide tables must keep their border without triggering a real wrap."""
    from agent_tui.tui import _fit_line_to_width, _is_table_line
    from agent_tui.components.markdown import Markdown
    from agent_tui.theme import load_theme, get_markdown_theme
    from agent_tui.utils import visible_width
    import re

    src = (
        "| 包 | 角色 | 职责 |\n"
        "|------|------|------|\n"
        "| packages/llm | 🧠 大脑 | LLM 接口层 |\n"
        "| packages/core | 🫀 躯干 | 运行时 |"
    )
    md_theme = get_markdown_theme(load_theme("dark"))
    # Render wide, then apply the renderer's guard at a narrower width.
    lines = Markdown(src, padding_x=1, theme=md_theme).render(100)
    assert lines, "table produced no lines"
    guarded = [_fit_line_to_width(line, 80) for line in lines]

    # Every line fits the terminal, while table rows keep a right border in
    # the same final column even when clipping stops before a double-width
    # grapheme.
    assert all(visible_width(line) <= 80 for line in guarded)
    for line in guarded:
        clean = re.sub(r"\x1b\[[0-9;]*m", "", line).rstrip()
        if not clean or not _is_table_line(line):
            continue
        assert visible_width(line) == 80
        last = clean[-1]
        assert last in "│┐┤┘", (
            f"table line lost its right border after guard; last char={last!r}\n"
            f"line={clean!r}"
        )


def test_overflow_guard_pads_before_border_after_wide_grapheme():
    """A clipped CJK grapheme must not leave the closing border one cell short."""
    from agent_tui.tui import _fit_line_to_width
    from agent_tui.utils import visible_width

    guarded = _fit_line_to_width("│12345汉字│", 8)

    assert visible_width(guarded) == 8
    assert guarded.endswith("│")


def test_printable_ascii_fast_path_preserves_all_ascii_boundaries():
    from agent_tui.utils import _is_printable_ascii

    assert _is_printable_ascii("") is True
    for codepoint in range(128):
        char = chr(codepoint)
        expected = 0x20 <= codepoint <= 0x7E
        assert _is_printable_ascii(char) is expected
        assert _is_printable_ascii("prefix" + char + "suffix") is expected


@pytest.mark.parametrize("text", ["中文", "é", "e\u0301", "👩‍💻", "😀", "\x1b[31mred\x1b[0m", "\x1b]133;A\x07"])
def test_printable_ascii_fast_path_excludes_unicode_and_terminal_sequences(text: str):
    from agent_tui.utils import _is_printable_ascii

    assert _is_printable_ascii(text) is False


@pytest.mark.parametrize(("text", "expected"), [
    ("", 0), (" ASCII~", 7), ("\t", 3), ("\x00\x1f\x7f", 0), ("\n\r", 0),
    ("中文", 4), ("e\u0301", 1), ("👩‍💻", 2),
    ("\x1b[31m中文\x1b[0m", 4),
    ("\x1b]8;;https://example.com/\x07link\x1b]8;;\x07", 4),
    ("\x1b]133;A\x07hi\x1b]133;B\x1b\\", 2),
    ("\x1b_Gi=1;payload\x1b\\X", 1),
])
def test_visible_width_preserves_unicode_controls_and_terminal_sequences(text: str, expected: int):
    from agent_tui.utils import visible_width

    assert visible_width(text) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("", ""), ("abc", "a"), ("中文", "中"), ("e\u0301suffix", "e\u0301"),
    ("👩‍💻suffix", "👩‍💻"), ("👍🏽!", "👍🏽"), ("🇨🇳🇯🇵", "🇨🇳"),
    ("\r\nx", "\r\n"), ("\u0301x", "\u0301"), ("\tspace", "\t"), ("\ud800rest", "\ud800"),
])
def test_first_grapheme_matches_existing_unicode_segmentation(text: str, expected: str):
    from agent_tui.utils import first_grapheme, segment_graphemes

    assert first_grapheme(text) == expected
    assert first_grapheme(text) == next(iter(segment_graphemes(text)), "")


@pytest.mark.parametrize("cluster", ["a", "中", "e\u0301", "👩‍💻", "👍🏽", "🇨🇳"])
@pytest.mark.parametrize("delete_key", ["\x1b[3~", "\x04"])
def test_editor_forward_delete_and_cursor_keep_complete_graphemes_and_undo(cluster: str, delete_key: str):
    from agent_tui.components.editor import Editor
    from agent_tui.tui import CURSOR_MARKER

    editor = Editor()
    editor.focused = True
    prefix, suffix = "p|", " remaining 中文 e\u0301 👍🏽 "
    original = prefix + cluster + suffix
    editor.set_text_and_cursor(original, 0, len(prefix))
    changes = []
    editor.on_change = changes.append

    rendered = editor.render(80)
    assert CURSOR_MARKER + f"\x1b[7m{cluster}\x1b[0m" + suffix in "\n".join(rendered)
    assert editor.get_text() == original

    assert editor.handle_input(delete_key)
    assert editor.get_text() == prefix + suffix
    assert editor.get_cursor() == (0, len(prefix))
    assert changes[-1] == prefix + suffix

    assert editor.handle_input("\x1a")  # Ctrl+Z restores the entire cluster and suffix.
    assert editor.get_text() == original
    assert editor.get_cursor() == (0, len(prefix))


def test_editor_forward_delete_and_cursor_do_not_segment_the_entire_suffix(monkeypatch):
    import agent_tui.components.editor as editor_module

    def unexpected_full_segmentation(_text):
        raise AssertionError("Cursor highlighting and forward delete only need the first grapheme")

    monkeypatch.setattr(editor_module, "segment_graphemes", unexpected_full_segmentation)
    editor = editor_module.Editor()
    suffix = " ordinary text 中文 " * 100
    editor.set_text_and_cursor("👩‍💻" + suffix, 0, 0)
    editor.focused = True

    assert "\x1b[7m👩‍💻\x1b[0m" in "\n".join(editor.render(4000))
    editor.handle_input("\x1b[3~")
    assert editor.get_text() == suffix


def test_editor_forward_delete_keeps_empty_and_line_join_behavior():
    from agent_tui.components.editor import Editor
    from agent_tui.tui import CURSOR_MARKER

    editor = Editor()
    editor.focused = True
    editor.handle_input("\x1b[3~")
    assert editor.get_text() == ""
    assert CURSOR_MARKER + "\x1b[7m \x1b[0m" in "\n".join(editor.render(80))

    editor.set_text_and_cursor("first\nsecond", 0, len("first"))
    editor.handle_input("\x1b[3~")
    assert editor.get_text() == "firstsecond"
    assert editor.get_cursor() == (0, len("first"))
    editor.handle_input("\x1a")
    assert editor.get_text() == "first\nsecond"
    assert editor.get_cursor() == (0, len("first"))
