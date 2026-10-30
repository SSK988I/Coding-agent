"""Search routing and explicit CLI credentials belong to their own session/provider."""
from __future__ import annotations

import asyncio
import importlib
import io
from types import SimpleNamespace

import httpx
import pytest
from agent_llm import (
    AssistantMessage, AssistantMessageEventStream, Model, ModelCost, TextContent,
    ToolCall, ToolResultMessage,
)
from agent_llm.providers.deepseek_models import DEEPSEEK_MODELS

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.plan_mode import PlanState
from coding_agent.desktop.side_conversations import SideConversations
from coding_agent.search.backends.deepseek import DeepSeekSearchBackend
from coding_agent.search.backends.mcp import McpSearchCall
from coding_agent.search.backends.model import ModelSearchBackend
from coding_agent.search.backends.zhipu import ZhipuMcpSearchBackend
from coding_agent.search.types import SearchResponse


def search_tool(session):
    return next(tool for tool in session.tools if tool.name == "web_search")


def test_existing_side_chat_keeps_its_own_search_provider_credentials_and_lifecycle(tmp_path, monkeypatch):
    requests, model_calls = [], []
    keys = {"deepseek": "fake-deepseek-side-key", "zai-coding-cn": "fake-zhipu-parent-key"}

    def http_search(request):
        requests.append(("deepseek", request.headers["authorization"]))
        return httpx.Response(200, json={"results": []})

    class McpClient:
        def __init__(self, *, authorization, **kwargs):
            self.authorization = authorization

        async def call(self, query, *, signal=None):
            requests.append(("zai-coding-cn", self.authorization))
            return McpSearchCall(structured_content={"results": []}, text_content=())

        async def aclose(self):
            pass

    monkeypatch.setattr(
        "coding_agent.search.backends.model.DeepSeekSearchBackend",
        lambda resolver, **kwargs: DeepSeekSearchBackend(
            resolver, **kwargs,
            client_factory=lambda **options: httpx.AsyncClient(
                **options, transport=httpx.MockTransport(http_search),
            ),
        ),
    )
    monkeypatch.setattr(
        "coding_agent.search.backends.model.ZhipuMcpSearchBackend",
        lambda resolver, **kwargs: ZhipuMcpSearchBackend(resolver, **kwargs, client_factory=McpClient),
    )

    def provider(model, context, _options):
        model_calls.append(model)
        assert "web_search" in {tool.name for tool in context.tools or []}
        if isinstance(context.messages[-1], ToolResultMessage):
            assert not context.messages[-1].is_error
            message = AssistantMessage(content=[TextContent(text="done")])
        else:
            message = AssistantMessage(content=[ToolCall(
                id=f"search-{len(model_calls)}", name="web_search", arguments={"query": "public landmark"},
            )], stop_reason="tool_use")
        stream = AssistantMessageEventStream()
        stream.push({"type": "done", "reason": message.stop_reason, "message": message})
        return stream

    monkeypatch.setattr("agent_llm.compat.stream_simple", provider)

    async def scenario():
        flash = DEEPSEEK_MODELS["deepseek-flash"]
        parent = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=flash, tools=[], web_search_enabled=True,
            web_search_credential_resolver=keys.get,
        ))
        side = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "full_access")
        identity = side.open()["conversationId"]
        live = side.records[identity]
        parent_backend = search_tool(parent).backend
        try:
            for switched in (False, True):
                if switched:
                    parent.set_model(Model(id="glm-parent", provider="zai-coding-cn", cost=ModelCost(0, 0, 0, 0)))
                side.send(identity, "research a public landmark")
                await asyncio.wait_for(live.task, 2)
                assert live.status == "settled", live.error
            assert requests == [("deepseek", f"Bearer {keys['deepseek']}")] * 2
            assert all(model is flash for model in model_calls)
            assert live.session.model is flash
            assert live.session.web_search_backend_label == "deepseek-search"
            assert live.session.web_search_credential_provider == "deepseek"
            assert search_tool(live.session).backend is not parent_backend
            child_backend = search_tool(live.session).backend
            await side.close()
            assert child_backend._closed is True
            assert parent_backend._closed is False
            await search_tool(parent).execute("parent-search", {"query": "public landmark"})
            assert requests[-1] == ("zai-coding-cn", keys["zai-coding-cn"])
        finally:
            await side.close()
            await parent.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("transition,allowed", [
    ("drafting", True), ("ready", False), ("awaiting_answer", False),
    ("recovery_error", False), ("disabled", False), ("denied", False),
    ("ready_during_approval", False), ("disabled_during_approval", False),
])
def test_side_search_keeps_current_parent_policy_and_permissions_after_model_switch(
    tmp_path, monkeypatch, transition, allowed,
):
    searches, results = [], []

    async def search(_backend, query, *, signal=None):
        searches.append(query)
        return SearchResponse(query=query.query, backend="deepseek-search", results=())

    def provider(_model, context, _options):
        if isinstance(context.messages[-1], ToolResultMessage):
            results.append(context.messages[-1])
            message = AssistantMessage(content=[TextContent(text="done")])
        else:
            message = AssistantMessage(content=[ToolCall(
                id="search-policy", name="web_search", arguments={"query": "public landmark"},
            )], stop_reason="tool_use")
        stream = AssistantMessageEventStream()
        stream.push({"type": "done", "reason": message.stop_reason, "message": message})
        return stream

    monkeypatch.setattr(DeepSeekSearchBackend, "search", search)
    monkeypatch.setattr("agent_llm.compat.stream_simple", provider)

    async def scenario():
        parent = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=DEEPSEEK_MODELS["deepseek-flash"], tools=[], web_search_enabled=True,
        ))
        permission = {"mode": "full_access"}
        approval_shown = asyncio.Event()
        side = SideConversations(
            parent, tmp_path / "cache",
            lambda _event, payload: approval_shown.set() if payload.get("approval") else None,
            lambda: permission["mode"],
        )
        identity = side.open()["conversationId"]
        live = side.records[identity]
        try:
            parent.set_model(Model(id="glm-parent", provider="zai-coding-cn", cost=ModelCost(0, 0, 0, 0)))
            if transition == "drafting":
                parent.enter_plan_mode()
            elif transition in {"ready", "awaiting_answer", "recovery_error"}:
                parent._plan_state = PlanState(mode="plan", phase=transition)
                parent._refresh_collaboration_runtime()
            elif transition == "disabled":
                await parent.set_web_search_enabled(False)
            else:
                permission["mode"] = "ask"
            side.send(identity, "research")
            if permission["mode"] == "ask":
                await asyncio.wait_for(approval_shown.wait(), 2)
                assert searches == []
                if transition == "ready_during_approval":
                    parent._plan_state = PlanState(mode="plan", phase="ready")
                    parent._refresh_collaboration_runtime()
                elif transition == "disabled_during_approval":
                    await parent.set_web_search_enabled(False)
                side.resolve(identity, live.approval["approvalId"], transition != "denied")
            await asyncio.wait_for(live.task, 2)
            assert bool(searches) is allowed
            assert len(results) == 1 and results[0].is_error is not allowed
        finally:
            await side.close()
            await parent.aclose()

    asyncio.run(scenario())


