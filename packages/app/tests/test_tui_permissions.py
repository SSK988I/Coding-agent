"""Real TUI menus and Agent hooks, with isolated settings and no provider calls."""
from __future__ import annotations

import asyncio
import re

import pytest
from agent_core import AgentContext, BeforeToolCallContext, SessionManager
from agent_llm import AssistantMessage, Model, ModelCost, ToolCall
from agent_tui.utils import visible_width

from coding_agent.cli.args import parse_args
from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.settings import SettingsManager
from coding_agent.modes.interactive.components.permission_menu import PermissionMenuComponent
from coding_agent.modes.interactive.interactive_mode import InteractiveMode


def make_mode(tmp_path, mode=None, *, web_search_enabled=False):
    settings = SettingsManager(tmp_path / "settings.json")
    settings.load()
    session = AgentSession(AgentSessionConfig(
        cwd=str(tmp_path), session_manager=SessionManager.create(in_memory=True),
        model=Model(id="test", cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0)),
        settings_manager=settings, permission_mode=mode,
        web_search_enabled=web_search_enabled,
    ))
    return InteractiveMode(session)


def tool_context(name, args):
    call = ToolCall(id="call-1", name=name, arguments=args)
    return BeforeToolCallContext(
        assistant_message=AssistantMessage(content=[call]), tool_call=call,
        args=args, context=AgentContext(),
    )


@pytest.mark.parametrize("args,expected", [
    (["--dangerously-skip-permissions"], "full_access"),
    (["--permission-mode", "bypassPermissions"], "full_access"),
    (["--permission-mode", "default"], "ask"),
    (["--permission-mode", "auto"], "auto"),
])
def test_cli_permission_aliases(args, expected):
    assert parse_args(args).permission_mode == expected


def test_cli_rejects_conflicting_or_invalid_permissions():
    for args in (["--permission-mode", "bad"], ["--permission-mode", "ask", "--dangerously-skip-permissions"]):
        with pytest.raises(SystemExit):
            parse_args(args)


def test_permission_command_persists_toggle_and_restores_input(tmp_path):
    async def scenario():
        mode = make_mode(tmp_path)
        try:
            mode.editor.set_text("keep this draft")
            assert await mode._handle_command("/permissions on")
            assert mode._session.permission_mode == "full_access"
            assert mode._session.settings_manager.settings.tui_permission_mode == "full_access"
            mode._cmd_permissions()
            assert isinstance(mode._current_selector, PermissionMenuComponent)
            assert "bypass permissions on" in "\n".join(mode._current_selector.render(80))
            mode._current_selector.handle_input("\x1b")
            assert mode._current_selector is None
            assert mode.editor.get_text() == "keep this draft"
            assert await mode._handle_command("/permissions off")
            assert mode._session.permission_mode == "ask"
            assert SettingsManager(mode._session.settings_manager.path).load().tui_permission_mode == "ask"
        finally:
            await mode._session.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("decision", ["allow", "deny", "abort"])
def test_normal_mode_waits_for_external_edit_approval(tmp_path, decision):
    async def scenario():
        mode = make_mode(tmp_path)
        signal = asyncio.Event()
        try:
            waiting = asyncio.create_task(mode._session._before_tool_call(
                tool_context("write", {"path": "../outside.txt", "content": "data"}), signal,
            ))
            await asyncio.sleep(0)
            selector = mode._current_selector
            assert isinstance(selector, PermissionMenuComponent)
            assert not waiting.done()
            if decision == "abort":
                signal.set()
            elif decision == "allow":
                selector.handle_input("\x1b[B")
                selector.handle_input("\r")
            else:
                selector.handle_input("\x1b")
            result = await asyncio.wait_for(waiting, 1)
            assert (result is None) is (decision == "allow")
            if result is not None:
                assert result.block
            assert mode._current_selector is None
            assert mode.editor in mode.tui.children
        finally:
            await mode._session.aclose()
    asyncio.run(scenario())


def test_bypass_skips_approval_but_still_enforces_plan(tmp_path):
    async def scenario():
        mode = make_mode(tmp_path, "full_access")
        try:
            request = tool_context("bash", {"command": "rm -rf build"})
            assert await mode._session._before_tool_call(request, asyncio.Event()) is None
            assert mode._current_selector is None
            mode._session.enter_plan_mode()
            result = await mode._session._before_tool_call(request, asyncio.Event())
            assert result.block and result.code == "PLAN_POLICY_BLOCKED"
            assert mode._current_selector is None
        finally:
            await mode._session.aclose()
    asyncio.run(scenario())


