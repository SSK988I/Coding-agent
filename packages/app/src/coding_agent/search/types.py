"""Normalized query, result, response, and error types for web search."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

SearchFreshness = Literal["all", "day", "week", "month", "year"]
SearchErrorCode = Literal[
    "WEB_SEARCH_DISABLED",
    "WEB_SEARCH_NOT_CONFIGURED",
    "WEB_SEARCH_AUTH_FAILED",
    "WEB_SEARCH_TIMEOUT",
    "WEB_SEARCH_RATE_LIMITED",
    "WEB_SEARCH_TOOL_NOT_FOUND",
    "WEB_SEARCH_INVALID_RESPONSE",
    "WEB_SEARCH_UPSTREAM_ERROR",
    "WEB_SEARCH_ABORTED",
]

_FRESHNESS_VALUES = {"all", "day", "week", "month", "year"}


class SearchBackendError(RuntimeError):
    """A user-safe search failure with a stable machine-readable code."""

    def __init__(self, code: SearchErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code

    def __str__(self) -> str:
        return f"{self.code}: {super().__str__()}"


@dataclass(frozen=True)
class SearchQuery:
    query: str
    count: int = 5
    freshness: SearchFreshness = "all"
    domains: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        normalized_query = self.query.strip()
        if not normalized_query:
            raise ValueError("query must be non-empty")
        if len(normalized_query) > 400:
            raise ValueError("query must be at most 400 characters")
        if isinstance(self.count, bool) or not 1 <= self.count <= 10:
            raise ValueError("count must be between 1 and 10")
        if self.freshness not in _FRESHNESS_VALUES:
            raise ValueError("freshness must be one of: all, day, week, month, year")
        if len(self.domains) > 10:
            raise ValueError("domains must contain at most 10 entries")

        normalized_domains: list[str] = []
        for domain in self.domains:
            value = domain.strip().lower().rstrip(".")
            parsed = urlsplit(f"//{value}")
            if (
                not value
                or parsed.hostname != value
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or any(char.isspace() for char in value)
            ):
                raise ValueError(f"invalid domain: {domain}")
            normalized_domains.append(value)

        object.__setattr__(self, "query", normalized_query)
        object.__setattr__(self, "domains", tuple(dict.fromkeys(normalized_domains)))


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str | None = None
    published_at: str | None = None


@dataclass(frozen=True)
class SearchResponse:
    query: str
    results: tuple[SearchResult, ...]
    backend: str
    request_id: str | None = None
    warnings: tuple[str, ...] = ()


__all__ = [
    "SearchBackendError",
    "SearchErrorCode",
    "SearchFreshness",
    "SearchQuery",
    "SearchResponse",
    "SearchResult",
]
