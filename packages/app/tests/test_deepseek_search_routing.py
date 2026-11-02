"""Provider routing keeps DeepSeek search independent of Zhipu credentials."""
from __future__ import annotations

import asyncio
import base64
import io

import pytest
from agent_core import AgentContext, BeforeToolCallContext
from agent_llm import (
    AssistantMessage, AssistantMessageEventStream, Context, ImageContent,
    Model, ModelCost, TextContent, Tool, ToolCall, ToolResultMessage, UserMessage,
)
from agent_llm.providers.deepseek_models import DEEPSEEK_MODELS
from PIL import Image

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.plan_mode import PlanState
from coding_agent.desktop.runtime import DesktopRuntime
from coding_agent.search.types import SearchQuery, SearchResponse, SearchResult


def other_model() -> Model:
    return Model(id="other-model", provider="test", cost=ModelCost(0, 0, 0, 0))


def search_tool(session: AgentSession):
    return next(tool for tool in session.tools if tool.name == "web_search")


def search_context() -> BeforeToolCallContext:
    call = ToolCall(id="search-call", name="web_search", arguments={"query": "public landmark"})
    return BeforeToolCallContext(
        assistant_message=AssistantMessage(content=[call]), tool_call=call,
        args=call.arguments, context=AgentContext(),
    )


class HostSearchBackend:
    name = "host-search"

    async def search(self, query: SearchQuery, *, signal=None) -> SearchResponse:
        del signal
        return SearchResponse(query=query.query, backend=self.name, results=())

    async def aclose(self) -> None:
        pass

    def request_stop(self) -> None:
        pass


def test_closed_router_cannot_reopen_search_clients(monkeypatch) -> None:
    from coding_agent.search.backends.model import ModelSearchBackend
    from coding_agent.search.types import SearchBackendError

    created = []
    def make_backend(*args, **kwargs):
        created.append(True)
        return HostSearchBackend()
    monkeypatch.setattr(
        "coding_agent.search.backends.model.DeepSeekSearchBackend",
        make_backend,
    )

    async def scenario():
        router = ModelSearchBackend(lambda: "deepseek", lambda _provider: "unused-test-key")
        await router.aclose()
        with pytest.raises(SearchBackendError) as caught:
            await router.search(SearchQuery("public landmark"))
        assert caught.value.code == "WEB_SEARCH_ABORTED"
        assert created == [], "A retained closed tool must not open a new network client"

    asyncio.run(scenario())


def test_flash_defaults_to_deepseek_search_without_switching_chat_model() -> None:
    from coding_agent.search.backends.model import ModelSearchBackend

    model = DEEPSEEK_MODELS["deepseek-flash"]
    session = AgentSession(AgentSessionConfig(model=model, tools=[], web_search_enabled=True))
    try:
        assert isinstance(search_tool(session).backend, ModelSearchBackend)
        assert session.web_search_backend_label == "deepseek-search"
        assert session.web_search_credential_provider == "deepseek"
        assert session.native_web_search_enabled is False
        assert session.model is model
        assert session.agent.state.model is model
        assert "image" in session.model.input
    finally:
        asyncio.run(session.aclose())


@pytest.mark.parametrize("plan", [False, True])
def test_flash_image_turn_searches_with_keywords_and_returns_sources_without_model_switch(monkeypatch, plan) -> None:
    from coding_agent.search.backends import DeepSeekSearchBackend

    calls, requests = [], []
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "green").save(output, format="PNG")
    image = ImageContent(data=base64.b64encode(output.getvalue()).decode("ascii"), mime_type="image/png")
    model = DEEPSEEK_MODELS["deepseek-flash"]

    async def search(_backend, query, *, signal=None):
        calls.append(query)
        return SearchResponse(
            query=query.query, backend="deepseek-search",
            results=(SearchResult("Public landmark", "https://example.com/landmark", "Public reference"),),
        )

    def provider(sent_model, context, _options):
        requests.append(context)
        assert sent_model is model
        if len(requests) == 1:
            assert "web_search" in {tool.name for tool in context.tools}
            message = AssistantMessage(content=[ToolCall(
                id="identify-landmark", name="web_search", arguments={"query": "public landmark green roof"},
            )], stop_reason="tool_use")
        else:
            result = context.messages[-1]
            assert isinstance(result, ToolResultMessage) and not result.is_error
            assert result.details["backend"] == "deepseek-search"
            assert "https://example.com/landmark" in result.content[0].text
            message = AssistantMessage(content=[TextContent(text="Reference: https://example.com/landmark")])
        stream = AssistantMessageEventStream()
        stream.push({"type": "done", "reason": message.stop_reason, "message": message})
        return stream

    monkeypatch.setattr(DeepSeekSearchBackend, "search", search)
    monkeypatch.setattr("agent_llm.compat.stream_simple", provider)

    async def scenario() -> None:
        session = AgentSession(AgentSessionConfig(
            model=model, tools=[], web_search_enabled=True, permission_mode="full_access",
        ))
        try:
            if plan:
                session.enter_plan_mode()
            await session.prompt(UserMessage(content=[TextContent(text="Which public landmark is this?"), image]))
            assert len(requests) == 2
            assert calls == [SearchQuery("public landmark green roof")]
            assert image.data not in repr(calls)
            assert session.model is model and session.agent.state.model is model
            assert session.state.messages[-1].stop_reason == "stop"
            assert "https://example.com/landmark" in session.state.messages[-1].content[0].text
            if plan:
                assert session.collaboration_mode == "plan" and session.plan_state.phase == "drafting"
                assert session.plan_state.latest_revision is None
        finally:
            await session.aclose()

    asyncio.run(scenario())