def test_permissions_cannot_change_during_a_run(tmp_path):
    mode = make_mode(tmp_path)
    mode._session._is_processing = True
    mode._cmd_permissions("on")
    assert mode._session.permission_mode == "ask"
    mode._session._is_processing = False
    asyncio.run(mode._session.aclose())


def test_alt_p_toggles_bypass_through_the_real_input_dispatcher(tmp_path):
    mode = make_mode(tmp_path)
    mode._register_input_listeners()
    mode.tui._handle_input("\x1bp")
    assert mode._session.permission_mode == "full_access"
    mode.tui._handle_input("\x1bp")
    assert mode._session.permission_mode == "ask"
    asyncio.run(mode._session.aclose())


def test_parallel_approvals_serialize_and_abort_without_stale_dialogs(tmp_path):
    async def scenario():
        mode = make_mode(tmp_path)
        signal = asyncio.Event()
        try:
            first = asyncio.create_task(mode._ask_permission("first", "first request", signal))
            second = asyncio.create_task(mode._ask_permission("second", "second request", signal))
            await asyncio.sleep(0)
            assert "first request" in "\n".join(mode._current_selector.render(80))
            assert not second.done()
            mode._current_selector.handle_input("\x1b")
            assert not await first
            await asyncio.sleep(0)
            assert "second request" in "\n".join(mode._current_selector.render(80))
            signal.set()
            assert not await asyncio.wait_for(second, 1)
            assert mode._current_selector is None
        finally:
            await mode._session.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("width", [120, 40, 20])
def test_footer_keeps_permission_and_plan_status_visible(tmp_path, width):
    mode = make_mode(tmp_path, "full_access")
    mode._session.enter_plan_mode()
    rows = mode._footer.render(width)
    text = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(rows))
    assert "plan" in text
    assert "bypass permissions on" in text or "bypass on" in text
    assert all(visible_width(row) <= width for row in rows)
    asyncio.run(mode._session.aclose())


def test_headless_explicit_ask_rejects_unapproved_operations(tmp_path):
    session = AgentSession(AgentSessionConfig(
        cwd=str(tmp_path), model=Model(id="test", cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0)),
        permission_mode="ask",
    ))
    result = asyncio.run(session._before_tool_call(tool_context("bash", {"command": "rm file"}), asyncio.Event()))
    assert result.block and result.code == "PERMISSION_APPROVAL_REQUIRED"
    asyncio.run(session.aclose())


@pytest.mark.parametrize("permission_mode", ["ask", "full_access"])
def test_real_write_executes_only_after_required_approval(tmp_path, permission_mode):
    async def scenario():
        root = tmp_path / "workspace"
        root.mkdir()
        mode = make_mode(root, permission_mode)
        output = tmp_path / "external.txt"
        opened = asyncio.Event()
        original_swap = mode._swap_editor_for
        def swap(selector):
            original_swap(selector)
            opened.set()
        mode._swap_editor_for = swap
        turns = 0
        class Stream:
            def __init__(self, message):
                self.message = message
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise StopAsyncIteration
            async def result(self):
                return self.message
        def stream(*_):
            nonlocal turns
            turns += 1
            return Stream(AssistantMessage(content=[ToolCall(
                id="write-1", name="write", arguments={"path": str(output), "content": "approved content"},
            )], stop_reason="tool_use") if turns == 1 else AssistantMessage())
        mode._session.agent.stream_fn = stream
        task = asyncio.create_task(mode._session.prompt("write the file"))
        try:
            if permission_mode == "ask":
                await asyncio.wait_for(opened.wait(), 1)
                assert not output.exists()
                mode._current_selector.handle_input("\x1b[B")
                mode._current_selector.handle_input("\r")
            await asyncio.wait_for(task, 2)
            assert output.read_text() == "approved content"
            assert opened.is_set() is (permission_mode == "ask")
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await mode._session.aclose()
    asyncio.run(scenario())


def test_native_search_decision_is_cached_only_within_one_task(tmp_path):
    async def scenario():
        mode = make_mode(tmp_path, web_search_enabled=True)
        signal = asyncio.Event()
        try:
            waiting = asyncio.create_task(mode._before_native_web_search(signal))
            await asyncio.sleep(0)
            mode._current_selector.handle_input("\x1b")
            assert not await waiting
            assert not await mode._before_native_web_search(signal)
            assert mode._current_selector is None
            # A fresh run has a fresh abort signal and must ask again.
            waiting = asyncio.create_task(mode._before_native_web_search(asyncio.Event()))
            await asyncio.sleep(0)
            assert isinstance(mode._current_selector, PermissionMenuComponent)
            mode._current_selector.handle_input("\x1b[B")
            mode._current_selector.handle_input("\r")
            assert await waiting
        finally:
            await mode._session.aclose()
    asyncio.run(scenario())
