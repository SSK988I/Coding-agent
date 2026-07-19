"""ls tool.

Lists a single directory level (no recursion). Directories get a ``/`` suffix;
entries are case-insensitively sorted; dotfiles are included. Pure pathlib —
no subprocess or external binary required. It operates on the local filesystem.
"""
from __future__ import annotations

import os
from typing import Any

from agent_llm import TextContent

from agent_core.types import AgentToolResult

LS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Directory to list (relative or absolute, optional, default current directory).",
        },
        "limit": {
            "type": "number",
            "description": "Maximum number of entries to return (optional, default 500).",
        },
    },
}

#: Default entry cap.
DEFAULT_LIMIT = 500
#: Byte cap on output.
DEFAULT_MAX_BYTES = 50 * 1024


class LsTool:
    """List a directory's entries (single level).

    Directories get a trailing
    ``/``; entries are sorted case-insensitively; dotfiles are included.
    Raises on missing path / not-a-directory (loop surfaces as error result).
    """

    name: str = "ls"
    label: str = "ls"
    description: str = (
        f"List directory contents. Returns entries sorted alphabetically, with "
        f"a '/' suffix for directories. Includes dotfiles. Output is truncated "
        f"to {DEFAULT_LIMIT} entries or {DEFAULT_MAX_BYTES // 1024}KB."
    )
    parameters: dict = LS_SCHEMA
    prompt_snippet: str = "List directory contents"
    prompt_guidelines: list[str] = [
        "Use ls to explore directory structure before reading specific files.",
    ]

    def __init__(self, cwd: str = ".", *, limit: int = DEFAULT_LIMIT) -> None:
        self.cwd = cwd
        self.limit = limit

    async def execute(
        self,
        tool_call_id: str,
        params: dict,
        signal: Any = None,
    ) -> AgentToolResult:
        path = params.get("path") or "."
        limit = int(params.get("limit") or self.limit)
        if limit < 1:
            limit = self.limit

        # Resolve relative to cwd.
        full_path = path if os.path.isabs(path) else os.path.join(self.cwd, path)
        full_path = os.path.normpath(full_path)

        if not os.path.exists(full_path):
            raise FileNotFoundError(f"Path not found: {path}")
        if not os.path.isdir(full_path):
            raise NotADirectoryError(f"Not a directory: {path}")

        try:
            entries = sorted(os.listdir(full_path), key=lambda s: s.lower())
        except OSError as e:
            raise RuntimeError(f"Cannot read directory: {e}") from e

        results: list[str] = []
        entry_limit_reached = False
        for entry in entries:
            if len(results) >= limit:
                entry_limit_reached = True
                break
            full = os.path.join(full_path, entry)
            try:
                suffix = "/" if os.path.isdir(full) else ""
            except OSError:
                # Skip entries we can't stat (permission, broken symlink).
                continue
            results.append(entry + suffix)

        if not results:
            return AgentToolResult(content=[TextContent(text="(empty directory)")])

        text = "\n".join(results)

        # Byte-cap.
        notices: list[str] = []
        text, byte_truncated = _head_truncate_bytes(text, DEFAULT_MAX_BYTES)
        if byte_truncated:
            notices.append(f"{DEFAULT_MAX_BYTES // 1024}KB limit reached")
        if entry_limit_reached:
            notices.append(
                f"{limit} entries limit reached. Use limit={limit * 2} for more"
            )
        if notices:
            text += "\n\n[" + ". ".join(notices) + "]"

        return AgentToolResult(content=[TextContent(text=text)])


def _head_truncate_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    """Truncate to the first ``max_bytes`` (UTF-8), on a line boundary.

    Returns (truncated_text, was_truncated). Never returns a partial line.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    # Cut on a line boundary at or before max_bytes.
    cut = encoded.rfind(b"\n", 0, max_bytes)
    if cut < 0:
        # First line itself exceeds the budget: return empty (no partial lines).
        return "", True
    return encoded[:cut].decode("utf-8", errors="replace"), True