def test_pro_preserves_native_search_without_duplicate_function(monkeypatch) -> None:
    model = DEEPSEEK_MODELS["deepseek-v4-pro"]
    captured = []

    def provider(sent_model, context, options):
        captured.append((sent_model, context, options))
        stream = AssistantMessageEventStream()
        stream.push({"type": "done", "reason": "stop", "message": AssistantMessage()})
        return stream

    monkeypatch.setattr("agent_llm.compat.stream_simple", provider)
    monkeypatch.setattr("coding_agent.core.agent_session.retrying_stream", lambda factory, *_a, **_kw: factory())

    async def scenario():
        session = AgentSession(AgentSessionConfig(
            model=model, tools=[], web_search_enabled=True, permission_mode="full_access",
        ))
        try:
            stream = session._create_stream_fn()(model, Context(tools=[Tool(name="web_search")]), None)
            message = await stream.result()
            assert getattr(message, "_usage_context_fingerprint", None)
            sent_model, context, options = captured[0]
            assert sent_model is model
            assert session.native_web_search_enabled is True
            assert session.web_search_backend_label == "deepseek-native"
            assert options["web_search"] is True
            assert not any(tool.name == "web_search" for tool in context.tools or [])
            await stream.aclose()
        finally:
            await session.aclose()

    asyncio.run(scenario())


def test_switching_provider_routes_the_builtin_search_backend_without_stale_delegates(monkeypatch) -> None:
    from coding_agent.search.backends import DeepSeekSearchBackend, ZhipuMcpSearchBackend

    calls = []

    async def deepseek_search(_backend, query, *, signal=None):
        calls.append(("deepseek", query))
        return SearchResponse(query=query.query, backend="deepseek-search", results=())

    async def zhipu_search(_backend, query, *, signal=None):
        calls.append(("zai-coding-cn", query))
        return SearchResponse(query=query.query, backend="zhipu-mcp", results=())

    monkeypatch.setattr(DeepSeekSearchBackend, "search", deepseek_search)
    monkeypatch.setattr(ZhipuMcpSearchBackend, "search", zhipu_search)

    async def scenario() -> None:
        session = AgentSession(AgentSessionConfig(
            model=DEEPSEEK_MODELS["deepseek-flash"], tools=[], web_search_enabled=True,
        ))
        tool = search_tool(session)
        try:
            for model, provider, label in (
                (other_model(), "zai-coding-cn", "zhipu-mcp"),
                (DEEPSEEK_MODELS["deepseek-v4-pro"], "deepseek", "deepseek-native"),
                (other_model(), "zai-coding-cn", "zhipu-mcp"),
                (DEEPSEEK_MODELS["deepseek-flash"], "deepseek", "deepseek-search"),
            ):
                session.set_model(model)
                assert search_tool(session) is tool
                assert session.web_search_backend_label == label
                assert session.web_search_credential_provider == provider
                assert session.model is model
                await tool.execute("search-1", {"query": "public landmark"})
                assert calls[-1] == (provider, SearchQuery("public landmark"))
            assert len(calls) == 4
        finally:
            await session.aclose()

    asyncio.run(scenario())


def test_disabled_search_uses_current_provider_when_reenabled() -> None:
    from coding_agent.search.backends.model import ModelSearchBackend

    async def scenario() -> None:
        session = AgentSession(AgentSessionConfig(model=other_model(), tools=[]))
        try:
            for model, provider in (
                (DEEPSEEK_MODELS["deepseek-flash"], "deepseek"),
                (other_model(), "zai-coding-cn"),
                (DEEPSEEK_MODELS["deepseek-v4-pro"], "deepseek"),
            ):
                session.set_model(model)
                assert session.web_search_enabled is False
                await session.set_web_search_enabled(True)
                assert isinstance(search_tool(session).backend, ModelSearchBackend)
                assert session.web_search_credential_provider == provider
                await session.set_web_search_enabled(False)
                assert "web_search" not in {tool.name for tool in session.tools}
        finally:
            await session.aclose()

    asyncio.run(scenario())


def test_explicit_host_backend_is_not_replaced_on_provider_switch() -> None:
    backend = HostSearchBackend()
    session = AgentSession(AgentSessionConfig(
        model=DEEPSEEK_MODELS["deepseek-flash"], tools=[],
        web_search_enabled=True, web_search_backend=backend,
    ))
    try:
        for model in (other_model(), DEEPSEEK_MODELS["deepseek-v4-pro"], DEEPSEEK_MODELS["deepseek-flash"]):
            session.set_model(model)
            assert search_tool(session).backend is backend
    finally:
        asyncio.run(session.aclose())


