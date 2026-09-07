from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from agent_llm import Context, Model, ModelCost, Tool

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.settings import SettingsManager
from coding_agent.desktop.runtime import DesktopRuntime
from coding_agent.search.backends.mcp import McpSearchCall, McpSearchClient, map_mcp_error
from coding_agent.search.backends.zhipu import ZhipuMcpSearchBackend
from coding_agent.search.formatter import format_response, normalize_results
from coding_agent.search.tool import WebSearchTool
from coding_agent.search.types import SearchBackendError, SearchQuery, SearchResponse, SearchResult


class _FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.queries: list[SearchQuery] = []
        self.closed = False
        self.stop_requested = False

    async def search(self, query: SearchQuery, *, signal=None) -> SearchResponse:
        del signal
        self.queries.append(query)
        return SearchResponse(
            query=query.query,
            backend=self.name,
            results=(SearchResult(
                title="Python <release>",
                url="https://python.org/downloads/#latest",
                snippet="Current & supported",
                source="Python.org",
            ),),
        )

    async def aclose(self) -> None:
        self.closed = True

    def request_stop(self) -> None:
        self.stop_requested = True


def _model() -> Model:
    return Model(
        id="search-test",
        provider="test",
        context_window=16_000,
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )


def test_search_query_validates_and_normalizes_domains() -> None:
    query = SearchQuery("  Python 3.14  ", count=3, domains=("Python.org.", "python.org"))
    assert query.query == "Python 3.14"
    assert query.domains == ("python.org",)

    for invalid in ("https://python.org", "python.org/docs", "user@example.com"):
        with pytest.raises(ValueError, match="invalid domain"):
            SearchQuery("test", domains=(invalid,))
    with pytest.raises(ValueError, match="non-empty"):
        SearchQuery(" ")
    with pytest.raises(ValueError, match="between 1 and 10"):
        SearchQuery("test", count=11)


def test_formatter_filters_urls_deduplicates_and_escapes_untrusted_text() -> None:
    results = normalize_results([
        SearchResult("A <tag>", "https://example.com/a#one", "x & y"),
        SearchResult("duplicate", "https://example.com/a#two", "ignored"),
        SearchResult("bad", "javascript:alert(1)", "ignored"),
    ], 10)
    assert len(results) == 1
    assert results[0].url == "https://example.com/a"

    text = format_response(SearchResponse(
        query='x "quoted"', backend="fake", results=results,
    ))
    assert text.startswith('<untrusted_web_results query="x &quot;quoted&quot;" backend="fake">')
    assert "A &lt;tag&gt;" in text
    assert "x &amp; y" in text
    assert text.endswith("</untrusted_web_results>")


def test_web_search_tool_returns_model_text_and_structured_details() -> None:
    backend = _FakeBackend()
    tool = WebSearchTool(backend, default_count=4)
    result = asyncio.run(tool.execute("call-1", {
        "query": "Python release",
        "domains": ["python.org"],
    }))

    assert backend.queries == [SearchQuery("Python release", count=4, domains=("python.org",))]
    assert result.content[0].text.startswith("<untrusted_web_results")
    assert result.details["backend"] == "fake"
    assert result.details["results"][0]["url"] == "https://python.org/downloads/"


def test_zhipu_backend_normalizes_structured_and_json_text_responses() -> None:
    backend = ZhipuMcpSearchBackend(lambda _provider: "test-key")
    response = backend._normalize_response(McpSearchCall(
        structured_content={
            "request_id": "req-1",
            "search_result": [{
                "title": "Example",
                "link": "https://example.com/news",
                "content": "Summary",
                "media": "Example News",
                "publish_date": "2026-08-31",
            }],
        },
        text_content=(),
        warnings=("filter ignored",),
    ), SearchQuery("example"))

    assert response.request_id == "req-1"
    assert response.warnings == ("filter ignored",)
    assert response.results == (SearchResult(
        title="Example",
        url="https://example.com/news",
        snippet="Summary",
        source="Example News",
        published_at="2026-08-31",
    ),)

    markdown = backend._normalize_response(McpSearchCall(
        structured_content=None,
        text_content=("[Python](https://python.org/)",),
    ), SearchQuery("python"))
    assert markdown.results[0].url == "https://python.org/"


