"""write tool.

Writes content to a file, creating or overwriting it. Missing parent
directories are created automatically (recursive). Errors (permission) are
raised (caught by the agent loop -> error tool result). The agent is expected
to call `read` first when editing existing files; this tool is a full
overwrite.

"""
from __future__ import annotations

import os
from typing import Any

from agent_llm import TextContent

from agent_core.tools._mutation import file_mutation_lock
from agent_core.types import AgentToolResult


WRITE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the file to write (relative or absolute).",
        },
        "content": {
            "type": "string",
            "description": "The full text content to write to the file.",
        },
    },
    "required": ["path", "content"],
}


class WriteTool:
    """Write content to a file (overwrite).

    Resolves relative paths against ``cwd``.
    Creates the file if missing; overwrites if present. Creates missing parent
    directories (recursive) so writes to new subpaths succeed.

    Mirrors ReadTool's path-resolution conventions for consistency.
    """

    name: str = "write"
    label: str = "write"
    description: str = (
        "Write content to a file. Creates the file if it doesn't exist, "
        "overwrites if it does. Automatically creates parent directories."
    )
    parameters: dict = WRITE_SCHEMA
    prompt_snippet: str = "Create or overwrite files"
    prompt_guidelines: list[str] = ["Use write only for new files or complete rewrites."]

    def __init__(self, cwd: str = ".") -> None:
        self.cwd = cwd

    async def execute(
        self,
        tool_call_id: str,
        params: dict,
        signal: Any = None,
    ) -> AgentToolResult:
        path = params["path"]
        content = params["content"]

        # Resolve relative to cwd (same convention as ReadTool).
        full_path = path if os.path.isabs(path) else os.path.join(self.cwd, path)
        full_path = os.path.normpath(full_path)

        # Serialize per-realpath (shared with edit via _mutation) so concurrent
        # writes/edits to the same file don't interleave or clobber.
        async with file_mutation_lock(full_path):
            parent = os.path.dirname(full_path)
            if parent:
                os.makedirs(parent, exist_ok=True)

            try:
                with open(full_path, "w", encoding="utf-8", newline="") as f:
                    f.write(content)
            except OSError as e:
                raise RuntimeError(f"Failed to write {path}: {e}") from e

        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        byte_count = len(content.encode("utf-8"))
        summary = f"Wrote {line_count} line(s) / {byte_count} byte(s) to {path}"
        return AgentToolResult(content=[TextContent(text=summary)])
