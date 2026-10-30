from __future__ import annotations

import asyncio

import pytest
from agent_core import AgentContext, BeforeToolCallContext
from agent_llm import AssistantMessage, Model, ModelCost, ToolCall, TextContent
from agent_llm.providers.deepseek_models import DEEPSEEK_MODELS

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.settings import Settings, SettingsManager
from coding_agent.desktop.permissions import requires_approval
from coding_agent.desktop.protocol import RpcError
from coding_agent.desktop.runtime import DesktopRuntime, _is_default_approval_safe_shell_command


def context(name, args):
    call = ToolCall(id="call-1", name=name, arguments=args)
    return BeforeToolCallContext(
        assistant_message=AssistantMessage(content=[call]), tool_call=call,
        args=args, context=AgentContext(),
    )


@pytest.mark.parametrize("mode,name,args,expected", [
    ("ask", "write", {"path": "src/new.py"}, False),
    ("ask", "edit", {"path": "../outside.txt"}, True),
    ("ask", "edit", {"path": ".env"}, True),
    ("auto", "write", {"path": "src/new.py"}, False),
    ("auto", "write", {"path": "../outside.txt"}, True),
    ("ask", "web_search", {"query": "python"}, True),
    ("auto", "web_search", {"query": "python"}, False),
    ("ask", "bash", {"command": "pnpm test"}, True),
    ("auto", "bash", {"command": "pnpm test"}, False),
    ("auto", "bash", {"command": "ruff check --fix ."}, True),
    ("auto", "bash", {"command": "git remote remove origin"}, True),
    ("auto", "bash", {"command": "git tag -d v1"}, True),
    ("auto", "bash", {"command": "ls & rm file"}, True),
    ("auto", "bash", {"command": "rm -rf build"}, True),
    ("ask", "read", {"path": "src/new.py"}, False),
    ("ask", "git_status", {}, False),
    ("ask", "request_user_input", {}, False),
    ("auto", "unknown_custom_tool", {}, True),
    ("full_access", "write", {"path": "../outside.txt"}, False),
    ("full_access", "bash", {"command": "rm -rf build"}, False),
    ("full_access", "web_search", {}, False),
])
def test_permission_decisions(mode, name, args, expected, tmp_path):
    assert requires_approval(mode, name, args, tmp_path, _is_default_approval_safe_shell_command) is expected


def test_symlinked_external_write_requires_approval(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    try:
        (root / "linked").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable")
    assert requires_approval("ask", "write", {"path": "linked/file.txt"}, root, _is_default_approval_safe_shell_command)


def test_mode_is_persisted_restored_and_included_in_snapshots(tmp_path):
    async def scenario():
        runtime = DesktopRuntime(lambda _: None)
        try:
            opened = await runtime.dispatch("workspace.open", {"path": str(tmp_path), "resume": False})
            assert opened["permissionMode"] == "ask"
            changed = await runtime.dispatch("permission.setMode", {"mode": "full_access"})
            assert changed["permissionMode"] == "full_access"
            assert (await runtime.dispatch("session.snapshot", {}))["permissionMode"] == "full_access"
            created = await runtime.dispatch("session.new", {})
            assert created["permissionMode"] == "full_access"
            assert SettingsManager().load().desktop_permission_mode == "full_access"
            # Other settings must not write a stale cached permission value.
            await runtime.dispatch("memory.setEnabled", {"enabled": False})
            assert SettingsManager().load().desktop_permission_mode == "full_access"
            restored = DesktopRuntime(lambda _: None)
            assert restored._permission_mode == "full_access"
            await restored.dispose()
            with pytest.raises(RpcError) as error:
                await runtime.dispatch("permission.setMode", {"mode": "invalid"})
            assert error.value.code == "INVALID_PARAMS"
            assert runtime._permission_mode == "full_access"
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_failed_save_does_not_change_live_permission(monkeypatch, tmp_path):
    async def scenario():
        runtime = DesktopRuntime(lambda _: None)
        try:
            await runtime.dispatch("workspace.open", {"path": str(tmp_path)})
            def fail(*_):
                raise OSError("disk full")
            monkeypatch.setattr(SettingsManager, "save", fail)
            with pytest.raises(OSError, match="disk full"):
                await runtime.dispatch("permission.setMode", {"mode": "full_access"})
            assert runtime._permission_mode == "ask"
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_full_access_still_respects_plan_tool_gate(tmp_path):
    async def scenario():
        events = []
        runtime = DesktopRuntime(events.append)
        runtime._permission_mode = "full_access"
        session = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=Model(id="test", cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0)),
            before_tool_call=runtime._before_tool_call,
        ))
        try:
            session.enter_plan_mode()
            result = await session.agent.before_tool_call(context("write", {"path": "a.txt"}), asyncio.Event())
            assert result.block and result.code == "PLAN_POLICY_BLOCKED"
            assert events == []
        finally:
            await session.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("approved", [True, False])