def test_mcp_schema_adapter_discovers_provider_field_names() -> None:
    tool = SimpleNamespace(input_schema={
        "type": "object",
        "properties": {
            "searchQuery": {"type": "string"},
            "numResults": {"type": "integer"},
            "domains": {"type": "array"},
        },
    })
    arguments, warnings = McpSearchClient._build_arguments(
        tool,
        SearchQuery("python", count=4, freshness="week", domains=("python.org",)),
    )
    assert arguments == {
        "searchQuery": "python",
        "numResults": 4,
        "domains": ["python.org"],
    }
    assert warnings == ("The selected backend ignored the unsupported freshness filter.",)


def test_mcp_worker_discovers_calls_and_closes_in_one_owner_task() -> None:
    lifecycle: list[str] = []
    calls: list[tuple[str, dict]] = []

    class FakeClient:
        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(
                name="webSearchPrime",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "count": {"type": "integer"},
                    },
                },
            )])

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(
                is_error=False,
                structured_content={"results": []},
                content=[],
            )

    @asynccontextmanager
    async def connection():
        lifecycle.append("opened")
        try:
            yield FakeClient()
        finally:
            lifecycle.append("closed")

    async def scenario() -> McpSearchCall:
        client = McpSearchClient(
            endpoint="https://example.com/mcp",
            authorization="secret-not-for-output",
            preferred_tool="webSearchPrime",
            timeout_seconds=2,
            connection_factory=connection,
        )
        result = await client.call(SearchQuery("python", count=3))
        await client.aclose()
        return result

    result = asyncio.run(scenario())
    assert calls == [("webSearchPrime", {"query": "python", "count": 3})]
    assert result.structured_content == {"results": []}
    assert lifecycle == ["opened", "closed"]


def test_mcp_error_mapping_uses_stable_codes() -> None:
    auth = RuntimeError("401 Unauthorized")
    setattr(auth, "status_code", 401)
    assert map_mcp_error(auth).code == "WEB_SEARCH_AUTH_FAILED"
    assert map_mcp_error(TimeoutError()).code == "WEB_SEARCH_TIMEOUT"


def test_agent_session_registers_filters_and_closes_web_search() -> None:
    backend = _FakeBackend()
    session = AgentSession(AgentSessionConfig(
        model=_model(),
        web_search_enabled=True,
        web_search_backend=backend,
    ))
    assert "web_search" in {tool.name for tool in session._tools}
    asyncio.run(session.aclose())
    assert backend.closed is True
    assert backend.stop_requested is True

    filtered = AgentSession(AgentSessionConfig(
        model=_model(),
        web_search_enabled=True,
        web_search_backend=_FakeBackend(),
        excluded_tool_names=["web_search"],
    ))
    assert "web_search" not in {tool.name for tool in filtered._tools}
    filtered.dispose()


def test_agent_session_can_toggle_web_search_at_runtime() -> None:
    backend = _FakeBackend()
    session = AgentSession(AgentSessionConfig(
        model=_model(),
        web_search_backend=backend,
    ))
    assert session.web_search_enabled is False

    async def scenario() -> None:
        await session.set_web_search_enabled(True)
        assert session.web_search_enabled is True
        assert "web_search" in {tool.name for tool in session.state.tools}
        await session.set_web_search_enabled(False)

    asyncio.run(scenario())
    assert session.web_search_enabled is False
    assert backend.closed is True


