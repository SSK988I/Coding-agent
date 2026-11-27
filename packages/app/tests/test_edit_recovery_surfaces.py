"""Drive the two user entry points against a real interrupted edit."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from agent_core import SessionManager
from agent_llm import AssistantMessage, TextContent

from coding_agent.desktop.runtime import DesktopRuntime
from coding_agent.modes.interactive.components.tool_execution import ToolExecutionComponent
from coding_agent.modes.interactive.interactive_mode import InteractiveMode
from test_desktop_transport import Wire
from test_edit_crash_recovery import CALL_ID, EDITED, ORIGINAL, edit_request, open_runtime, reply, run_until_crash
from test_interactive_events import _make_mode


@pytest.fixture
def interrupted(tmp_path, monkeypatch):
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "agent-home"))
    workspace = tmp_path / "project"
    workspace.mkdir()
    target = workspace / "example.py"
    target.write_bytes(ORIGINAL)
    path = run_until_crash(workspace, "before-replace")
    manager = SessionManager.open(path, sessions_dir=tmp_path / "sessions")
    return target, manager, manager.get_branch()[-1].id


def test_stdio_checks_then_retries_only_the_selected_edit(interrupted, monkeypatch):
    target, manager, entry_id = interrupted

    async def scenario():
        wire = Wire()
        runtime = DesktopRuntime(lambda _: None)
        runtime._workspace = target.parent
        runtime._session = open_runtime(target.parent, manager)
        serving = wire.start(monkeypatch, runtime)
        scope = {"sessionId": manager.header.id, "assistantEntryId": entry_id, "toolCallId": CALL_ID}
        try:
            wire.send("stale", "edit.retry", **{**scope, "sessionId": "other"})
            assert (await wire.response("stale"))["error"]["code"] == "STALE_SESSION"
            wire.send("check", "edit.check", **scope)
            checked = (await wire.response("check"))["result"]
            assert checked["turnChanges"] == []
            result, = [msg for msg in checked["messages"] if msg["role"] == "toolResult"]
            assert result["details"]["assistantEntryId"] == entry_id and result["details"]["retryable"] is True
            assert target.read_bytes() == ORIGINAL
            wire.send("retry", "edit.retry", **scope)
            reply = (await wire.response("retry"))["result"]
            assert reply["result"]["status"] == "completed"
            card, = [msg for msg in reply["snapshot"]["messages"] if msg["role"] == "toolResult"]
            assert card["details"]["patch"] == reply["result"]["details"]["patch"]
            assert target.read_bytes() == EDITED
        finally:
            await wire.close(serving)

    asyncio.run(scenario())


def test_tui_card_exposes_check_then_retry_and_replaces_its_result(interrupted):
    target, manager, entry_id = interrupted

    async def scenario():
        session = open_runtime(target.parent, manager)
        mode = _make_mode()
        mode._session = session
        mode._add_user_message = lambda _text: None
        mode._refresh_footer = mode._update_editor_border_color = mode._restore_editor = mode._render_plan_state_controls = Mock()

        def card_text():
            card, = [child for child in mode.chat_container.children if isinstance(child, ToolExecutionComponent)]
            return "\n".join(card.render(240))

        try:
            mode._rebuild_chat_from_messages()
            check = f"/check-edits {entry_id} {CALL_ID}"
            retry = f"/retry-edit {entry_id} {CALL_ID}"
            assert check in card_text() and retry not in card_text()
            parent_entry = manager.get_branch()[-1].parent_id
            InteractiveMode._on_tree_selected(mode, parent_entry)
            assert not any(isinstance(child, ToolExecutionComponent) for child in mode.chat_container.children)
            InteractiveMode._on_tree_selected(mode, entry_id)
            assert check in card_text() and target.read_bytes() == ORIGINAL
            assert await InteractiveMode._handle_command(mode, check)
            assert retry in card_text() and target.read_bytes() == ORIGINAL
            assert await InteractiveMode._handle_command(mode, retry)
            assert retry not in card_text() and target.read_bytes() == EDITED
        finally:
            await session.aclose()

    asyncio.run(scenario())


def test_stdio_resolves_sensitive_edit_check_approval_while_rpc_is_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "agent-home"))
    target = tmp_path / ".env.test"
    target.write_bytes(ORIGINAL)

    async def scenario():
        manager = SessionManager.create(cwd=str(tmp_path), sessions_dir=tmp_path / "sessions")
        session = open_runtime(tmp_path, manager)
        call = edit_request()
        call.content[0].arguments["path"] = target.name
        answers = iter([call, AssistantMessage(content=[TextContent(text="Done")])])
        session.agent.stream_fn = lambda *_: reply(next(answers))
        replace = os.replace

        def interrupt_replace(source, destination):
            if Path(destination) == target:
                raise OSError("injected commit failure")
            replace(source, destination)

        with patch("os.replace", interrupt_replace):
            await session.prompt("Edit the test fixture.")
        source = next(entry for entry in manager.get_branch()
                      if getattr(entry, "message", None) == call)
        approvals = asyncio.Queue()
        runtime = DesktopRuntime(lambda event: approvals.put_nowait(event["event"]["payload"])
                                 if event["event"]["type"] == "approval.requested" else None)
        runtime._session, runtime._workspace = session, tmp_path
        runtime._permission_mode = "ask"
        session.set_permission_mode("ask", persist=False)
        session._config.before_tool_call = runtime._before_tool_call
        wire = Wire()
        serving = wire.start(monkeypatch, runtime)
        try:
            scope = {"sessionId": manager.header.id, "assistantEntryId": source.id, "toolCallId": CALL_ID}
            wire.send("check", "edit.check", **scope)
            approval = await asyncio.wait_for(approvals.get(), 2)
            assert approval["assistantEntryId"] == source.id and approval["toolCallId"] == CALL_ID
            assert "check" not in wire.responses
            wire.send("allow", "approval.resolve", approvalId=approval["approvalId"], approved=True)
            assert (await wire.response("allow"))["result"]["resolved"]
            checked = (await wire.response("check"))["result"]
            result, = [message for message in checked["messages"] if message["role"] == "toolResult"]
            assert result["details"]["retryable"]
            assert target.read_bytes() == ORIGINAL
        finally:
            await wire.close(serving)

    asyncio.run(scenario())