def test_native_search_waits_for_permission_and_rejects_caller_bypass(monkeypatch, tmp_path, approved):
    from agent_llm import AssistantMessageEventStream

    async def scenario():
        events, captured = [], []
        waiting = asyncio.Event()
        def emit(event):
            events.append(event)
            if event["event"]["type"] == "permission.networkRequested":
                waiting.set()
        runtime = DesktopRuntime(emit)
        runtime._workspace = tmp_path
        model = DEEPSEEK_MODELS["deepseek-v4-pro"]
        def fake_stream(_model, ctx, options):
            captured.append((ctx, options))
            stream = AssistantMessageEventStream()
            stream.push({"type": "done", "reason": "stop", "message": AssistantMessage(content=[TextContent(text="done")])})
            return stream
        monkeypatch.setattr("agent_llm.compat.stream_simple", fake_stream)
        session = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=model, web_search_enabled=True,
            before_tool_call=runtime._before_tool_call,
            before_native_web_search=runtime._before_native_web_search,
        ))
        runtime._session = session
        try:
            await runtime.dispatch("run.start", {"text": "research"})
            running = runtime._run_task
            await asyncio.wait_for(waiting.wait(), 2)
            assert captured == []
            with pytest.raises(RpcError) as error:
                await runtime.dispatch("permission.setMode", {"mode": "full_access"})
            assert error.value.code == "RUN_IN_PROGRESS"
            request = next(e["event"]["payload"] for e in events if e["event"]["type"] == "permission.networkRequested")
            await runtime.dispatch("approval.resolve", {"approvalId": request["approvalId"], "approved": approved})
            await asyncio.wait_for(running, 2)
            assert bool((captured[0][1] or {}).get("web_search")) is approved
            assert not any(t.name == "web_search" for t in (captured[0][0].tools or []))
            assert any(e["event"]["type"] == "permission.networkResolved" for e in events)
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_stopping_native_search_approval_never_starts_provider(monkeypatch, tmp_path):
    async def scenario():
        requested = asyncio.Event()
        events, calls = [], []
        def emit(event):
            events.append(event)
            if event["event"]["type"] == "permission.networkRequested":
                requested.set()
        def provider(*args):
            calls.append(args)
            raise AssertionError("Provider must not start while approval is pending")
        monkeypatch.setattr("agent_llm.compat.stream_simple", provider)
        runtime = DesktopRuntime(emit)
        runtime._workspace = tmp_path
        runtime._session = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=DEEPSEEK_MODELS["deepseek-v4-pro"],
            web_search_enabled=True, before_native_web_search=runtime._before_native_web_search,
        ))
        try:
            await runtime.dispatch("run.start", {"text": "research"})
            await asyncio.wait_for(requested.wait(), 2)
            result = await runtime.dispatch("run.abort", {})
            assert result["aborted"]
            assert calls == []
            assert runtime._approvals == {}
            assert any(e["event"]["type"] == "permission.networkResolved" for e in events)
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_permission_setting_rejects_invalid_values(tmp_path):
    settings = Settings(desktop_permission_mode="bad")
    with pytest.raises(ValueError, match="desktop_permission_mode"):
        SettingsManager(tmp_path / "settings.json").save(settings)
