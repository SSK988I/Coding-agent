"""Zhipu Web Search Prime Remote MCP backend."""
from __future__ import annotations

import inspect
import hashlib
import json
import re
from typing import Any, Awaitable, Callable

from coding_agent.search.backends.mcp import McpSearchCall, McpSearchClient
from coding_agent.search.formatter import normalize_results
from coding_agent.search.types import SearchBackendError, SearchQuery, SearchResponse, SearchResult

ZHIPU_SEARCH_ENDPOINT = "https://open.bigmodel.cn/api/mcp/web_search_prime/mcp"
ZHIPU_SEARCH_TOOL = "webSearchPrime"
CredentialResolver = Callable[[str], str | None | Awaitable[str | None]]

_RESULT_LIST_KEYS = (
    "search_result", "search_results", "searchResult", "searchResults",
    "results", "items", "data", "web_results", "webResults",
)
_URL_KEYS = ("url", "link", "href")
_TITLE_KEYS = ("title", "name")
_SNIPPET_KEYS = ("snippet", "content", "description", "summary", "text")
_SOURCE_KEYS = ("source", "media", "site", "site_name", "siteName")
_DATE_KEYS = ("published_at", "publishedAt", "publish_date", "publishDate", "date")
_REQUEST_ID_KEYS = ("request_id", "requestId", "id")


class ZhipuMcpSearchBackend:
    name = "zhipu-mcp"

    def __init__(
        self,
        credential_resolver: CredentialResolver | None,
        *,
        endpoint: str = ZHIPU_SEARCH_ENDPOINT,
        timeout_seconds: float = 30.0,
        client_factory: Callable[..., McpSearchClient] = McpSearchClient,
    ) -> None:
        self._credential_resolver = credential_resolver
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self._client_factory = client_factory
        self._client: McpSearchClient | None = None
        self._credential_marker: str | None = None
        self._client_lock: Any = None

    async def search(self, query: SearchQuery, *, signal: Any = None) -> SearchResponse:
        client = await self._get_client()
        call = await client.call(query, signal=signal)
        response = self._normalize_response(call, query)
        return SearchResponse(
            query=response.query,
            results=normalize_results(response.results, query.count),
            backend=response.backend,
            request_id=response.request_id,
            warnings=response.warnings,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None
        self._credential_marker = None

    def request_stop(self) -> None:
        """Synchronous lifecycle hint; authoritative cleanup happens in ``aclose``."""
        worker = getattr(self._client, "_worker", None)
        if worker is not None and not worker.done():
            worker.cancel()

    async def _get_client(self) -> McpSearchClient:
        import asyncio

        if self._client_lock is None:
            self._client_lock = asyncio.Lock()
        async with self._client_lock:
            resolver = self._credential_resolver
            credential = resolver("zai-coding-cn") if resolver is not None else None
            if inspect.isawaitable(credential):
                credential = await credential
            if not credential or not isinstance(credential, str):
                raise SearchBackendError(
                    "WEB_SEARCH_NOT_CONFIGURED",
                    "未找到智谱 Coding Plan 凭据，请先登录 zai-coding-cn 后再使用联网检索。",
                )
            marker = hashlib.sha256(credential.encode("utf-8")).hexdigest()
            if self._client is not None and marker != self._credential_marker:
                await self._client.aclose()
                self._client = None
            if self._client is None:
                self._client = self._client_factory(
                    endpoint=self.endpoint,
                    authorization=credential,
                    preferred_tool=ZHIPU_SEARCH_TOOL,
                    timeout_seconds=self.timeout_seconds,
                )
                self._credential_marker = marker
            return self._client

    def _normalize_response(self, call: McpSearchCall, query: SearchQuery) -> SearchResponse:
        payloads: list[Any] = []
        if call.structured_content is not None:
            payloads.append(call.structured_content)
        for text in call.text_content:
            parsed = self._parse_json_text(text)
            if parsed is not None:
                payloads.append(parsed)

        results: list[SearchResult] = []
        request_id: str | None = None
        for payload in payloads:
            if request_id is None:
                request_id = self._find_scalar(payload, _REQUEST_ID_KEYS)
            for item in self._candidate_records(payload):
                url = self._first(item, _URL_KEYS)
                if url is None:
                    continue
                results.append(SearchResult(
                    title=str(self._first(item, _TITLE_KEYS) or url),
                    url=str(url),
                    snippet=str(self._first(item, _SNIPPET_KEYS) or ""),
                    source=self._optional_string(self._first(item, _SOURCE_KEYS)),
                    published_at=self._optional_string(self._first(item, _DATE_KEYS)),
                ))

        if not results:
            for text in call.text_content:
                results.extend(self._parse_markdown_results(text))
        if not results and any(text.strip() for text in call.text_content):
            raise SearchBackendError(
                "WEB_SEARCH_INVALID_RESPONSE", "联网检索服务返回了无法识别的结果格式。",
            )
        return SearchResponse(
            query=query.query,
            results=tuple(results),
            backend=self.name,
            request_id=request_id,
            warnings=call.warnings,
        )

    @classmethod
    def _candidate_records(cls, value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            records: list[dict[str, Any]] = []
            for item in value:
                records.extend(cls._candidate_records(item))
            return records
        if not isinstance(value, dict):
            return []
        if cls._first(value, _URL_KEYS) is not None:
            return [value]
        for key in _RESULT_LIST_KEYS:
            if key in value:
                records = cls._candidate_records(value[key])
                if records:
                    return records
        records = []
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                records.extend(cls._candidate_records(nested))
        return records

    @staticmethod
    def _parse_json_text(text: str) -> Any | None:
        value = text.strip()
        if value.startswith("```"):
            value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE)
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _parse_markdown_results(text: str) -> list[SearchResult]:
        results = []
        for title, url in re.findall(r"\[([^\]]{1,300})\]\((https?://[^\s)]+)\)", text):
            results.append(SearchResult(title=title, url=url, snippet=""))
        if results:
            return results
        for url in re.findall(r"https?://[^\s<>\])}]+", text):
            results.append(SearchResult(title=url, url=url, snippet=""))
        return results

    @classmethod
    def _find_scalar(cls, value: Any, keys: tuple[str, ...]) -> str | None:
        if isinstance(value, dict):
            found = cls._first(value, keys)
            if found is not None and not isinstance(found, (dict, list)):
                return str(found)
            for nested in value.values():
                result = cls._find_scalar(nested, keys)
                if result is not None:
                    return result
        elif isinstance(value, list):
            for nested in value:
                result = cls._find_scalar(nested, keys)
                if result is not None:
                    return result
        return None

    @staticmethod
    def _first(value: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            item = value.get(key)
            if item is not None and item != "":
                return item
        return None

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        return str(value) if value not in {None, ""} else None


__all__ = ["ZHIPU_SEARCH_ENDPOINT", "ZHIPU_SEARCH_TOOL", "ZhipuMcpSearchBackend"]
