"""Structured, read-only Git tools for Plan mode.

The tools in this module never invoke a shell and expose no free-form Git
arguments.  Each command is assembled from a fixed argv template, with
repository-controlled pagers, prompts, fsmonitor helpers, external diff
drivers, and textconv helpers disabled where applicable.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from agent_llm import TextContent

from agent_core.tools._subprocess import find_in_path
from agent_core.tools.bash import _decode_output
from agent_core.types import AgentToolResult, PlanAccess

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_BYTES = 100 * 1024
MAX_STDERR_BYTES = 16 * 1024
MAX_REVISION_LENGTH = 256
MAX_PATH_LENGTH = 4096
MAX_LOG_ENTRIES = 200

_REVISION_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/@{}~^+\-]*\Z")
_WINDOWS_ABSOLUTE_RE = re.compile(r"\A[A-Za-z]:[\\/]")

_PATH_PROPERTY = {
    "type": "string",
    "description": "Optional literal repository-relative path (not a Git pathspec).",
    "minLength": 1,
    "maxLength": MAX_PATH_LENGTH,
}
_REVISION_PROPERTY = {
    "type": "string",
    "description": "Optional Git revision or revision range, such as HEAD, HEAD~1, or main..topic.",
    "minLength": 1,
    "maxLength": MAX_REVISION_LENGTH,
}

GIT_STATUS_SCHEMA: dict = {
    "type": "object",
    "properties": {"path": _PATH_PROPERTY},
    "additionalProperties": False,
}

GIT_LOG_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "revision": _REVISION_PROPERTY,
        "path": _PATH_PROPERTY,
        "limit": {
            "type": "integer",
            "description": f"Maximum commits to return (default 20, maximum {MAX_LOG_ENTRIES}).",
            "minimum": 1,
            "maximum": MAX_LOG_ENTRIES,
        },
    },
    "additionalProperties": False,
}

GIT_DIFF_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "revision": _REVISION_PROPERTY,
        "path": _PATH_PROPERTY,
    },
    "additionalProperties": False,
}

GIT_SHOW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "revision": {
            **_REVISION_PROPERTY,
            "description": "Git revision to inspect (default HEAD).",
        },
        "path": _PATH_PROPERTY,
    },
    "additionalProperties": False,
}


def _contains_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _validate_revision(value: Any, *, default: str | None = None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("revision must be a string")
    if not value or len(value) > MAX_REVISION_LENGTH:
        raise ValueError(f"revision must contain 1-{MAX_REVISION_LENGTH} characters")
    if value.startswith("-") or _contains_control(value) or not _REVISION_RE.fullmatch(value):
        raise ValueError("revision contains unsupported characters")
    return value


def _validate_path(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("path must be a string")
    if not value or len(value) > MAX_PATH_LENGTH:
        raise ValueError(f"path must contain 1-{MAX_PATH_LENGTH} characters")
    if value.startswith("-") or _contains_control(value):
        raise ValueError("path contains unsupported characters")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or _WINDOWS_ABSOLUTE_RE.match(value):
        raise ValueError("path must be repository-relative")
    if any(part == ".." for part in normalized.split("/")):
        raise ValueError("path must not escape the repository")
    return value


def _validate_limit(value: Any) -> int:
    if value is None:
        return 20
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("limit must be an integer")
    if not 1 <= value <= MAX_LOG_ENTRIES:
        raise ValueError(f"limit must be between 1 and {MAX_LOG_ENTRIES}")
    return value


async def _read_limited(
    stream: asyncio.StreamReader | None,
    max_bytes: int,
) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    retained = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        remaining = max_bytes - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
    return bytes(retained), truncated


class _StructuredGitTool:
    effect: str = "read"
    plan_access: PlanAccess = "observe"

    def __init__(
        self,
        cwd: str = ".",
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        git_path: str | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.cwd = os.path.abspath(cwd)
        self.timeout = float(timeout)
        self.max_bytes = int(max_bytes)
        self.git_path = git_path

    def _argv(self, command_args: list[str]) -> list[str]:
        git_path = self.git_path or find_in_path("git")
        if not git_path:
            raise RuntimeError("Git executable not found on PATH")
        return [
            git_path,
            "--no-pager",
            "--literal-pathspecs",
            "-c",
            "core.fsmonitor=false",
            *command_args,
        ]

    async def _run(self, command_args: list[str]) -> AgentToolResult:
        argv = self._argv(command_args)
        env = os.environ.copy()
        env.update(
            {
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
            }
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Failed to run Git: {exc}") from exc

        stdout_task = asyncio.create_task(_read_limited(process.stdout, self.max_bytes))
        stderr_task = asyncio.create_task(_read_limited(process.stderr, MAX_STDERR_BYTES))
        try:
            stdout_result, stderr_result = await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task),
                timeout=self.timeout,
            )
            await process.wait()
        except TimeoutError as exc:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise RuntimeError(f"Git command timed out after {self.timeout:g}s") from exc
        except asyncio.CancelledError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise

        stdout_bytes, stdout_truncated = stdout_result
        stderr_bytes, stderr_truncated = stderr_result
        stdout = _decode_output(stdout_bytes)
        stderr = _decode_output(stderr_bytes)
        exit_code = process.returncode if process.returncode is not None else -1
        if exit_code != 0:
            reason = stderr.strip() or stdout.strip() or "no diagnostic output"
            if stderr_truncated:
                reason += f"\n[stderr truncated at {MAX_STDERR_BYTES} bytes]"
            raise RuntimeError(f"Git command failed with exit code {exit_code}: {reason}")

        text = stdout.rstrip()
        if stdout_truncated:
            marker = f"[output truncated at {self.max_bytes} bytes]"
            text = f"{text}\n\n{marker}" if text else marker
        if not text:
            text = "(no output)"
        return AgentToolResult(
            content=[TextContent(text=text)],
            details={
                "exit_code": exit_code,
                "output_truncated": stdout_truncated,
            },
        )


class GitStatusTool(_StructuredGitTool):
    """Show a bounded, machine-stable summary of repository status."""

    name: str = "git_status"
    label: str = "git status"
    description: str = "Show branch and working-tree status without invoking a shell, pager, or fsmonitor helper."
    parameters: dict = GIT_STATUS_SCHEMA
    prompt_snippet: str = "Inspect Git branch and working-tree status safely"

    async def execute(
        self,
        tool_call_id: str,
        params: dict,
        signal: Any = None,
    ) -> AgentToolResult:
        path = _validate_path(params.get("path"))
        args = ["status", "--short", "--branch", "--untracked-files=normal"]
        if path is not None:
            args += ["--", path]
        return await self._run(args)


class GitLogTool(_StructuredGitTool):
    """Show commit metadata without patches or caller-provided formatting."""

    name: str = "git_log"
    label: str = "git log"
    description: str = "Show bounded commit metadata; patches and arbitrary Git arguments are disabled."
    parameters: dict = GIT_LOG_SCHEMA
    prompt_snippet: str = "Inspect Git commit history safely"

    async def execute(
        self,
        tool_call_id: str,
        params: dict,
        signal: Any = None,
    ) -> AgentToolResult:
        revision = _validate_revision(params.get("revision"))
        path = _validate_path(params.get("path"))
        limit = _validate_limit(params.get("limit"))
        args = [
            "log",
            "--no-patch",
            "--decorate=no",
            "--date=iso-strict",
            f"--max-count={limit}",
            "--pretty=format:%H%x09%ad%x09%an%x09%s",
        ]
        if revision is not None:
            args.append(revision)
        if path is not None:
            args += ["--", path]
        return await self._run(args)


class GitDiffTool(_StructuredGitTool):
    """Show a patch with external diff and text conversion disabled."""

    name: str = "git_diff"
    label: str = "git diff"
    description: str = "Show a bounded Git diff without shell, external diff drivers, or textconv helpers."
    parameters: dict = GIT_DIFF_SCHEMA
    prompt_snippet: str = "Inspect Git changes safely"

    async def execute(
        self,
        tool_call_id: str,
        params: dict,
        signal: Any = None,
    ) -> AgentToolResult:
        revision = _validate_revision(params.get("revision"))
        path = _validate_path(params.get("path"))
        args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
        if revision is not None:
            args.append(revision)
        if path is not None:
            args += ["--", path]
        return await self._run(args)


class GitShowTool(_StructuredGitTool):
    """Show a revision with external diff and text conversion disabled."""

    name: str = "git_show"
    label: str = "git show"
    description: str = "Show a bounded Git revision without shell, external diff drivers, or textconv helpers."
    parameters: dict = GIT_SHOW_SCHEMA
    prompt_snippet: str = "Inspect a Git revision safely"

    async def execute(
        self,
        tool_call_id: str,
        params: dict,
        signal: Any = None,
    ) -> AgentToolResult:
        revision = _validate_revision(params.get("revision"), default="HEAD")
        path = _validate_path(params.get("path"))
        assert revision is not None
        args = [
            "show",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--decorate=no",
            "--date=iso-strict",
            "--format=fuller",
            revision,
        ]
        if path is not None:
            args += ["--", path]
        return await self._run(args)


__all__ = [
    "GIT_STATUS_SCHEMA",
    "GIT_LOG_SCHEMA",
    "GIT_DIFF_SCHEMA",
    "GIT_SHOW_SCHEMA",
    "GitStatusTool",
    "GitLogTool",
    "GitDiffTool",
    "GitShowTool",
]
