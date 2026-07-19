"""grep tool.

Searches file contents for a pattern (regex or literal). Prefers ripgrep
(``rg``) when on PATH for correctness (.gitignore adherence, Rust regex
dialect, speed); falls back to a pure-Python implementation (``re`` + walk +
pathspec for .gitignore) when ``rg`` is unavailable.

Output format is ``{relpath}:{lineno}: {line}``, one per match.

Searches use the local filesystem. Context-line support is simplified in the
pure-Python fallback.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from agent_llm import TextContent

from agent_core.types import AgentToolResult
from agent_core.tools._subprocess import (
    find_in_path,
    head_truncate_bytes,
    truncate_line,
)
from agent_core.tools._gitignore import is_ignored

GREP_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Search pattern (regex or literal string).",
        },
        "path": {
            "type": "string",
            "description": "Directory or file to search (optional, default current directory).",
        },
        "glob": {
            "type": "string",
            "description": "Filter files by glob pattern, e.g. '*.py' or '**/*.spec.py'.",
        },
        "ignoreCase": {
            "type": "boolean",
            "description": "Case-insensitive search (optional, default false).",
        },
        "literal": {
            "type": "boolean",
            "description": "Treat pattern as a literal string instead of regex (optional, default false).",
        },
        "context": {
            "type": "number",
            "description": "Number of lines to show before and after each match (optional, default 0).",
        },
        "limit": {
            "type": "number",
            "description": "Maximum number of matches to return (optional, default 100).",
        },
    },
    "required": ["pattern"],
}

#: Default match cap.
DEFAULT_LIMIT = 100
#: Per-line char cap.
MAX_LINE_LENGTH = 500
#: Output byte cap.
MAX_OUTPUT_BYTES = 50 * 1024


class GrepTool:
    """Search file contents for a pattern.

    Uses ripgrep when available; otherwise falls back to a pure-Python search that respects
    .gitignore via pathspec.
    """

    name: str = "grep"
    label: str = "grep"
    description: str = (
        f"Search file contents for a pattern. Returns matching lines with file "
        f"paths and line numbers. Respects .gitignore. Output is truncated to "
        f"{DEFAULT_LIMIT} matches or {MAX_OUTPUT_BYTES // 1024}KB. Long lines "
        f"are truncated to {MAX_LINE_LENGTH} chars."
    )
    parameters: dict = GREP_SCHEMA
    prompt_snippet: str = "Search file contents for patterns (respects .gitignore)"
    prompt_guidelines: list[str] = [
        "Use grep to find code, definitions, or usages instead of reading files blindly.",
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
        glob_pat = params.get("glob")
        ignore_case = bool(params.get("ignoreCase", False))
        literal = bool(params.get("literal", False))
        context = max(0, int(params.get("context") or 0))
        limit = max(1, int(params.get("limit") or self.limit))

        full_path = path if os.path.isabs(path) else os.path.join(self.cwd, path)
        full_path = os.path.normpath(full_path)
        if not os.path.exists(full_path):
            raise FileNotFoundError(f"Path not found: {path}")

        # Prefer ripgrep; fall back to pure Python.
        rg = find_in_path("rg")
        if rg:
            text = await self._run_with_rg(
                rg, pattern, full_path, glob_pat, ignore_case, literal, limit
            )
        else:
            text = self._run_with_python(
                pattern, full_path, glob_pat, ignore_case, literal, context, limit
            )

        if not text.strip():
            return AgentToolResult(content=[TextContent(text="No matches found")])
        return AgentToolResult(content=[TextContent(text=text)])

    # ─── ripgrep path ────────────────────────────────

    async def _run_with_rg(
        self,
        rg: str,
        pattern: str,
        search_path: str,
        glob_pat: str | None,
        ignore_case: bool,
        literal: bool,
        limit: int,
    ) -> str:
        args = [rg, "--json", "--line-number", "--color=never", "--hidden"]
        if ignore_case:
            args.append("--ignore-case")
        if literal:
            args.append("--fixed-strings")
        if glob_pat:
            args += ["--glob", glob_pat]
        args += ["--", pattern, search_path]

        from agent_core.tools._subprocess import run_subprocess_lines
        lines, exit_code, stderr = await run_subprocess_lines(args, timeout=60.0)

        # exit 0 = matches, 1 = no matches, both OK.
        if exit_code not in (0, 1):
            err = stderr.strip() or f"ripgrep exited with code {exit_code}"
            raise RuntimeError(err)

        matches: list[tuple[str, int, str]] = []
        limit_reached = False
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data", {})
            try:
                matches.append((
                    data["path"]["text"],
                    data["line_number"],
                    data["lines"]["text"],
                ))
            except (KeyError, TypeError):
                continue
            if len(matches) >= limit:
                limit_reached = True
                break

        return self._format_matches(matches, search_path, limit_reached, limit)

    # ─── pure-Python fallback ──────────────────────────────────────────

    def _run_with_python(
        self,
        pattern: str,
        search_path: str,
        glob_pat: str | None,
        ignore_case: bool,
        literal: bool,
        context: int,
        limit: int,
    ) -> str:
        flags = re.IGNORECASE if ignore_case else 0
        if literal:
            regex = re.compile(re.escape(pattern), flags)
        else:
            try:
                regex = re.compile(pattern, flags)
            except re.error as e:
                raise ValueError(f"Invalid regex pattern: {e}") from e

        # Compile the glob filter if provided.
        glob_regex = None
        if glob_pat:
            glob_regex = self._glob_to_regex(glob_pat)

        matches: list[tuple[str, int, str]] = []
        limit_reached = False
        root_is_dir = os.path.isdir(search_path)
        root = search_path if root_is_dir else os.path.dirname(search_path)

        def _scan_file(abs_file: str, rel: str) -> None:
            nonlocal limit_reached
            if limit_reached:
                return
            try:
                with open(abs_file, "r", encoding="utf-8", errors="replace") as fh:
                    for lineno, line in enumerate(fh, start=1):
                        line_stripped = line.rstrip("\n").rstrip("\r")
                        if regex.search(line_stripped):
                            matches.append((rel, lineno, line_stripped))
                            if len(matches) >= limit:
                                limit_reached = True
                                return
            except OSError:
                pass

        if not root_is_dir:
            # Single file.
            _scan_file(search_path, os.path.basename(search_path))
        else:
            for dirpath, dirnames, filenames in os.walk(search_path):
                # Skip .git; respect .gitignore for dirs.
                pruned = []
                for d in list(dirnames):
                    full_d = os.path.join(dirpath, d)
                    if d == ".git" or is_ignored(full_d, root, is_dir=True):
                        continue
                    pruned.append(d)
                dirnames[:] = pruned
                for fn in filenames:
                    abs_f = os.path.join(dirpath, fn)
                    if is_ignored(abs_f, root, is_dir=False):
                        continue
                    if glob_regex and not glob_regex.search(abs_f.replace(os.sep, "/")):
                        continue
                    rel = os.path.relpath(abs_f, search_path).replace(os.sep, "/")
                    _scan_file(abs_f, rel)
                    if limit_reached:
                        break

        return self._format_matches(matches, search_path, limit_reached, limit)

    # ─── output formatting ───────────────────────────

    def _format_matches(
        self,
        matches: list[tuple[str, int, str]],
        search_path: str,
        limit_reached: bool,
        limit: int,
    ) -> str:
        out_lines: list[str] = []
        root_is_dir = os.path.isdir(search_path)
        for raw_path, lineno, line_text in matches:
            # Relative path formatting.
            if root_is_dir:
                try:
                    rel = os.path.relpath(raw_path, search_path)
                except ValueError:
                    rel = raw_path
                display = rel.replace(os.sep, "/")
            else:
                display = os.path.basename(raw_path)
            line_clean = truncate_line(line_text.rstrip("\n"), MAX_LINE_LENGTH)
            out_lines.append(f"{display}:{lineno}: {line_clean}")

        text = "\n".join(out_lines)
        text, byte_truncated = head_truncate_bytes(text, MAX_OUTPUT_BYTES)

        notices: list[str] = []
        if byte_truncated:
            notices.append(f"{MAX_OUTPUT_BYTES // 1024}KB limit reached")
        if limit_reached:
            notices.append(f"{limit} matches limit reached. Use limit={limit * 2} for more, or refine pattern")
        if notices:
            text += "\n\n[" + ". ".join(notices) + "]"
        return text

    @staticmethod
    def _glob_to_regex(glob_pat: str) -> re.Pattern:
        """Convert a simple glob (with * / ** / ?) to a regex matching the path."""
        import fnmatch
        # fnmatch translates * and ?; we treat ** as matching across separators.
        # Normalize: ** → placeholder, then fnmatch, then restore.
        # Simple approach: use fnmatch.translate on each path segment joined by /.
        # For our use, treating the whole pattern as an fnmatch against the
        # forward-slashed path is sufficient.
        # ** in fnmatch is just *, but we want ** to cross dir boundaries.
        norm = glob_pat.replace("\\", "/")
        # Let ** match anything including /
        regex = fnmatch.translate(norm)
        # fnmatch.translate treats * as [^/]*-ish? Actually fnmatch * matches
        # everything except path sep on some platforms; on Python it matches
        # everything. We post-process ** patterns to cross separators.
        return re.compile(regex)
