"""A small, lifecycle-safe MCP client specialized for search tools."""
from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncContextManager, AsyncIterator, Awaitable, Callable, cast

from coding_agent.search.types import SearchBackendError, SearchQuery


@dataclass(frozen=True)
class McpSearchCall:
    """Credential-free subset of an MCP tool response."""

    structured_content: Any
    text_content: tuple[str, ...]
    warnings: tuple[str, ...] = ()


@dataclass
class _WorkItem:
    query: SearchQuery
    future: asyncio.Future[McpSearchCall]


def _signal_is_set(signal: Any) -> bool:
    if signal is None:
        return False
    checker = getattr(signal, "is_set", None)
    if callable(checker):
        return bool(checker())
    return bool(getattr(signal, "aborted", False) or getattr(signal, "cancelled", False))


def _safe_error_text(value: object) -> str:
    return str(value or "").lower()[:2_000]


def map_mcp_error(exc: BaseException) -> SearchBackendError:
    """Map transport/SDK failures without exposing headers or credentials."""
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
    text = f"{type(exc).__name__} {_safe_error_text(exc)}"
    if status in {401, 403} or "unauthorized" in text or "forbidden" in text:
        return SearchBackendError(
            "WEB_SEARCH_AUTH_FAILED",
            "智谱联网检索认证失败，请检查 Coding Plan 凭据、套餐权限和剩余额度。",
        )
    if status == 429 or "rate limit" in text or "too many requests" in text:
        return SearchBackendError("WEB_SEARCH_RATE_LIMITED", "联网检索请求过于频繁，请稍后重试。")
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in text:
        return SearchBackendError("WEB_SEARCH_TIMEOUT", "联网检索请求超时。")
    if "tool" in text and ("not found" in text or "unknown" in text):
        return SearchBackendError("WEB_SEARCH_TOOL_NOT_FOUND", "MCP 服务未提供兼容的联网检索工具。")
    if isinstance(exc, SearchBackendError):
        return exc
    return SearchBackendError("WEB_SEARCH_UPSTREAM_ERROR", "联网检索服务暂时不可用。")