def test_side_chat_preserves_explicit_host_backend_and_does_not_close_it(tmp_path):
    async def scenario():
        backend = ModelSearchBackend(lambda: "deepseek", lambda _provider: "fake-host-key")
        parent = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=DEEPSEEK_MODELS["deepseek-flash"], tools=[],
            web_search_enabled=True, web_search_backend=backend, web_search_backend_name="host-search",
        ))
        side = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "full_access")
        try:
            child = side.records[side.open()["conversationId"]].session
            assert search_tool(child).backend is backend
            assert child.web_search_backend_label == "host-search"
            await side.close()
            assert backend._closed is False
        finally:
            await side.close()
            await parent.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("launch_provider,model_id,search_provider,uses_override", [
    ("deepseek", "deepseek-flash", "deepseek", True),
    ("deepseek", "deepseek-v4-pro", "deepseek", True),
    ("deepseek", "deepseek-flash", "zai-coding-cn", False),
    ("glm", "glm-5.2", "deepseek", False),
    ("glm", "glm-5.2", "zai-coding-cn", True),
])
def test_cli_search_config_scopes_explicit_key_to_launch_model_provider(
    tmp_path, monkeypatch, launch_provider, model_id, search_provider, uses_override,
):
    cli = importlib.import_module("coding_agent.cli.main")
    captured, resolutions = [], []
    override_key = "fake-cli-explicit-provider-key"

    def resolve(provider_id, *, override=None):
        resolutions.append((provider_id, override))
        return override or f"fake-stored-{provider_id}-key"

    def create_session(config):
        captured.append(config)
        return SimpleNamespace(collaboration_mode="default")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(cli, "_configure_output_encoding", lambda: None)
    monkeypatch.setattr(cli, "_take_over_stdout", lambda: None)
    monkeypatch.setattr(cli, "_load_context_for_run", lambda *_: [])
    monkeypatch.setattr(cli, "_load_skills_for_run", lambda *_: [])
    monkeypatch.setattr(cli, "_load_prompt_templates_for_run", lambda *_: [])
    monkeypatch.setattr(cli, "_resolve_api_key_for", resolve)
    monkeypatch.setattr(cli, "AgentSession", create_session)
    monkeypatch.setattr(cli, "_run_print", lambda *_: 0)
    assert cli.main([
        "--print", "--provider", launch_provider, "--model", model_id,
        "--api-key", override_key, "--web-search", "public landmark",
    ]) == 0
    assert len(captured) == 1
    config = captured[0]
    # Invoke the actual resolver wired by CLI startup, rather than a test copy.
    result = config.web_search_credential_resolver(search_provider)
    assert result == (override_key if uses_override else f"fake-stored-{search_provider}-key")
    assert resolutions == [(search_provider, override_key if uses_override else None)]
