"""find tool.

Finds files by glob pattern. Prefers ``fd`` when on PATH (.gitignore
adherence, hidden-file handling); falls back to ``pathlib.Path.glob`` +
pathspec-based .gitignore filtering when ``fd`` is unavailable. Searches use
the local filesystem.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent_llm import TextContent

from agent_core.types import AgentToolResult
from agent_core.tools._subprocess import find_in_path, head_truncate_bytes
from agent_core.tools._gitignore import is_ignored

FIND_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Glob pattern to match files, e.g. '*.py', '**/*.json', or 'src/**/*.py'.",
        },
        "path": {
            "type": "string",
            "description": "Directory to search in (optional, default current directory).",
        },
        "limit": {
            "type": "number",
            "description": "Maximum number of results (optional, default 1000).",
        },
    },
    "required": ["pattern"],
}

#: Default result cap.
DEFAULT_LIMIT = 1000
#: Output byte cap.
MAX_OUTPUT_BYTES = 50 * 1024


class FindTool:
    """Find files by glob pattern.

    Uses fd when available; otherwise falls back to pathlib.Path.glob with
    .gitignore filtering.
    Returns POSIX relative paths, one per line.
    """

    name: str = "find"
    label: str = "find"
    description: str = (
        f"Search for files by glob pattern. Returns matching file paths relative "
        f"to the search directory. Respects .gitignore. Output is truncated to "
        f"{DEFAULT_LIMIT} results or {MAX_OUTPUT_BYTES // 1024}KB."
    )
    parameters: dict = FIND_SCHEMA
    prompt_snippet: str = "Find files by glob pattern (respects .gitignore)"
    prompt_guidelines: list[str] = [
        "Use find to locate files by name or extension before searching their contents.",
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
        pattern = params["pattern"]
        path = params.get("path") or "."
        limit = max(1, int(params.get("limit") or self.limit))

        full_path = path if os.path.isabs(path) else os.path.join(self.cwd, path)
        full_path = os.path.normpath(full_path)
        if not os.path.isdir(full_path):
            raise FileNotFoundError(f"Path not found: {path}")

        fd = find_in_path("fd")
        if fd:
            text = await self._run_with_fd(fd, pattern, full_path, limit)
        else:
            text = self._run_with_pathlib(pattern, full_path, limit)

        if not text.strip():
            return AgentToolResult(content=[TextContent(text="No files found matching pattern")])
        return AgentToolResult(content=[TextContent(text=text)])

    # ─── fd path ─────────────────────────────────────

    async def _run_with_fd(
        self,
        fd: str,
        pattern: str,
        search_path: str,
        limit: int,
    ) -> str:
        args = [fd, "--glob", "--color=never", "--hidden"]
        # --no-require-git so .gitignore is honored even outside a repo.
        if not self._inside_git_repo(search_path):
            args.append("--no-require-git")
        args += ["--max-results", str(limit)]

        # Pattern patching: patterns with "/" switch to
        # full-path matching and need a "**/" prefix when relative.
        effective = pattern
        if "/" in pattern and not pattern.startswith("/") and not pattern.startswith("**/"):
            effective = "**/" + pattern
            args.append("--full-path")
        args += ["--", effective, search_path]

        from agent_core.tools._subprocess import run_subprocess_lines
        lines, exit_code, stderr = await run_subprocess_lines(args, timeout=30.0)

        # fd exits non-zero sometimes even with partial output; tolerate it.
        if exit_code != 0 and not lines:
            err = stderr.strip() or f"fd exited with code {exit_code}"
            raise RuntimeError(err)

        # Relativize + POSIX-ize.
        results: list[str] = []
        limit_reached = False
        for raw in lines:
            entry = raw.strip().replace("\\", "/").rstrip("/")
            if not entry:
                continue
            # Make relative to search_path.
            try:
                rel = os.path.relpath(entry, search_path).replace(os.sep, "/")
            except ValueError:
                rel = entry
            # Re-add trailing slash for directories (fd omits it).
            if os.path.isdir(os.path.join(search_path, *entry.split("/")) if os.path.isabs(entry) else entry):
                pass  # fd already tells us; we keep as-is
            results.append(rel)
            if len(results) >= limit:
                limit_reached = True
                break

        return self._format(results, limit_reached, limit)

    # ─── pathlib fallback ──────────────────────────────────────────────

    def _run_with_pathlib(
        self,
        pattern: str,
        search_path: str,
        limit: int,
    ) -> str:
        root = Path(search_path)
        results: list[str] = []
        limit_reached = False

        # pathlib's glob treats ** as recursive. Normalize backslashes.
        glob_pat = pattern.replace("\\", "/")
        seen: set[str] = set()
        for p in root.glob(glob_pat):
            # Skip ignored files.
            if is_ignored(str(p), search_path, is_dir=p.is_dir()):
                continue
            if p.name == ".git" or ".git" in p.parts:
                continue
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                rel = str(p)
            if rel in seen:
                continue
            seen.add(rel)
            suffix = "/" if p.is_dir() else ""
            results.append(rel + suffix)
            if len(results) >= limit:
                limit_reached = True
                break

        # Sort for deterministic output (fd sorts; pathlib doesn't guarantee).
        results.sort(key=lambda s: s.lower())
        return self._format(results, limit_reached, limit)

    # ─── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _inside_git_repo(path: str) -> bool:
        """Walk up from path looking for a .git dir."""
        current = os.path.abspath(path)
        while True:
            if os.path.isdir(os.path.join(current, ".git")):
                return True
            parent = os.path.dirname(current)
            if parent == current:
                return False
            current = parent

    @staticmethod
    def _format(results: list[str], limit_reached: bool, limit: int) -> str:
        text = "\n".join(results)
        text, byte_truncated = head_truncate_bytes(text, MAX_OUTPUT_BYTES)
        notices: list[str] = []
        if byte_truncated:
            notices.append(f"{MAX_OUTPUT_BYTES // 1024}KB limit reached")
        if limit_reached:
            notices.append(f"{limit} results limit reached. Use limit={limit * 2} for more, or refine pattern")
        if notices:
            text += "\n\n[" + ". ".join(notices) + "]"
        return text
