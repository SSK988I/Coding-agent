"""Host approval gates keep Plan drafting searches read-only and revocable."""
from __future__ import annotations

import asyncio

import pytest
from agent_core import BeforeToolCallResult, SessionManager
from agent_llm.providers.deepseek_models import DEEPSEEK_MODELS

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.plan_mode import PlanState
from coding_agent.core.settings import SettingsManager
from coding_agent.desktop.runtime import DesktopRuntime
from coding_agent.desktop.side_conversations import SideConversations
from coding_agent.modes.interactive.interactive_mode import InteractiveMode


def plan_session(tmp_path, permission):
    return AgentSession(AgentSessionConfig(
        cwd=str(tmp_path), session_manager=SessionManager.create(in_memory=True),
        model=DEEPSEEK_MODELS["deepseek-v4-pro"], web_search_enabled=True,
        settings_manager=SettingsManager(tmp_path / "settings.json"),
        permission_mode=permission, collaboration_mode="plan",
    ))


@pytest.mark.parametrize("adapter", ["desktop", "tui"])
@pytest.mark.parametrize("permission", ["ask", "auto", "full_access"])
@pytest.mark.parametrize("approved", [True, False])
def test_plan_drafting_native_search_preserves_host_approval(tmp_path, monkeypatch, adapter, permission, approved):
    async def scenario():
        session = plan_session(tmp_path, permission)
        requests = []
        if adapter == "desktop":
            host = DesktopRuntime(lambda _: None)
            host._session, host._run_id, host._permission_mode = session, "run-1", permission

            async def request(_context, _signal):
                requests.append("network")
                return None if approved else BeforeToolCallResult(block=True)

            monkeypatch.setattr(host, "_request_approval", request)
        else:
            host = InteractiveMode(session)

            async def request(*_args):
                requests.append("network")
                return approved

            monkeypatch.setattr(host, "_ask_permission", request)
        try:
            assert session.collaboration_mode == "plan"
            assert session.plan_state.phase == "drafting"
            assert await host._before_native_web_search(asyncio.Event()) is (approved if permission == "ask" else True)
            assert requests == (["network"] if permission == "ask" else [])
            assert session.collaboration_mode == "plan"
            assert not {"write", "edit", "bash"} & {tool.name for tool in session.tools}
        finally:
            await session.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("adapter", ["desktop", "tui"])
@pytest.mark.parametrize("phase", ["awaiting_answer", "ready", "uncertain", "recovery_error"])
@pytest.mark.parametrize("permission", ["ask", "auto", "full_access"])
def test_host_native_search_rejects_non_drafting_plan_before_approval(tmp_path, monkeypatch, adapter, phase, permission):
    async def scenario():
        session = plan_session(tmp_path, permission)
        session._plan_state = PlanState(mode="plan", phase=phase)
        session._refresh_collaboration_runtime()
        if adapter == "desktop":
            host = DesktopRuntime(lambda _: None)
            host._session, host._run_id, host._permission_mode = session, "run-1", permission
        else:
            host = InteractiveMode(session)

        async def unexpected(*_args):
            raise AssertionError("Unavailable search must not request approval")

        monkeypatch.setattr(host, "_request_approval" if adapter == "desktop" else "_ask_permission", unexpected)
        try:
            assert not await host._before_native_web_search(asyncio.Event())
        finally:
            await session.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("adapter", ["desktop", "tui"])
@pytest.mark.parametrize("transition", ["ready", "disabled", "session_changed"])
def test_host_rechecks_native_search_after_approval_await(tmp_path, monkeypatch, adapter, transition):
    async def scenario():
        session = plan_session(tmp_path, "ask")
        if adapter == "desktop":
            host = DesktopRuntime(lambda _: None)
            host._session, host._run_id, host._permission_mode = session, "run-1", "ask"
        else:
            host = InteractiveMode(session)
        replacement = None

        async def request(*_args):
            nonlocal replacement
            if transition == "ready":
                session._plan_state = PlanState(mode="plan", phase="ready")
                session._refresh_collaboration_runtime()
            elif transition == "disabled":
                await session.set_web_search_enabled(False)
            else:
                replacement = plan_session(tmp_path, "ask")
                host._session = replacement
            return None if adapter == "desktop" else True

        monkeypatch.setattr(host, "_request_approval" if adapter == "desktop" else "_ask_permission", request)
        try:
            assert not await host._before_native_web_search(asyncio.Event())
        finally:
            await session.aclose()
            if replacement is not None:
                await replacement.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["unchanged", "ready", "disabled", "closed"])
def test_side_native_search_rechecks_drafting_parent_after_approval(tmp_path, monkeypatch, transition):
    async def scenario():
        parent = plan_session(tmp_path, "ask")
        side = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "ask")
        identity = side.open()["conversationId"]
        live = side.records[identity]

        async def request(*_args):
            if transition == "ready":
                parent._plan_state = PlanState(mode="plan", phase="ready")
                parent._refresh_collaboration_runtime()
            elif transition == "disabled":
                await parent.set_web_search_enabled(False)
            elif transition == "closed":
                side.closed = True
            return True

        monkeypatch.setattr(side, "_request_decision", request)
        try:
            assert await side._approve_native_search(live, asyncio.Event()) is (transition == "unchanged")
            assert parent.collaboration_mode == "plan"
        finally:
            await side.close()
            await parent.aclose()
    asyncio.run(scenario())
