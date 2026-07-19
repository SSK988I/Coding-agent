"""read tool.

Reads a file's contents. Errors (file not found, permission) are raised
(caught by the agent loop -> error tool result). Image support is cut.

"""
from __future__ import annotations

import os
from typing import Any

from agent_llm import TextContent

from agent_core.types import AgentToolResult


READ_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the file to read (relative or absolute).",
        },
        "offset": {
            "type": "number",
            "description": "Line number to start reading from (1-indexed, optional).",
        },
        "limit": {
            "type": "number",
            "description": "Maximum number of lines to read (optional).",
        },
    },
    "required": ["path"],
}

#: Default line cap to keep tool output bounded.
DEFAULT_MAX_LINES = 2000


class ReadTool:
    """Read a file and return its contents as text.

    Resolves relative paths
    against ``cwd``. Supports optional ``offset`` (1-indexed) and ``limit``.
    Raises on I/O failure (the loop surfaces this as an error tool result).
    """

    name: str = "read"
    label: str = "read"
    description: str = "Read the contents of a file."
    parameters: dict = READ_SCHEMA
    prompt_snippet: str = "Read file contents"
    prompt_guidelines: list[str] = ["Use read to examine files instead of cat or sed."]

    def __init__(self, cwd: str = ".", *, max_lines: int = DEFAULT_MAX_LINES) -> None:
        self.cwd = cwd
        self.max_lines = max_lines

    async def execute(
        self,
        tool_call_id: str,
        params: dict,
        signal: Any = None,
    ) -> AgentToolResult:
        path = params["path"]
        offset = params.get("offset")
        limit = params.get("limit")

        # Resolve relative to cwd.
        full_path = path if os.path.isabs(path) else os.path.join(self.cwd, path)
        full_path = os.path.normpath(full_path)

        if not os.path.isfile(full_path):
            raise FileNotFoundError(f"File not found: {path}")

        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            raise RuntimeError(f"Failed to read {path}: {e}") from e

        # Apply offset (1-indexed).
        start = 0
        if offset is not None and offset > 0:
            start = int(offset) - 1
        lines = lines[start:]

        # Apply limit.
        cap = int(limit) if limit is not None else self.max_lines
        truncated = len(lines) > cap
        lines = lines[:cap]

        # Build output with line numbers.
        out_lines = []
        for i, line in enumerate(lines):
            lineno = start + i + 1
            out_lines.append(f"{lineno:>6}\t{line.rstrip()}")
        text = "\n".join(out_lines)

        if truncated:
            # Head-keep: file reads want the start.
            # Tell the model exactly where to resume with the next offset.
            next_offset = start + cap + 1
            text += f"\n\n... ({cap} lines shown; use offset={next_offset} to continue)"

        if not text:
            text = "(empty file)"

        return AgentToolResult(content=[TextContent(text=text)])
