"""Model-independent web search tool and backend interfaces."""

from coding_agent.search.backend import SearchBackend
from coding_agent.search.tool import WebSearchTool
from coding_agent.search.types import (
    SearchBackendError,
    SearchQuery,
    SearchResponse,
    SearchResult,
)

__all__ = [
    "SearchBackend",
    "SearchBackendError",
    "SearchQuery",
    "SearchResponse",
    "SearchResult",
    "WebSearchTool",
]
