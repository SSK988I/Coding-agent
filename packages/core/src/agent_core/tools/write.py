"""write tool.

Writes content to a file, creating or overwriting it. Errors (parent dir
missing, permission) are raised (caught by the agent loop -> error tool
result). The agent is expected to call `read` first when editing existing
files; this tool is a full overwrite.

"""
from __future__ import annotations

import os
from typing import Any

from agent_llm import TextContent

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
    Creates the file if missing; overwrites if present. Does NOT create parent
    directories — if the parent dir doesn't exist, this raises (the loop
    surfaces it as an error tool result so the model can react).

    Mirrors ReadTool's path-resolution conventions for consistency.
    """

    name: str = "write"
    label: str = "write"
    description: str = "Write content to a file. Creates or overwrites the file."
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

        parent = os.path.dirname(full_path)
        if parent and not os.path.isdir(parent):
            raise FileNotFoundError(
                f"Directory does not exist: {parent}. Create it first."
            )

        try:
            with open(full_path, "w", encoding="utf-8", newline="") as f:
                f.write(content)
        except OSError as e:
            raise RuntimeError(f"Failed to write {path}: {e}") from e

        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        byte_count = len(content.encode("utf-8"))
        summary = f"Wrote {line_count} line(s) / {byte_count} byte(s) to {path}"
        return AgentToolResult(content=[TextContent(text=summary)])
