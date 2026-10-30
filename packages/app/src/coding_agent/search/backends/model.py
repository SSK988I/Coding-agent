"""Select the application's search service without changing the chat model."""
from __future__ import annotations

from typing import Any, Callable

from coding_agent.search.backend import SearchBackend
from coding_agent.search.backends.deepseek import DeepSeekSearchBackend
from coding_agent.search.backends.zhipu import ZhipuMcpSearchBackend
from coding_agent.search.types import SearchBackendError, SearchQuery, SearchResponse


class ModelSearchBackend:
    """Resolve the current provider at call time, including after model switches."""

    def __init__(
        self,
        model_provider: Callable[[], str],
        credential_resolver: Any,
        *,
        fallback_name: str = "zhipu-mcp",
        timeout_seconds: float = 30.0,
    ) -> None:
        if fallback_name != "zhipu-mcp":
            raise ValueError(f"Unsupported web search backend: {fallback_name}")
        self._model_provider = model_provider
        self._credential_resolver = credential_resolver
        self._timeout_seconds = timeout_seconds
        self._backends: dict[str, SearchBackend] = {}
        self._closed = False

    @property
    def name(self) -> str:
        return "deepseek-search" if self._model_provider() == "deepseek" else "zhipu-mcp"

    def _selected_backend(self) -> SearchBackend:
        if self._closed:
            raise SearchBackendError("WEB_SEARCH_ABORTED", "联网检索已被中断。")
        name = self.name
        if name not in self._backends:
            factory = DeepSeekSearchBackend if name == "deepseek-search" else ZhipuMcpSearchBackend
            self._backends[name] = factory(
                self._credential_resolver, timeout_seconds=self._timeout_seconds,
            )
        return self._backends[name]

    async def search(self, query: SearchQuery, *, signal: Any = None) -> SearchResponse:
        # A selected backend owns only this explicit query, never chat history
        # or attached images. Approval/Plan checks happen before this tool runs.
        return await self._selected_backend().search(query, signal=signal)

    def request_stop(self) -> None:
        for backend in self._backends.values():
            stop = getattr(backend, "request_stop", None)
            if stop is not None:
                stop()

    async def aclose(self) -> None:
        self._closed = True
        backends, self._backends = self._backends, {}
        for backend in backends.values():
            await backend.aclose()
