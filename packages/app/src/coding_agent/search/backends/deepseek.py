"""DeepSeek's standalone, text-only Search API (not a model conversation)."""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from coding_agent.search.formatter import normalize_results
from coding_agent.search.types import SearchBackendError, SearchQuery, SearchResponse, SearchResult

DEEPSEEK_SEARCH_ENDPOINT = "https://api.deepseek.com/v1/tools/search"
CredentialResolver = Callable[[str], str | None | Awaitable[str | None]]
_FRESHNESS_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


def _aborted(signal: Any) -> bool:
    checker = getattr(signal, "is_set", None)
    if callable(checker):
        return bool(checker())
    return bool(getattr(signal, "aborted", False) or getattr(signal, "cancelled", False))


class DeepSeekSearchBackend:
    """Resolve each approved query with a fresh credential and HTTP client.

    No SDK, model request, history, image, or application tool is sent. HTTPX
    does not retry by default; redirects are disabled to protect the key. The
    caller is responsible for authorizing every invocation before ``search``.
    """

    name = "deepseek-search"

    def __init__(
        self,
        credential_resolver: CredentialResolver | None,
        *,
        endpoint: str = DEEPSEEK_SEARCH_ENDPOINT,
        timeout_seconds: float = 30.0,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        self._credential_resolver = credential_resolver
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self._client_factory = client_factory
        self._active: set[asyncio.Task[SearchResponse]] = set()
        self._closed = False

    async def search(self, query: SearchQuery, *, signal: Any = None) -> SearchResponse:
        if self._closed or _aborted(signal):
            raise SearchBackendError("WEB_SEARCH_ABORTED", "联网检索已被中断。")
        # Validate before looking up credentials or opening an HTTP client.
        search_text = self._search_text(query)
        worker = asyncio.create_task(self._perform(query, search_text))
        self._active.add(worker)
        watcher = asyncio.create_task(self._wait_for_abort(signal)) if signal is not None else None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                if watcher is not None:
                    done, _ = await asyncio.wait({worker, watcher}, return_when=asyncio.FIRST_COMPLETED)
                    if watcher in done:
                        raise SearchBackendError("WEB_SEARCH_ABORTED", "联网检索已被用户中断。")
                return await worker
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            raise SearchBackendError("WEB_SEARCH_ABORTED", "联网检索已被中断。") from None
        except (TimeoutError, httpx.TimeoutException):
            raise SearchBackendError("WEB_SEARCH_TIMEOUT", "DeepSeek 联网检索请求超时。") from None
        finally:
            worker.cancel()
            if watcher is not None:
                watcher.cancel()
            await asyncio.gather(worker, *([watcher] if watcher is not None else []), return_exceptions=True)
            self._active.discard(worker)

    async def aclose(self) -> None:
        self._closed = True
        self.request_stop()
        await asyncio.gather(*tuple(self._active), return_exceptions=True)

    def request_stop(self) -> None:
        for task in tuple(self._active):
            task.cancel()

    @staticmethod
    async def _wait_for_abort(signal: Any) -> None:
        # Both asyncio.Event and the Agent's synchronous abort signals work.
        # Polling also covers a signal that has no awaitable wait() method.
        while not _aborted(signal):
            await asyncio.sleep(0.025)

    @staticmethod
    def _search_text(query: SearchQuery) -> str:
        clauses = [query.query]
        if query.domains:
            clauses.append(" OR ".join(f"site:{domain}" for domain in query.domains))
        if query.freshness != "all":
            cutoff = datetime.now(timezone.utc) - timedelta(days=_FRESHNESS_DAYS[query.freshness])
            clauses.append(f"after:{cutoff.date().isoformat()}")
        text = " ".join(clauses)
        if len(text) > 128:
            raise SearchBackendError(
                "WEB_SEARCH_INVALID_QUERY",
                "DeepSeek 搜索关键词与域名、时间筛选合计不能超过 128 个字符，请缩短关键词或减少筛选条件。",
            )
        return text

    async def _perform(self, query: SearchQuery, search_text: str) -> SearchResponse:
        try:
            resolver = self._credential_resolver
            credential = resolver("deepseek") if resolver is not None else None
            if inspect.isawaitable(credential):
                credential = await credential
            if not isinstance(credential, str) or not credential.strip():
                raise SearchBackendError(
                    "WEB_SEARCH_NOT_CONFIGURED", "未找到 DeepSeek API 凭据，请先配置 DeepSeek 后再使用联网检索。",
                )
            # A task that owns the client also closes it on HTTP/auth aborts.
            async with self._client_factory(
                timeout=self.timeout_seconds, follow_redirects=False,
            ) as client:
                response = await client.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {credential}"},
                    json={"query": search_text, "count": query.count, "need_content": False},
                )
                self._check_status(response.status_code)
                try:
                    payload = response.json()
                except (ValueError, UnicodeError):
                    raise SearchBackendError(
                        "WEB_SEARCH_INVALID_RESPONSE", "DeepSeek 搜索服务返回了无法识别的结果格式。",
                    ) from None
                return self._normalize_response(payload, query, response.headers.get("x-request-id"))
        except SearchBackendError:
            raise
        except httpx.TimeoutException:
            raise SearchBackendError("WEB_SEARCH_TIMEOUT", "DeepSeek 联网检索请求超时。") from None
        except Exception:
            # Do not echo provider exception bodies, queries, keys, or headers.
            raise SearchBackendError("WEB_SEARCH_UPSTREAM_ERROR", "DeepSeek 联网检索服务暂时不可用。") from None

    @staticmethod
    def _check_status(status: int) -> None:
        if status == 200:
            return
        if status in {401, 403}:
            raise SearchBackendError("WEB_SEARCH_AUTH_FAILED", "DeepSeek 联网检索认证失败，请检查 API 凭据和账户权限。")
        if status == 402:
            raise SearchBackendError("WEB_SEARCH_AUTH_FAILED", "DeepSeek 账户余额不足，请充值后再使用联网检索。")
        if status == 429:
            raise SearchBackendError("WEB_SEARCH_RATE_LIMITED", "DeepSeek 联网检索请求过于频繁，请稍后重试。")
        if status in {408, 504}:
            raise SearchBackendError("WEB_SEARCH_TIMEOUT", "DeepSeek 联网检索请求超时。")
        if status == 400:
            raise SearchBackendError("WEB_SEARCH_INVALID_QUERY", "DeepSeek 搜索服务拒绝了请求，请检查关键词和筛选条件。")
        raise SearchBackendError("WEB_SEARCH_UPSTREAM_ERROR", "DeepSeek 联网检索服务暂时不可用。")

    def _normalize_response(self, payload: Any, query: SearchQuery, request_id: str | None) -> SearchResponse:
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise SearchBackendError("WEB_SEARCH_INVALID_RESPONSE", "DeepSeek 搜索服务返回了无法识别的结果格式。")
        results: list[SearchResult] = []
        malformed = False
        for record in payload["results"][:200]:
            if not isinstance(record, dict) or not all(isinstance(record.get(key), str) for key in ("url", "title", "snippet")):
                malformed = True
                continue
            summary = record.get("summary")
            results.append(SearchResult(
                title=record["title"], url=record["url"],
                snippet=summary if isinstance(summary, str) and summary else record["snippet"],
                source=record.get("site_name") if isinstance(record.get("site_name"), str) else None,
                published_at=record.get("date") if isinstance(record.get("date"), str) else None,
            ))
        if malformed and not results:
            raise SearchBackendError("WEB_SEARCH_INVALID_RESPONSE", "DeepSeek 搜索服务返回了无法识别的结果格式。")
        normalized = normalize_results(results, 200)
        if query.domains:
            normalized = tuple(result for result in normalized if self._domain_allowed(result.url, query.domains))
        warnings = []
        if malformed:
            warnings.append("Some malformed DeepSeek search results were omitted.")
        if query.freshness != "all":
            warnings.append("freshness uses DeepSeek's after: UTC calendar-date filter, not an exact rolling-hour guarantee.")
        return SearchResponse(
            query=query.query, backend=self.name, results=normalized[:query.count],
            request_id=request_id, warnings=tuple(warnings),
        )

    @staticmethod
    def _domain_allowed(url: str, domains: tuple[str, ...]) -> bool:
        hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
        return any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains)


__all__ = ["DEEPSEEK_SEARCH_ENDPOINT", "DeepSeekSearchBackend"]
