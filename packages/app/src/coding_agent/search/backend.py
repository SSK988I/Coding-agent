"""Backend protocol for model-independent web search."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from coding_agent.search.types import SearchQuery, SearchResponse


@runtime_checkable
class SearchBackend(Protocol):
    """A service capable of resolving a normalized search query."""

    name: str

    async def search(self, query: SearchQuery, *, signal: Any = None) -> SearchResponse: ...

    async def aclose(self) -> None: ...


__all__ = ["SearchBackend"]
