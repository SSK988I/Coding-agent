"""Tests for components/assistant_message.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_llm import AssistantMessage, TextContent, ThinkingContent, ToolCall

from coding_agent.modes.interactive.components.assistant_message import (
    OSC133_ZONE_END,
    OSC133_ZONE_FINAL,
    OSC133_ZONE_START,
    AssistantMessageComponent,
)


def _msg(*content, stop_reason="stop", error_message=None) -> AssistantMessage:
    return AssistantMessage(content=list(content), stop_reason=stop_reason, error_message=error_message)


# ─── empty / no content ────────────────────────────────────────────────


def test_empty_message_renders_nothing():
    comp = AssistantMessageComponent(_msg())
    lines = comp.render(40)
    assert lines == []


def test_no_message_renders_nothing():
    comp = AssistantMessageComponent()
    assert comp.render(40) == []


# ─── visible content gating ────────────────────────────────────────────


def test_whitespace_only_text_renders_nothing():
    comp = AssistantMessageComponent(_msg(TextContent(text="   ")))
    assert comp.render(40) == []


def test_visible_text_gets_leading_spacer():
    comp = AssistantMessageComponent(_msg(TextContent(text="hello")))
    lines = comp.render(40)
    # First line is the spacer (OSC133-wrapped empty), then content.
    assert lines[0].strip() == "" or OSC133_ZONE_START in lines[0]
    assert any("hello" in ln for ln in lines)


# ─── thinking + text ordering ──────────────────────────────────────────


def test_thinking_then_text_renders_both_with_spacer_between():
    comp = AssistantMessageComponent(
        _msg(ThinkingContent(thinking="reasoning"), TextContent(text="answer")),
    )
    lines = comp.render(40)
    joined = "\n".join(lines)
    assert "reasoning" in joined
    assert "answer" in joined
    # Index of reasoning comes before answer.
    assert joined.index("reasoning") < joined.index("answer")


def test_hidden_thinking_shows_label_not_trace():
    comp = AssistantMessageComponent(
        _msg(ThinkingContent(thinking="secret reasoning"), TextContent(text="answer")),
        hide_thinking_block=True,
        hidden_thinking_label="Thinking...",
    )
    joined = "\n".join(comp.render(40))
    assert "Thinking..." in joined
    assert "secret reasoning" not in joined


def test_thinking_alone_no_spacer_after():
    comp = AssistantMessageComponent(_msg(ThinkingContent(thinking="just thinking")))
    lines = comp.render(40)
    # Leading spacer present (OSC133-wrapped empty), no trailing spacer.
    assert lines[0].strip() == "" or OSC133_ZONE_START in lines[0]
    assert "just thinking" in "\n".join(lines)


# ─── stop reasons ──────────────────────────────────────────────────────


def test_length_stop_reason_shows_token_limit_error():
    comp = AssistantMessageComponent(
        _msg(TextContent(text="partial"), stop_reason="length"),
    )
    # 规范化空白字符（文本在 40 列宽度下会换行）。
    flat = "".join(comp.render(40))
    import re
    flat_no_ansi = re.sub(r"\x1b\][0-9;]*[A-Z]\x07", "", flat)
    assert "最大输出 Token 限制" in re.sub(r"\s+", " ", flat_no_ansi)


def test_error_stop_reason_shows_error_message():
    comp = AssistantMessageComponent(
        _msg(TextContent(text="partial"), stop_reason="error", error_message="boom"),
    )
    joined = "\n".join(comp.render(40))
    assert "错误：boom" in joined


def test_aborted_stop_reason_shows_default_message():
    comp = AssistantMessageComponent(
        _msg(TextContent(text="partial"), stop_reason="aborted"),
    )
    joined = "\n".join(comp.render(40))
    assert "操作已中止" in joined


def test_aborted_with_custom_error_message_uses_it():
    comp = AssistantMessageComponent(
        _msg(TextContent(text="partial"), stop_reason="aborted", error_message="killed by user"),
    )
    joined = "\n".join(comp.render(40))
    assert "killed by user" in joined
    assert "操作已中止" not in joined


# ─── tool calls suppress error + OSC133 ────────────────────────────────


def test_tool_call_suppresses_error_display():
    comp = AssistantMessageComponent(
        _msg(
            TextContent(text="running tool"),
            ToolCall(id="t1", name="bash"),
            stop_reason="error",
            error_message="boom",
        ),
    )
    joined = "\n".join(comp.render(40))
    assert "boom" not in joined  # error suppressed when tool calls present


def test_no_osc133_wrap_when_tool_calls_present():
    comp = AssistantMessageComponent(
        _msg(TextContent(text="hi"), ToolCall(id="t1", name="bash")),
    )
    rendered = "\n".join(comp.render(40))
    assert OSC133_ZONE_START not in rendered


def test_osc133_wrap_added_for_pure_text():
    comp = AssistantMessageComponent(_msg(TextContent(text="hi")))
    lines = comp.render(40)
    rendered = "".join(lines)
    assert OSC133_ZONE_START in rendered
    assert OSC133_ZONE_END in rendered
    assert OSC133_ZONE_FINAL in rendered


# ─── update_content rebuild ────────────────────────────────────────────


def test_update_content_rebuilds_on_each_call():
    comp = AssistantMessageComponent(_msg(TextContent(text="first")))
    assert "first" in "\n".join(comp.render(40))
    comp.update_content(_msg(TextContent(text="second")))
    joined = "\n".join(comp.render(40))
    assert "second" in joined
    assert "first" not in joined


def test_set_hide_thinking_block_rebuilds():
    comp = AssistantMessageComponent(
        _msg(ThinkingContent(thinking="trace"), TextContent(text="ans")),
        hide_thinking_block=False,
        hidden_thinking_label="Thinking...",
    )
    assert "trace" in "\n".join(comp.render(40))
    comp.set_hide_thinking_block(True)
    joined = "\n".join(comp.render(40))
    assert "Thinking..." in joined
    assert "trace" not in joined
