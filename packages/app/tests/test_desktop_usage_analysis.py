"""The analysis RPC is session-scoped and read-only, including Plan/archives."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from agent_core import SessionManager
from agent_llm import AssistantMessage, Model, ModelCost, TextContent, Usage, UserMessage

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.desktop.protocol import RpcError
from coding_agent.desktop.runtime import DesktopRuntime


def runtime_with_usage(tmp_path, events):
    manager = SessionManager.create(cwd=str(tmp_path), sessions_dir=tmp_path / "sessions")
    manager.append_message(UserMessage(content="private prompt"))
    manager.append_message(AssistantMessage(
        model="old-model", provider="test", content=[TextContent(text="private answer")],
        usage=Usage(input=50, output=12, cache_read=50, cache_read_reported=True),
    ))
    runtime = DesktopRuntime(events.append)
    runtime._workspace = tmp_path
    runtime._session = AgentSession(AgentSessionConfig(
        cwd=str(tmp_path), session_manager=manager, no_builtin_tools=True,
        model=Model(id="current-model", provider="test", context_window=10000,
                    base_url="https://offline.invalid", cost=ModelCost(0, 0, 0, 0)),
    ))
    return runtime, manager


def test_empty_session_analysis_does_not_create_history_or_invent_requests(tmp_path):
    async def scenario():
        manager = SessionManager.create(cwd=str(tmp_path), sessions_dir=tmp_path / "sessions")
        runtime = DesktopRuntime(lambda _event: None)
        runtime._workspace = tmp_path
        runtime._session = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), session_manager=manager, no_builtin_tools=True,
            model=Model(id="offline", provider="test", cost=ModelCost(0, 0, 0, 0)),
        ))
        try:
            result = await runtime.dispatch("session.usage", {"sessionId": manager.header.id})
            assert result["requests"] == [] and result["leafId"] is None
            assert result["stats"]["metrics"]["input_tokens"] == 0
            assert manager.path is not None and not manager.path.exists()
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["default", "plan", "archived"])
def test_usage_rpc_is_read_only_and_uses_authoritative_branch_stats(tmp_path, monkeypatch, mode):
    async def scenario():
        events = []
        runtime, manager = runtime_with_usage(tmp_path, events)
        assert runtime._session is not None and manager.path is not None
        if mode == "plan":
            runtime._session.enter_plan_mode()
        elif mode == "archived":
            manager.set_archived(True)
        before = manager.path.read_bytes()
        entries = list(manager.entries)
        leaf = manager.leaf_id
        drive = AsyncMock()
        monkeypatch.setattr(runtime, "_drive_run", drive)
        try:
            result = await runtime.dispatch("session.usage", {"sessionId": manager.header.id})
            assert result["sessionId"] == manager.header.id and result["leafId"] == leaf
            assert result["stats"]["metrics"]["input_tokens"] == 100
            assert result["stats"]["metrics"]["cache_hit_rate"] == 50
            assert len(result["requests"]) == 1
            assert result["requests"][0]["model"] == "old-model"
            assert result["requests"][0]["inputTokens"] == 100
            assert result["requests"][0]["outputTokens"] == 12
            assert manager.entries == entries and manager.leaf_id == leaf
            assert manager.path.read_bytes() == before
            assert events == []
            drive.assert_not_called()
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("session_id", [None, "", " \n", True, 42, [], {}])
def test_usage_rpc_requires_nonempty_session_identifier(tmp_path, session_id):
    async def scenario():
        runtime, _manager = runtime_with_usage(tmp_path, [])
        try:
            with pytest.raises(RpcError) as error:
                await runtime.dispatch("session.usage", {"sessionId": session_id})
            assert error.value.code == "INVALID_PARAMS"
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_usage_rpc_rejects_a_stale_session_instead_of_returning_another_history(tmp_path):
    async def scenario():
        runtime, manager = runtime_with_usage(tmp_path, [])
        try:
            with pytest.raises(RpcError) as error:
                await runtime.dispatch("session.usage", {"sessionId": "previous-session"})
            assert error.value.code == "STALE_SESSION"
            assert (await runtime.dispatch("session.usage", {"sessionId": manager.header.id}))["sessionId"] == manager.header.id
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_usage_rpc_remains_available_during_a_live_run(tmp_path):
    async def scenario():
        runtime, manager = runtime_with_usage(tmp_path, [])
        active = asyncio.create_task(asyncio.Event().wait())
        runtime._run_task = active
        try:
            result = await runtime.dispatch("session.usage", {"sessionId": manager.header.id})
            assert len(result["requests"]) == 1
            assert runtime._run_task is active and not active.done()
        finally:
            runtime._run_task = None
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
            await runtime.dispose()
    asyncio.run(scenario())
