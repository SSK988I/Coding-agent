"""bash tool.

Runs a command in a real bash shell (Git Bash on Windows, /bin/bash on Unix)
so the model can rely on ls / pwd / grep everywhere. Falls back to the system
shell only when no bash is present; in that case ``shell_kind == "system"``
and callers can inject platform hints into the prompt.

"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any

from agent_llm import TextContent

from agent_core.shell import ShellConfig, get_shell_config
from agent_core.types import AgentToolResult


BASH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "The bash command to execute.",
        },
        "timeout": {
            "type": "number",
            "description": "Timeout in seconds (optional, default 120).",
        },
    },
    "required": ["command"],
}

#: Default timeout in seconds.
DEFAULT_TIMEOUT = 120
#: Content-level cap: keep output bounded.
DEFAULT_MAX_LINES = 2000
#: Hard byte cap applied before decoding so a single giant line cannot enter
#: the model context. The subprocess transport still captures output in memory,
#: but the retained/decoded result is bounded.
DEFAULT_MAX_BYTES = 1024 * 1024


def _decode_output(data: bytes) -> str:
    """Decode subprocess output robustly.

    Git Bash emits UTF-8; the cmd.exe fallback emits the OEM code page
    (CP936/GBK on Chinese Windows). Try UTF-8 first, then the Windows OEM
    code page, then a lossy pass — never raising.
    """
    candidates = ["utf-8"]
    if sys.platform == "win32":
        candidates.append(_windows_oem_encoding())
    for enc in candidates:
        if not enc:
            continue
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _windows_oem_encoding() -> str:
    """Get the Windows console (OEM) code page, e.g. cp936 for Chinese Windows."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        codepage = ctypes.windll.kernel32.GetConsoleOutputCP()  # type: ignore[attr-defined]
        if codepage:
            return f"cp{codepage}"
    except Exception:
        pass
    return "mbcs"


class BashTool:
    """Run a command in bash and return combined stdout/stderr.

    Resolves the shell via :func:`agent_core.shell.get_shell_config` (Git Bash
    first, system shell fallback). Exposes ``shell_kind`` so the prompt builder
    knows whether to inject platform hints. Output is tail-truncated to
    ``max_lines`` with an ``... (N earlier lines)`` prefix.
    """

    name: str = "bash"
    label: str = "bash"
    description: str = (
        f"Execute a bash command in the current working directory. Returns "
        f"stdout and stderr. Output is truncated to the last {DEFAULT_MAX_LINES} "
        f"lines. Optionally provide a timeout in seconds."
    )
    parameters: dict = BASH_SCHEMA
    prompt_snippet: str = "Execute bash commands (ls, grep, find, etc.)"

    def __init__(
        self,
        cwd: str = ".",
        *,
        max_lines: int = DEFAULT_MAX_LINES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        shell_config: ShellConfig | None = None,
    ) -> None:
        self.cwd = cwd
        self.max_lines = max_lines
        self.max_bytes = max_bytes
        # Resolve once and cache. Callers may inject for tests.
        self.shell_config = shell_config or get_shell_config()

    @property
    def shell_kind(self) -> str:
        return self.shell_config.shell_kind

    async def execute(
        self,
        tool_call_id: str,
        params: dict,
        signal: Any = None,
    ) -> AgentToolResult:
        command = params["command"]
        timeout = params.get("timeout")
        timeout_s = float(timeout) if timeout is not None else float(DEFAULT_TIMEOUT)

        proc = await self._spawn(command)
        try:
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise RuntimeError(
                f"Command timed out after {int(timeout_s)}s: {command}"
            )

        stdout_bytes, _ = self._tail_cap_bytes(stdout_bytes)
        output = _decode_output(stdout_bytes) if stdout_bytes else ""
        exit_code = proc.returncode if proc.returncode is not None else -1

        # Content-level truncation. Bash output is most
        # useful at the end (errors, final results), so keep the tail and prefix
        # a count of dropped earlier lines.
        text = self._tail_truncate(output)

        if not text.strip():
            text = "(no output)"
        if exit_code != 0:
            text += f"\n[exit code: {exit_code}]"

        return AgentToolResult(content=[TextContent(text=text)])

    async def _spawn(self, command: str):
        """Spawn the resolved shell with ``command``.

        Bash gets ``bash -c "<command>"``; the WSL legacy launcher gets the
        command via stdin. The cmd.exe fallback uses ``cmd /c <command>``.
        """
        cfg = self.shell_config
        cwd = self.cwd

        if cfg.command_transport == "stdin":
            proc = await asyncio.create_subprocess_exec(
                cfg.shell, *cfg.args,
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            # Feed command via stdin then close.
            assert proc.stdin is not None
            proc.stdin.write(command.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            return proc

        # argv transport: bash -c "cmd" / cmd /c "cmd".
        return await asyncio.create_subprocess_exec(
            cfg.shell, *cfg.args, command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    def _tail_truncate(self, output: str) -> str:
        """Keep the last ``max_lines`` lines; prefix a dropped-count notice.

        Bash output is usually most useful at the end, so truncation keeps the tail.
        """
        lines = output.splitlines()
        if len(lines) <= self.max_lines:
            return output
        dropped = len(lines) - self.max_lines
        kept = lines[-self.max_lines:]
        return f"... ({dropped} earlier lines, truncated)\n" + "\n".join(kept)

    def _tail_cap_bytes(self, output: bytes) -> tuple[bytes, bool]:
        """Keep at most ``max_bytes`` from the tail of raw subprocess output."""
        if len(output) <= self.max_bytes:
            return output, False
        dropped = len(output) - self.max_bytes
        notice = f"... ({dropped} earlier bytes, truncated)\n".encode("ascii")
        return notice + output[-self.max_bytes:], True

    async def run_raw(self, command: str, *, timeout: float | None = None) -> "BashRawResult":
        """Run ``command`` and return structured output (no text formatting).

        Used by the interactive ``!`` passthrough, which needs the raw output
        and exit code as separate fields (for ``BashExecutionMessage``) rather
        than the LLM-facing formatted text that :meth:`execute` produces.

        Returns a :class:`BashRawResult` with ``output`` (tail-truncated, same
        rule as execute), ``exit_code``, ``truncated``, and ``timed_out``.
        """
        timeout_s = float(timeout) if timeout is not None else float(DEFAULT_TIMEOUT)
        try:
            proc = await self._spawn(command)
        except Exception as e:
            return BashRawResult(output=f"Failed to spawn shell: {e}", exit_code=-1)

        try:
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            return BashRawResult(
                output=f"Command timed out after {int(timeout_s)}s",
                exit_code=124, timed_out=True,
            )

        stdout_bytes, byte_truncated = self._tail_cap_bytes(stdout_bytes)
        output = _decode_output(stdout_bytes) if stdout_bytes else ""
        exit_code = proc.returncode if proc.returncode is not None else -1
        lines = output.splitlines()
        line_truncated = len(lines) > self.max_lines
        truncated = byte_truncated or line_truncated
        if line_truncated:
            output = self._tail_truncate(output)
        return BashRawResult(output=output, exit_code=exit_code, truncated=truncated)


@dataclass
class BashRawResult:
    """Structured result of :meth:`BashTool.run_raw`."""

    output: str
    exit_code: int = 0
    truncated: bool = False
    timed_out: bool = False
