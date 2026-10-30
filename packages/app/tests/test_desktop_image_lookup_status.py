"""Search snapshots consistently describe the selected model's real backend."""
from __future__ import annotations

import asyncio
import copy

import pytest
from agent_core import SessionManager
from agent_llm.providers.deepseek_models import DEEPSEEK_MODELS

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.desktop.runtime import DesktopRuntime


def make_runtime(tmp_path, *, model_id, enabled):
    events = []
    runtime = DesktopRuntime(events.append)
    session = AgentSession(AgentSessionConfig(
        cwd=str(tmp_path), model=copy.deepcopy(DEEPSEEK_MODELS[model_id]), tools=[],
        session_manager=SessionManager.create(cwd=str(tmp_path), sessions_dir=tmp_path / "sessions"),
        web_search_enabled=enabled,
    ))
    runtime._session = session
    runtime._workspace = tmp_path
    return runtime, session, events


@pytest.mark.parametrize("model_id,credential_provider,backend", [
    ("deepseek-flash", "deepseek", "deepseek-search"),
    ("deepseek-v4-pro", "deepseek", "deepseek-native"),
])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("configured", [False, True])
def test_status_snapshot_and_workspace_share_authoritative_search_payload(
    tmp_path, monkeypatch, model_id, credential_provider, backend, enabled, configured,
):
    async def scenario():
        runtime, _session, _events = make_runtime(tmp_path, model_id=model_id, enabled=enabled)
        providers = []

        def resolve(provider):
            providers.append(provider)
            return "configured-test-key" if configured and provider == credential_provider else None

        monkeypatch.setattr("coding_agent.desktop.runtime._resolve_api_key_for", resolve)
        try:
            expected = {
                "enabled": enabled, "backend": backend, "available": configured,
                "status": "idle" if enabled and configured else "disabled" if not enabled else "unavailable",
                "errorCode": None if configured else "WEB_SEARCH_NOT_CONFIGURED",
            }
            assert await runtime.dispatch("webSearch.status", {}) == expected
            assert (await runtime.dispatch("session.snapshot", {}))["webSearch"] == expected
            assert runtime._workspace_payload()["webSearch"] == expected
            assert providers == [credential_provider] * 3
        finally:
            await runtime.dispose()

    asyncio.run(scenario())


def test_disabled_pro_with_only_deepseek_key_can_reenable_native_search(tmp_path, monkeypatch):
    async def scenario():
        runtime, session, events = make_runtime(tmp_path, model_id="deepseek-v4-pro", enabled=False)
        monkeypatch.setattr(
            "coding_agent.desktop.runtime._resolve_api_key_for",
            lambda provider: "configured-test-key" if provider == "deepseek" else None,
        )
        try:
            disabled = (await runtime.dispatch("session.snapshot", {}))["webSearch"]
            assert disabled["enabled"] is False and disabled["available"] is True
            assert disabled["backend"] == "deepseek-native"
            enabled = await runtime.dispatch("webSearch.setEnabled", {"enabled": True})
            assert enabled["status"] == "idle" and enabled["available"] is True
            assert session.native_web_search_enabled is True
            assert events[-1]["event"] == {"type": "webSearch.changed", "payload": enabled}
            assert (await runtime.dispatch("session.snapshot", {}))["webSearch"] == enabled
        finally:
            await runtime.dispose()

    asyncio.run(scenario())


def test_plan_drafting_allows_pro_without_changing_selected_search_backend(tmp_path, monkeypatch):
    async def scenario():
        runtime, session, _events = make_runtime(tmp_path, model_id="deepseek-v4-pro", enabled=True)
        monkeypatch.setattr(
            "coding_agent.desktop.runtime._resolve_api_key_for",
            lambda provider: "configured-test-key" if provider == "deepseek" else None,
        )
        try:
            session.enter_plan_mode()
            assert session.native_web_search_enabled is True
            snapshot = await runtime.dispatch("session.snapshot", {})
            assert snapshot["collaborationMode"] == "plan"
            assert snapshot["webSearch"]["backend"] == "deepseek-native"
            assert snapshot["webSearch"]["enabled"] is True
            assert snapshot["webSearch"]["available"] is True
            assert "web_search" in {tool.name for tool in session.tools}
        finally:
            await runtime.dispose()

    asyncio.run(scenario())


def test_model_switch_publishes_search_status_before_authoritative_model_event(tmp_path, monkeypatch):
    async def scenario():
        runtime, session, events = make_runtime(tmp_path, model_id="deepseek-flash", enabled=True)
        models = [copy.deepcopy(DEEPSEEK_MODELS[key]) for key in ("deepseek-flash", "deepseek-v4-pro")]
        monkeypatch.setattr(runtime, "_available_models", lambda: models)
        monkeypatch.setattr(
            "coding_agent.desktop.runtime._resolve_api_key_for",
            lambda provider: "configured-test-key" if provider == "deepseek" else None,
        )
        try:
            for model_id, backend, available in (
                ("deepseek-v4-pro", "deepseek-native", True),
                ("deepseek-flash", "deepseek-search", True),
            ):
                selected = await runtime.dispatch("model.select", {"modelId": model_id, "provider": "deepseek"})
                search = events[-2]
                assert search["sessionId"] == session.session_manager.header.id
                assert search["event"]["type"] == "webSearch.changed"
                payload = search["event"]["payload"]
                assert payload["backend"] == backend and payload["available"] is available
                assert payload == (await runtime.dispatch("session.snapshot", {}))["webSearch"]
                assert payload == runtime._workspace_payload()["webSearch"]
                assert events[-1]["event"] == {"type": "model.changed", "payload": selected}
        finally:
            await runtime.dispose()

    asyncio.run(scenario())