@pytest.mark.parametrize("model_id", ["deepseek-flash", "deepseek-v4-pro"])
@pytest.mark.parametrize("plan", [False, True])
@pytest.mark.parametrize("restriction", [
    {"no_tools": True}, {"no_builtin_tools": True},
    {"allowed_tool_names": ["read"]}, {"excluded_tool_names": ["web_search"]},
])
def test_deepseek_routing_cannot_bypass_tool_filters(model_id, plan, restriction) -> None:
    async def scenario() -> None:
        session = AgentSession(AgentSessionConfig(
            model=DEEPSEEK_MODELS[model_id], tools=[], web_search_enabled=True, **restriction,
        ))
        try:
            if plan:
                session.enter_plan_mode()
            assert "web_search" not in {tool.name for tool in session.tools}
            assert session.native_web_search_enabled is False
            await session.set_web_search_enabled(False)
            await session.set_web_search_enabled(True)
            assert "web_search" not in {tool.name for tool in session.tools}
            assert session.native_web_search_enabled is False
        finally:
            await session.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("model_id", ["deepseek-flash", "deepseek-v4-pro"])
@pytest.mark.parametrize("phase", ["awaiting_answer", "ready", "uncertain", "recovery_error"])
def test_deepseek_search_is_blocked_by_plan_or_recovery_even_for_stale_calls(model_id, phase) -> None:
    async def scenario() -> None:
        session = AgentSession(AgentSessionConfig(
            model=DEEPSEEK_MODELS[model_id], tools=[], web_search_enabled=True, permission_mode="full_access",
        ))
        try:
            session._plan_state = PlanState(mode="default" if phase in {"uncertain", "recovery_error"} else "plan", phase=phase)
            session._refresh_collaboration_runtime()
            assert "web_search" not in {tool.name for tool in session.tools}
            assert session.native_web_search_enabled is False
            decision = await session._before_tool_call(search_context(), asyncio.Event())
            assert decision is not None and decision.block is True
        finally:
            await session.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode,blocked", [("ask", True), ("auto", False), ("full_access", False)])
def test_flash_delegated_search_retains_existing_network_permission_gate(mode, blocked) -> None:
    async def scenario() -> None:
        session = AgentSession(AgentSessionConfig(
            model=DEEPSEEK_MODELS["deepseek-flash"], tools=[], web_search_enabled=True, permission_mode=mode,
        ))
        try:
            decision = await session._before_tool_call(search_context(), asyncio.Event())
            assert bool(decision and decision.block) is blocked
            if blocked:
                assert decision.code == "PERMISSION_APPROVAL_REQUIRED"
        finally:
            await session.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("model_id", ["deepseek-flash", "deepseek-v4-pro", "other"])
@pytest.mark.parametrize("configured_provider", ["deepseek", "zai-coding-cn", None])
def test_desktop_status_uses_the_effective_search_provider_credential(monkeypatch, model_id, configured_provider) -> None:
    model = other_model() if model_id == "other" else DEEPSEEK_MODELS[model_id]
    session = AgentSession(AgentSessionConfig(model=model, tools=[], web_search_enabled=True))
    runtime = DesktopRuntime(lambda _event: None)
    runtime._session = session
    requested = []

    def resolve(provider):
        requested.append(provider)
        return "configured-test-key" if provider == configured_provider else None

    monkeypatch.setattr("coding_agent.desktop.runtime._resolve_api_key_for", resolve)
    try:
        status = asyncio.run(runtime.dispatch("webSearch.status", {}))
        expected_provider = "zai-coding-cn" if model_id == "other" else "deepseek"
        assert requested == [expected_provider]
        assert status["available"] is (configured_provider == expected_provider)
        assert status["status"] == ("idle" if configured_provider == expected_provider else "unavailable")
    finally:
        asyncio.run(session.aclose())


@pytest.mark.parametrize("model_id", ["deepseek-flash", "deepseek-v4-pro"])
def test_disabling_deepseek_search_does_not_switch_status_to_zhipu(monkeypatch, model_id) -> None:
    async def scenario() -> None:
        session = AgentSession(AgentSessionConfig(model=DEEPSEEK_MODELS[model_id], tools=[], web_search_enabled=True))
        runtime = DesktopRuntime(lambda _event: None)
        runtime._session = session
        requested = []

        def resolve(provider):
            requested.append(provider)
            return "deepseek-test-key" if provider == "deepseek" else None

        monkeypatch.setattr("coding_agent.desktop.runtime._resolve_api_key_for", resolve)
        try:
            disabled = await runtime.dispatch("webSearch.setEnabled", {"enabled": False})
            assert disabled["enabled"] is False and disabled["available"] is True
            assert disabled["status"] == "disabled"
            enabled = await runtime.dispatch("webSearch.setEnabled", {"enabled": True})
            assert enabled["enabled"] is True and enabled["available"] is True
            assert enabled["status"] == "idle"
            assert requested == ["deepseek", "deepseek"]
        finally:
            await session.aclose()

    asyncio.run(scenario())
