"""Shared subprocess helpers for grep and find tools.

Provides:
  - find_in_path(binary): shutil.which wrapper, None if not on PATH.
  - run_subprocess_lines(args, cwd, timeout): async, yields decoded stdout
    lines, kills on timeout. Reuses bash.py's _decode_output for encoding
    robustness on Windows (Git Bash UTF-8 vs cmd OEM code page).
  - head_truncate(text, max_bytes): byte-cap on a line boundary.

These are used by grep/find to prefer ripgrep/fd when available, with a pure
Python fallback when not.
"""
from __future__ import annotations

import asyncio
import shutil

from agent_core.tools.bash import _decode_output

__all__ = [
    "find_in_path",
    "run_subprocess_lines",
    "head_truncate_bytes",
    "truncate_line",
]


def find_in_path(binary: str) -> str | None:
    """Return the full path to ``binary`` on PATH, or None (shutil.which).

    Checks for ``.exe`` suffix on Windows too.
    """
    found = shutil.which(binary)
    if found:
        return found
    if __import__("sys").platform == "win32":
        found = shutil.which(binary + ".exe")
        if found:
            return found
    return None


async def run_subprocess_lines(
    args: list[str],
    *,
    cwd: str | None = None,
    timeout: float = 30.0,
) -> tuple[list[str], int, str]:
    """Run a subprocess, returning (stdout_lines, exit_code, stderr_text).

    Decodes output via _decode_output (UTF-8 → OEM fallback). On timeout,
    kills the process and raises RuntimeError. Merges stderr into stdout is
    NOT done here — stderr is captured separately for error reporting.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"Failed to run {args[0]}: {e}") from e

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise RuntimeError(f"{args[0]} timed out after {int(timeout)}s")

    stdout = _decode_output(stdout_bytes) if stdout_bytes else ""
    stderr = _decode_output(stderr_bytes) if stderr_bytes else ""
    exit_code = proc.returncode if proc.returncode is not None else -1
    return stdout.splitlines(), exit_code, stderr


def head_truncate_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    """Truncate to the first ``max_bytes`` (UTF-8), on a line boundary.

    Returns (text, was_truncated). Never returns a partial line.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    cut = encoded.rfind(b"\n", 0, max_bytes)
    if cut < 0:
        return "", True
    return encoded[:cut].decode("utf-8", errors="replace"), True


def truncate_line(line: str, max_chars: int = 500) -> str:
    """Truncate a single line to ``max_chars`` with an ellipsis marker."""
    if len(line) <= max_chars:
        return line
    return line[:max_chars] + "... [truncated]"
