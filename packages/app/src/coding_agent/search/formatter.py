"""Sanitize and format untrusted web search results for model consumption."""
from __future__ import annotations

from dataclasses import asdict
from html import escape
from urllib.parse import urlsplit, urlunsplit

from coding_agent.search.types import SearchResponse, SearchResult

MAX_RESULTS = 10
MAX_TITLE_CHARS = 300
MAX_SNIPPET_CHARS = 2_000
MAX_OUTPUT_CHARS = 24_000


def _clean_text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"


def normalize_url(value: object) -> str | None:
    """Return a normalized public HTTP(S) URL without its fragment."""
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, ""))
    except ValueError:
        return None


def normalize_results(results: list[SearchResult] | tuple[SearchResult, ...], count: int) -> tuple[SearchResult, ...]:
    """Validate, deduplicate, and bound provider results."""
    normalized: list[SearchResult] = []
    seen: set[str] = set()
    for result in results:
        url = normalize_url(result.url)
        if url is None or url in seen:
            continue
        seen.add(url)
        title = _clean_text(result.title, MAX_TITLE_CHARS) or url
        normalized.append(SearchResult(
            title=title,
            url=url,
            snippet=_clean_text(result.snippet, MAX_SNIPPET_CHARS),
            source=_clean_text(result.source, MAX_TITLE_CHARS) if result.source else None,
            published_at=_clean_text(result.published_at, 100) if result.published_at else None,
        ))
        if len(normalized) >= min(count, MAX_RESULTS):
            break
    return tuple(normalized)


def response_details(response: SearchResponse) -> dict:
    """Build JSON-safe UI details without retaining transport credentials."""
    return {
        "query": response.query,
        "backend": response.backend,
        "request_id": response.request_id,
        "warnings": list(response.warnings),
        "results": [asdict(result) for result in response.results],
    }


def format_response(response: SearchResponse) -> str:
    """Wrap results in a deterministic untrusted-data boundary."""
    if not response.results:
        return f"No web results found for the query: {response.query}"

    lines = [
        f'<untrusted_web_results query="{escape(response.query, quote=True)}" '
        f'backend="{escape(response.backend, quote=True)}">'
    ]
    for index, result in enumerate(response.results[:MAX_RESULTS], start=1):
        lines.extend([
            f"Result {index}",
            f"Title: {escape(result.title)}",
            f"URL: {escape(result.url)}",
        ])
        if result.source:
            lines.append(f"Source: {escape(result.source)}")
        if result.published_at:
            lines.append(f"Published: {escape(result.published_at)}")
        lines.append(f"Snippet: {escape(result.snippet)}")
        lines.append("")
    if response.warnings:
        lines.append(f"Warnings: {escape('; '.join(response.warnings))}")
    lines.append("</untrusted_web_results>")
    text = "\n".join(lines)
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    suffix = "\n[Web search output truncated]\n</untrusted_web_results>"
    return f"{text[: MAX_OUTPUT_CHARS - len(suffix)].rstrip()}{suffix}"


__all__ = ["format_response", "normalize_results", "normalize_url", "response_details"]