class McpSearchClient:
    """Own an MCP connection in one task so its context exits safely.

    MCP transports use AnyIO cancellation scopes. Opening and closing those
    scopes from different tool tasks is unsafe, so a dedicated worker owns the
    complete HTTP + MCP context lifecycle and serializes calls.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        authorization: str,
        preferred_tool: str,
        timeout_seconds: float,
        connection_factory: Callable[[], AsyncContextManager[Any]] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._authorization = authorization
        self.preferred_tool = preferred_tool
        self.timeout_seconds = timeout_seconds
        self._connection_factory = connection_factory
        self._state_lock: asyncio.Lock | None = None
        self._queue: asyncio.Queue[_WorkItem | None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._closed = False

    async def call(self, query: SearchQuery, *, signal: Any = None) -> McpSearchCall:
        if self._closed:
            raise SearchBackendError("WEB_SEARCH_ABORTED", "联网检索客户端已关闭。")
        if _signal_is_set(signal):
            raise SearchBackendError("WEB_SEARCH_ABORTED", "联网检索已被用户中断。")
        await self._ensure_worker()
        assert self._queue is not None
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(_WorkItem(query=query, future=future))

        signal_wait = getattr(signal, "wait", None)
        signal_task: asyncio.Future[Any] | None = None
        if callable(signal_wait):
            signal_task = asyncio.ensure_future(cast(Awaitable[Any], signal_wait()))
        try:
            if signal_task is None:
                return await future
            done, _ = await asyncio.wait(
                {future, signal_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            if signal_task in done and _signal_is_set(signal):
                future.cancel()
                await self._reset_worker()
                raise SearchBackendError("WEB_SEARCH_ABORTED", "联网检索已被用户中断。")
            return await future
        finally:
            if signal_task is not None:
                signal_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await signal_task

    async def aclose(self) -> None:
        self._closed = True
        worker = self._worker
        queue = self._queue
        if worker is not None:
            if not worker.done() and queue is not None:
                await queue.put(None)
            try:
                await asyncio.wait_for(worker, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                if not worker.done():
                    worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
        self._worker = None
        self._queue = None
        self._ready = None

    async def _ensure_worker(self) -> None:
        if self._state_lock is None:
            self._state_lock = asyncio.Lock()
        async with self._state_lock:
            if self._worker is None or self._worker.done():
                self._queue = asyncio.Queue()
                self._ready = asyncio.get_running_loop().create_future()
                self._worker = asyncio.create_task(self._run_worker())
            ready = self._ready
        assert ready is not None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await asyncio.shield(ready)
        except TimeoutError as exc:
            await self._reset_worker()
            raise SearchBackendError("WEB_SEARCH_TIMEOUT", "连接联网检索服务超时。") from exc

    async def _reset_worker(self) -> None:
        worker = self._worker
        if worker is not None:
            if not worker.done():
                worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        self._worker = None
        self._queue = None
        self._ready = None

    async def _run_worker(self) -> None:
        assert self._queue is not None
        assert self._ready is not None
        queue = self._queue
        ready = self._ready
        current: _WorkItem | None = None
        try:
            async with self._connect() as client:
                tools = await client.list_tools()
                if not ready.done():
                    ready.set_result(None)
                while True:
                    current = await queue.get()
                    if current is None:
                        return
                    try:
                        tool = self._select_tool(tools.tools)
                        arguments, warnings = self._build_arguments(tool, current.query)
                        async with asyncio.timeout(self.timeout_seconds):
                            result = await client.call_tool(tool.name, arguments)
                        if getattr(result, "is_error", False):
                            error_text = " ".join(self._text_blocks(result))
                            if "tool" in error_text.lower() and (
                                "not found" in error_text.lower() or "unknown" in error_text.lower()
                            ):
                                tools = await client.list_tools()
                                tool = self._select_tool(tools.tools)
                                arguments, warnings = self._build_arguments(tool, current.query)
                                async with asyncio.timeout(self.timeout_seconds):
                                    result = await client.call_tool(tool.name, arguments)
                            if getattr(result, "is_error", False):
                                raise map_mcp_error(RuntimeError("MCP tool error " + error_text))
                        if not current.future.done():
                            current.future.set_result(McpSearchCall(
                                structured_content=getattr(result, "structured_content", None),
                                text_content=self._text_blocks(result),
                                warnings=warnings,
                            ))
                    except BaseException as exc:
                        if isinstance(exc, asyncio.CancelledError):
                            raise
                        if not current.future.done():
                            current.future.set_exception(map_mcp_error(exc))
                    finally:
                        current = None
        except BaseException as exc:
            if not ready.done():
                if isinstance(exc, asyncio.CancelledError):
                    ready.cancel()
                else:
                    ready.set_exception(map_mcp_error(exc))
            if current is not None and not current.future.done():
                current.future.set_exception(
                    SearchBackendError("WEB_SEARCH_ABORTED", "联网检索连接已关闭。")
                    if isinstance(exc, asyncio.CancelledError)
                    else map_mcp_error(exc)
                )
            self._fail_queued(queue, exc)
            if isinstance(exc, asyncio.CancelledError):
                raise

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[Any]:
        if self._connection_factory is not None:
            async with self._connection_factory() as client:
                yield client
            return

        # MCP v2 requires HTTP-shaped options on the caller-owned client.
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {self._authorization}"},
            timeout=httpx2.Timeout(self.timeout_seconds),
            follow_redirects=True,
        ) as http_client:
            transport = streamable_http_client(self.endpoint, http_client=http_client)
            async with Client(transport) as client:
                yield client

    def _select_tool(self, tools: list[Any]) -> Any:
        exact = next((tool for tool in tools if getattr(tool, "name", "") == self.preferred_tool), None)
        if exact is not None:
            return exact
        folded = self.preferred_tool.casefold()
        insensitive = next(
            (tool for tool in tools if str(getattr(tool, "name", "")).casefold() == folded), None,
        )
        if insensitive is not None:
            return insensitive
        compatible = [
            tool for tool in tools
            if "search" in str(getattr(tool, "name", "")).casefold()
            and "web" in str(getattr(tool, "name", "")).casefold()
        ]
        if len(compatible) == 1:
            return compatible[0]
        raise SearchBackendError("WEB_SEARCH_TOOL_NOT_FOUND", "MCP 服务未提供兼容的联网检索工具。")

    @staticmethod
    def _build_arguments(tool: Any, query: SearchQuery) -> tuple[dict[str, Any], tuple[str, ...]]:
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        arguments: dict[str, Any] = {}
        warnings: list[str] = []

        query_name = McpSearchClient._first_property(
            properties, ("query", "search_query", "searchQuery", "q", "keywords"),
        )
        if query_name is None:
            string_fields = [
                name for name, value in properties.items()
                if isinstance(value, dict) and value.get("type") == "string"
            ]
            if len(string_fields) == 1:
                query_name = string_fields[0]
        if query_name is None:
            raise SearchBackendError(
                "WEB_SEARCH_TOOL_NOT_FOUND", "联网检索工具的输入 Schema 不包含可识别的查询字段。",
            )
        arguments[query_name] = query.query

        count_name = McpSearchClient._first_property(
            properties, ("count", "limit", "num_results", "numResults", "max_results", "maxResults"),
        )
        if count_name:
            arguments[count_name] = query.count

        freshness_name = McpSearchClient._first_property(
            properties, ("freshness", "recency", "time_range", "timeRange"),
        )
        if query.freshness != "all":
            if freshness_name:
                spec = properties.get(freshness_name, {})
                allowed = spec.get("enum") if isinstance(spec, dict) else None
                if not allowed or query.freshness in allowed:
                    arguments[freshness_name] = query.freshness
                else:
                    warnings.append("The selected backend ignored the unsupported freshness filter.")
            else:
                warnings.append("The selected backend ignored the unsupported freshness filter.")

        domains_name = McpSearchClient._first_property(
            properties, ("domains", "domain", "site", "sites", "domain_filter", "domainFilter"),
        )
        if query.domains:
            if domains_name:
                spec = properties.get(domains_name, {})
                arguments[domains_name] = (
                    list(query.domains) if isinstance(spec, dict) and spec.get("type") == "array"
                    else ",".join(query.domains)
                )
            else:
                # Preserve filter semantics even when the provider only exposes a query string.
                arguments[query_name] = f"{query.query} " + " OR ".join(
                    f"site:{domain}" for domain in query.domains
                )
                warnings.append("Domain filters were translated into site: query operators.")
        return arguments, tuple(warnings)

    @staticmethod
    def _first_property(properties: dict[str, Any], candidates: tuple[str, ...]) -> str | None:
        return next((name for name in candidates if name in properties), None)

    @staticmethod
    def _text_blocks(result: Any) -> tuple[str, ...]:
        values: list[str] = []
        for block in getattr(result, "content", ()) or ():
            text = getattr(block, "text", None)
            if isinstance(text, str):
                values.append(text)
        return tuple(values)

    @staticmethod
    def _fail_queued(queue: asyncio.Queue[_WorkItem | None], exc: BaseException) -> None:
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if item is not None and not item.future.done():
                item.future.set_exception(
                    SearchBackendError("WEB_SEARCH_ABORTED", "联网检索连接已关闭。")
                    if isinstance(exc, asyncio.CancelledError)
                    else map_mcp_error(exc)
                )


McpClientFactory = Callable[[str], McpSearchClient]

__all__ = ["McpSearchCall", "McpSearchClient", "map_mcp_error"]