def test_deepseek_uses_native_search_without_exposing_duplicate_function(monkeypatch) -> None:
    backend = _FakeBackend()
    model = Model(
        id="deepseek-v4-flash",
        api="openai-responses",
        provider="deepseek",
        context_window=16_000,
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )
    session = AgentSession(AgentSessionConfig(
        model=model,
        web_search_enabled=True,
        web_search_backend=backend,
    ))
    captured = {}

    def fake_stream(stream_model, stream_context, options):
        captured.update(model=stream_model, context=stream_context, options=options)
        return object()

    monkeypatch.setattr("agent_llm.compat.stream_simple", fake_stream)
    monkeypatch.setattr(
        "coding_agent.core.agent_session.retrying_stream",
        lambda factory, *_args, **_kwargs: factory(),
    )
    stream_fn = session._create_stream_fn()
    stream_fn(model, Context(tools=[
        Tool(name="read"),
        Tool(name="web_search"),
    ]), None)

    assert session.native_web_search_enabled is True
    assert session.web_search_backend_label == "deepseek-native"
    assert captured["options"]["web_search"] is True
    assert [tool.name for tool in captured["context"].tools] == ["read"]
    assert "Do not claim that web access is unavailable" in captured["context"].system_prompt
    session.dispose()


def test_default_search_backend_uses_dedicated_credential_resolver() -> None:
    def model_key(_provider: str) -> str:
        return "model-override-key"

    def search_key(_provider: str) -> str:
        return "zhipu-search-key"

    session = AgentSession(AgentSessionConfig(
        model=_model(),
        get_api_key=model_key,
        web_search_enabled=True,
        web_search_credential_resolver=search_key,
    ))
    assert isinstance(session._web_search_backend, ZhipuMcpSearchBackend)
    assert session._web_search_backend._credential_resolver is search_key
    session.dispose()


@pytest.mark.parametrize("phase", ["drafting", "ready", "uncertain", "recovery_error", "memory"])
def test_native_search_cannot_bypass_plan_or_background_context_tool_policy(monkeypatch, phase) -> None:
    from coding_agent.core.plan_mode import PlanState
    model = Model(id="deepseek-v4-flash", api="openai-responses", provider="deepseek",
                  context_window=16_000, cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0))
    session = AgentSession(AgentSessionConfig(
        model=model, web_search_enabled=True, web_search_backend=_FakeBackend(),
    ))
    captured = {}
    def fake_stream(_model, context, options):
        captured.update(context=context, options=options)
        return object()
    monkeypatch.setattr("agent_llm.compat.stream_simple", fake_stream)
    monkeypatch.setattr("coding_agent.core.agent_session.retrying_stream", lambda factory, *_a, **_kw: factory())
    if phase != "memory":
        session._plan_state = PlanState(mode="default" if phase == "uncertain" else "plan", phase=phase)
        session._refresh_collaboration_runtime()
        assert "web_search" not in {tool.name for tool in session.tools}
        assert session.native_web_search_enabled is False
    # Even a stale context advertising the tool cannot escape Plan restrictions.
    context = Context(tools=None if phase == "memory" else [Tool(name="web_search")])
    session._create_stream_fn()(model, context, {"web_search": True})
    assert not (captured["options"] or {}).get("web_search")
    assert "<native_web_search>" not in (captured["context"].system_prompt or "")
    session.dispose()


def test_desktop_web_search_status_and_toggle(monkeypatch, tmp_path) -> None:
    backend = _FakeBackend()
    manager = SettingsManager(tmp_path / "settings.json")
    manager.load()
    session = AgentSession(AgentSessionConfig(
        model=_model(),
        web_search_enabled=True,
        web_search_backend=backend,
        settings_manager=manager,
    ))
    runtime = DesktopRuntime(lambda _event: None)
    runtime._session = session
    monkeypatch.setattr(
        "coding_agent.desktop.runtime._resolve_api_key_for",
        lambda _provider: "configured-test-key",
    )

    status = asyncio.run(runtime.dispatch("webSearch.status", {}))
    assert status == {
        "enabled": True,
        "backend": "zhipu-mcp",
        "available": True,
        "status": "idle",
        "errorCode": None,
    }
    disabled = asyncio.run(runtime.dispatch("webSearch.setEnabled", {"enabled": False}))
    assert disabled["enabled"] is False
    assert manager.settings.web_search_enabled is False


def test_zhipu_backend_requires_coding_plan_credential() -> None:
    backend = ZhipuMcpSearchBackend(lambda _provider: None)
    with pytest.raises(SearchBackendError) as caught:
        asyncio.run(backend.search(SearchQuery("python")))
    assert caught.value.code == "WEB_SEARCH_NOT_CONFIGURED"
