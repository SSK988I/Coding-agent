"""DeepSeek's raw Search API uses only text queries and one authorized HTTP call."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from coding_agent.search.backends.deepseek import DeepSeekSearchBackend
from coding_agent.search.types import SearchBackendError, SearchQuery


def source(url="https://example.org/one", title="One", snippet="Source snippet.", **kwargs):
    return {"url": url, "title": title, "snippet": snippet, **kwargs}


def backend_with_transport(handler, *, resolver=None, timeout=1):
    clients, options = [], []

    def factory(**kwargs):
        options.append(kwargs.copy())
        client = httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(handler), trust_env=False)
        clients.append(client)
        return client

    backend = DeepSeekSearchBackend(
        resolver if resolver is not None else lambda provider: "local-test-key",
        timeout_seconds=timeout, client_factory=factory,
    )
    return backend, clients, options


def test_request_is_text_only_direct_search_and_sources_are_normalized():
    async def scenario():
        requests, providers = [], []

        def handle(request):
            requests.append((request, json.loads(request.content)))
            return httpx.Response(200, headers={"x-request-id": "request-local"}, json={"results": [
                source("https://example.org/one#section", " One ", site_name="Example", date="2026-10-17T00:00:00Z"),
                source("https://example.org/one", "duplicate"),
                source("javascript:alert(1)", "bad"),
            ], "text": '{"url":"https://invented.example/"}'})

        def resolve(provider):
            providers.append(provider)
            return "local-test-key"

        backend, clients, options = backend_with_transport(handle, resolver=resolve)
        try:
            result = await backend.search(SearchQuery("public landmark", count=3))
            assert result.backend == "deepseek-search"
            assert result.request_id == "request-local"
            assert [(r.title, r.url, r.snippet, r.source, r.published_at) for r in result.results] == [
                ("One", "https://example.org/one", "Source snippet.", "Example", "2026-10-17T00:00:00Z"),
            ]
            assert providers == ["deepseek"]
            request, body = requests[0]
            assert request.method == "POST" and request.url == "https://api.deepseek.com/v1/tools/search"
            assert request.headers["authorization"] == "Bearer local-test-key"
            assert request.headers["content-type"] == "application/json"
            assert options[0]["follow_redirects"] is False
            assert body == {"query": "public landmark", "count": 3, "need_content": False}
            assert all(client.is_closed for client in clients)
        finally:
            await backend.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [
    None, [], {}, {"results": None}, {"results": {}}, {"results": "https://invented.example/"},
    {"results": ["https://invented.example/"]}, {"results": [{"url": "https://invented.example/"}]},
    {"output_text": '{"results":[{"url":"https://invented.example/"}]}'},
])
def test_result_schema_is_required_and_generated_text_is_never_parsed(payload):
    async def scenario():
        backend, _, _ = backend_with_transport(lambda _: httpx.Response(200, json=payload))
        with pytest.raises(SearchBackendError, match="WEB_SEARCH_INVALID_RESPONSE"):
            await backend.search(SearchQuery("public landmark"))
        await backend.aclose()
    asyncio.run(scenario())


def test_empty_results_are_valid_and_summary_is_bounded_by_common_normalizer():
    async def scenario():
        payloads = [
            {"results": []},
            {"results": [source(summary="Longer summary"), "bad", source("https://example.org/two", summary="x" * 5000)]},
        ]
        backend, _, _ = backend_with_transport(lambda _: httpx.Response(200, json=payloads.pop(0)))
        assert (await backend.search(SearchQuery("public landmark"))).results == ()
        result = await backend.search(SearchQuery("public landmark"))
        assert result.results[0].snippet == "Longer summary"
        assert len(result.results[1].snippet) == 2000
        assert result.warnings == ("Some malformed DeepSeek search results were omitted.",)
        await backend.aclose()
    asyncio.run(scenario())


def test_count_domains_and_documented_date_operators_are_applied():
    async def scenario():
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"results": [source(url) for url in (
                "https://unrelated.org/page", "https://sub.example.org/1", "https://example.org/2",
                "https://example.org.evil.test/3", "https://example.org/4",
            )]})

        backend, _, _ = backend_with_transport(handle)
        result = await backend.search(SearchQuery("public landmark", count=2, domains=("example.org", "docs.org"), freshness="day"))
        body = requests[0]
        assert body["query"].startswith("public landmark site:example.org OR site:docs.org after:")
        cutoff = body["query"].rsplit("after:", 1)[1]
        assert (datetime.now(timezone.utc).date() - datetime.fromisoformat(cutoff).date()).days in {1, 2}
        assert body["count"] == 2
        assert [r.url for r in result.results] == ["https://sub.example.org/1", "https://example.org/2"]
        assert any("freshness" in warning for warning in result.warnings)
        await backend.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("query", [SearchQuery("x" * 129), SearchQuery("x" * 120, domains=("example.org",)),
                                       SearchQuery("x" * 120, freshness="day")])
def test_query_limit_includes_operators_and_rejects_before_auth_and_http(query):
    async def scenario():
        backend, clients, _ = backend_with_transport(
            lambda _: pytest.fail("No HTTP for invalid query"), resolver=lambda _: pytest.fail("Must validate before auth"),
        )
        with pytest.raises(SearchBackendError, match="WEB_SEARCH_INVALID_QUERY"):
            await backend.search(query)
        assert not clients
        await backend.aclose()
    asyncio.run(scenario())


def test_unicode_query_limit_is_characters_not_utf8_bytes():
    async def scenario():
        requests = []
        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"results": []})
        backend, _, _ = backend_with_transport(handle)
        await backend.search(SearchQuery("图" * 128))
        assert requests[0]["query"] == "图" * 128
        await backend.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure,code", [
    (400, "WEB_SEARCH_INVALID_QUERY"), (401, "WEB_SEARCH_AUTH_FAILED"), (402, "WEB_SEARCH_AUTH_FAILED"),
    (403, "WEB_SEARCH_AUTH_FAILED"), (408, "WEB_SEARCH_TIMEOUT"), (504, "WEB_SEARCH_TIMEOUT"),
    (429, "WEB_SEARCH_RATE_LIMITED"), (503, "WEB_SEARCH_UPSTREAM_ERROR"),
    ("timeout", "WEB_SEARCH_TIMEOUT"), ("connection", "WEB_SEARCH_UPSTREAM_ERROR"),
])
def test_http_has_no_hidden_retries_and_errors_are_sanitized(failure, code):
    async def scenario():
        requests = []

        def handle(request):
            requests.append(request)
            if failure == "timeout":
                raise httpx.ReadTimeout("secret-key-and-query", request=request)
            if failure == "connection":
                raise httpx.ConnectError("secret-key-and-query", request=request)
            return httpx.Response(failure, json={"error": {"message": "secret-key-and-query"}})

        backend, clients, _ = backend_with_transport(handle)
        try:
            with pytest.raises(SearchBackendError) as error:
                await backend.search(SearchQuery("public landmark"))
            assert error.value.code == code
            assert "secret-key-and-query" not in str(error.value)
            assert len(requests) == 1
            assert all(client.is_closed for client in clients)
        finally:
            await backend.aclose()
    asyncio.run(scenario())


def test_redirects_are_not_followed_and_invalid_json_is_sanitized():
    async def scenario():
        requests = []
        def handle(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(307, headers={"location": "https://untrusted.example/"})
            return httpx.Response(200, content=b"secret-upstream-data")
        backend, clients, _ = backend_with_transport(handle)
        with pytest.raises(SearchBackendError, match="WEB_SEARCH_UPSTREAM_ERROR"):
            await backend.search(SearchQuery("public landmark"))
        assert len(requests) == 1
        with pytest.raises(SearchBackendError, match="WEB_SEARCH_INVALID_RESPONSE") as error:
            await backend.search(SearchQuery("public landmark"))
        assert "secret-upstream-data" not in str(error.value)
        assert all(client.is_closed for client in clients)
        await backend.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("credential", [None, "", "   ", 123])
def test_missing_credentials_do_not_open_http(credential):
    async def scenario():
        backend, clients, _ = backend_with_transport(
            lambda _: pytest.fail("No HTTP without credentials"), resolver=lambda _: credential,
        )
        with pytest.raises(SearchBackendError, match="WEB_SEARCH_NOT_CONFIGURED"):
            await backend.search(SearchQuery("public landmark"))
        assert not clients
        await backend.aclose()
    asyncio.run(scenario())


def test_credentials_are_resolved_per_call_and_clients_not_retained():
    async def scenario():
        keys = iter(["first-key", "second-key"])
        headers = []

        def handle(request):
            headers.append(request.headers["authorization"])
            return httpx.Response(200, json={"results": []})

        backend, clients, _ = backend_with_transport(handle, resolver=lambda _: next(keys))
        await backend.search(SearchQuery("public landmark"))
        await backend.search(SearchQuery("public landmark"))
        assert headers == ["Bearer first-key", "Bearer second-key"]
        assert len(clients) == 2 and all(client.is_closed for client in clients)
        assert not backend._active
        await backend.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["credentials", "http"])
@pytest.mark.parametrize("stop", ["signal", "request_stop", "aclose", "task_cancel", "timeout"])
def test_cancellation_covers_authentication_and_active_http(stage, stop):
    async def scenario():
        entered, cancelled, signal = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def block():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def resolver(_provider):
            if stage == "credentials":
                await block()
            return "local-test-key"

        async def handle(_request):
            await block()
            return httpx.Response(200, json={"results": []})

        backend, clients, _ = backend_with_transport(handle, resolver=resolver, timeout=0.08 if stop == "timeout" else 2)
        task = asyncio.create_task(backend.search(SearchQuery("public landmark"), signal=signal))
        await asyncio.wait_for(entered.wait(), 1)
        if stop == "signal":
            signal.set()
        elif stop == "request_stop":
            backend.request_stop()
        elif stop == "aclose":
            await backend.aclose()
        elif stop == "task_cancel":
            task.cancel()
        if stop == "task_cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(SearchBackendError) as error:
                await asyncio.wait_for(task, 1)
            assert error.value.code == ("WEB_SEARCH_TIMEOUT" if stop == "timeout" else "WEB_SEARCH_ABORTED")
        assert cancelled.is_set()
        assert all(client.is_closed for client in clients)
        assert not backend._active
        await backend.aclose()
    asyncio.run(scenario())


def test_already_aborted_signal_does_not_resolve_credentials_or_open_http():
    async def scenario():
        signal = asyncio.Event()
        signal.set()
        backend, clients, _ = backend_with_transport(
            lambda _: pytest.fail("Must not open HTTP"), resolver=lambda _: pytest.fail("Must not resolve credentials"),
        )
        with pytest.raises(SearchBackendError, match="WEB_SEARCH_ABORTED"):
            await backend.search(SearchQuery("public landmark"), signal=signal)
        assert not clients
        await backend.aclose()
    asyncio.run(scenario())
