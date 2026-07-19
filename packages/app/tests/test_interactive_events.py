"""Integration tests for interactive_mode event dispatch.

These exercise the agent-event -> UI mapping without a real TUI by stubbing
the minimal surface InteractiveMode touches (chat_container, status_container,
tui.request_render). Verifies the single-AssistantMessageComponent rebuild
model and tool-card lifecycle.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_llm import AssistantMessage, TextContent, ToolCall

from coding_agent.modes.interactive.components.assistant_message import (
    AssistantMessageComponent,
)
from coding_agent.modes.interactive.components.tool_execution import (
    ToolExecutionComponent,
)


class FakeTUI:
    """Minimal TUI stub: records request_render calls."""

    def __init__(self):
        self._loop = None
        self.renders = 0
        # Mirrors TUI._last_render_at initial state so _render_streaming_delta
        # can read it. 0.0 triggers the immediate-render_now() branch on first
        # streaming delta, matching real TUI behavior.
        self._last_render_at = 0.0

    def request_render(self):
        self.renders += 1

    def render_now(self):
        # Mirror real TUI: paint immediately and advance the baseline so the
        # next _render_streaming_delta throttle check sees fresh elapsed time.
        import time
        self.renders += 1
        self._last_render_at = time.monotonic()

    def add_input_listener(self, fn):
        pass


def _make_mode():
    """Build an InteractiveMode-like object with just the event-dispatch state.

    We avoid constructing the full TUI/Editor by instantiating the class's
    methods on a SimpleNamespace that mirrors the relevant attributes.
    """
    from agent_tui import Container, load_theme, get_markdown_theme
    from coding_agent.modes.interactive.interactive_mode import InteractiveMode

    theme = load_theme("dark")
    md_theme = get_markdown_theme(theme)

    obj = SimpleNamespace()
    obj.theme = theme
    obj.md_theme = md_theme
    obj.tui = FakeTUI()
    obj.chat_container = Container()
    obj.status_container = Container()
    obj._session = SimpleNamespace(session_manager=SimpleNamespace(get_branch=lambda: []))
    obj._streaming_component = None
    obj._pending_tools = {}
    obj._tool_cards = obj._pending_tools
    obj._active_status_indicator = None
    obj._is_responding = False

    # Bind the real methods.
    obj._on_agent_event = InteractiveMode._on_agent_event.__get__(obj, SimpleNamespace)
    obj._handle_message_start = InteractiveMode._handle_message_start.__get__(obj, SimpleNamespace)
    obj._handle_message_update = InteractiveMode._handle_message_update.__get__(obj, SimpleNamespace)
    obj._render_streaming_delta = InteractiveMode._render_streaming_delta.__get__(obj, SimpleNamespace)
    # Class-level constant read by _render_streaming_delta (SimpleNamespace
    # doesn't inherit class attrs, so mirror it explicitly).
    obj._STREAM_RENDER_MIN_INTERVAL_S = InteractiveMode._STREAM_RENDER_MIN_INTERVAL_S
    obj._handle_message_end = InteractiveMode._handle_message_end.__get__(obj, SimpleNamespace)
    obj._handle_tool_start = InteractiveMode._handle_tool_start.__get__(obj, SimpleNamespace)
    obj._handle_tool_end = InteractiveMode._handle_tool_end.__get__(obj, SimpleNamespace)
    obj._handle_compaction_start = InteractiveMode._handle_compaction_start.__get__(obj, SimpleNamespace)
    obj._handle_compaction_end = InteractiveMode._handle_compaction_end.__get__(obj, SimpleNamespace)
    obj._show_status_indicator = InteractiveMode._show_status_indicator.__get__(obj, SimpleNamespace)
    obj._clear_status_indicator = InteractiveMode._clear_status_indicator.__get__(obj, SimpleNamespace)
    obj._rebuild_chat_from_messages = InteractiveMode._rebuild_chat_from_messages.__get__(obj, SimpleNamespace)

    def _add_to_chat(component):
        obj.chat_container.add_child(component)
        obj.tui.request_render()

    def _add_system_message(text):
        pass

    obj._add_to_chat = _add_to_chat
    obj._add_system_message = _add_system_message
    return obj


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ─── message lifecycle ─────────────────────────────────────────────────


def test_assistant_message_start_creates_streaming_component():
    mode = _make_mode()
    msg = AssistantMessage(content=[TextContent(text="hi")])
    mode._on_agent_event({"type": "message_start", "message": msg})
    assert isinstance(mode._streaming_component, AssistantMessageComponent)
    assert mode._streaming_component in mode.chat_container.children


def test_message_update_rebuilds_component_content():
    mode = _make_mode()
    msg1 = AssistantMessage(content=[TextContent(text="hel")])
    mode._on_agent_event({"type": "message_start", "message": msg1})
    msg2 = AssistantMessage(content=[TextContent(text="hello")])
    mode._on_agent_event({"type": "message_update", "message": msg2, "event": {"type": "text_delta"}})
    # Same component, content rebuilt.
    rendered = "".join(mode._streaming_component.render(40))
    assert "hello" in rendered


def test_message_end_finalizes_and_clears_streaming_ref():
    mode = _make_mode()
    msg = AssistantMessage(content=[TextContent(text="done")])
    mode._on_agent_event({"type": "message_start", "message": msg})
    final = AssistantMessage(content=[TextContent(text="done")])
    mode._on_agent_event({"type": "message_end", "message": final})
    assert mode._streaming_component is None
    # The finalized component stays in chat.
    assert any(isinstance(c, AssistantMessageComponent) for c in mode.chat_container.children)


def test_user_message_start_does_not_create_streaming_component():
    mode = _make_mode()
    from agent_llm import UserMessage
    mode._on_agent_event({"type": "message_start", "message": UserMessage(content="hi")})
    assert mode._streaming_component is None


# ─── tool lifecycle ────────────────────────────────────────────────────


def test_tool_call_block_in_message_creates_tool_card():
    mode = _make_mode()
    msg = AssistantMessage(content=[
        TextContent(text="running"),
        ToolCall(id="t1", name="bash", arguments={"command": "ls"}),
    ])
    mode._on_agent_event({"type": "message_start", "message": msg})
    mode._on_agent_event({"type": "message_update", "message": msg, "event": {"type": "toolcall_delta"}})
    assert "t1" in mode._pending_tools
    assert isinstance(mode._pending_tools["t1"], ToolExecutionComponent)


def test_tool_execution_start_then_end_updates_card():
    mode = _make_mode()
    mode._on_agent_event({
        "type": "tool_execution_start",
        "tool_call_id": "t1", "tool_name": "bash", "args": {"command": "ls"},
    })
    assert "t1" in mode._pending_tools
    from agent_core.types import AgentToolResult
    mode._on_agent_event({
        "type": "tool_execution_end",
        "tool_call_id": "t1", "tool_name": "bash",
        "result": AgentToolResult(content=[]), "is_error": False,
    })
    # Card popped after result.
    assert "t1" not in mode._pending_tools


def test_error_message_end_pushes_error_to_pending_tools():
    mode = _make_mode()
    # Simulate a tool call block appeared, then an errored message end.
    msg_with_tool = AssistantMessage(content=[
        TextContent(text=""),
        ToolCall(id="t1", name="bash"),
    ])
    mode._on_agent_event({"type": "message_start", "message": msg_with_tool})
    mode._on_agent_event({"type": "message_update", "message": msg_with_tool, "event": {"type": "x"}})
    assert "t1" in mode._pending_tools

    err_msg = AssistantMessage(
        content=[TextContent(text="")], stop_reason="error", error_message="boom",
    )
    mode._on_agent_event({"type": "message_end", "message": err_msg})
    # Pending tools flushed on error.
    assert "t1" not in mode._pending_tools


# ─── status indicators ─────────────────────────────────────────────────


def test_agent_start_shows_working_indicator():
    from coding_agent.modes.interactive.components.status_indicator import (
        WorkingStatusIndicator,
    )
    mode = _make_mode()
    mode._on_agent_event({"type": "agent_start"})
    assert isinstance(mode._active_status_indicator, WorkingStatusIndicator)
    # Stop it to avoid leaving a dangling asyncio task.
    mode._active_status_indicator.dispose()


def test_agent_end_clears_working_indicator():
    mode = _make_mode()
    mode._on_agent_event({"type": "agent_start"})
    mode._on_agent_event({"type": "agent_end"})
    assert mode._active_status_indicator is None


def test_compaction_start_then_end():
    from coding_agent.modes.interactive.components.status_indicator import (
        CompactionStatusIndicator,
    )
    mode = _make_mode()
    mode._on_agent_event({"type": "compaction_start", "reason": "manual"})
    assert isinstance(mode._active_status_indicator, CompactionStatusIndicator)
    mode._on_agent_event({"type": "compaction_end", "reason": "manual", "summary_preview": ""})
    assert mode._active_status_indicator is None
