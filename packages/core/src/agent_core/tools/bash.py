"""bash tool.

Runs a command in a real bash shell (Git Bash on Windows, /bin/bash on Unix)
so the model can rely on ls / pwd / grep everywhere. Falls back to the system
shell only when no bash is present; in that case ``shell_kind == "system"``
and callers can inject platform hints into the prompt.

"""
from __future__ import annotations

import asyncio
import os
import signal as os_signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

from agent_llm import TextContent

from agent_core.shell import ShellConfig, get_shell_config
from agent_core.types import AgentToolResult, PlanAccess


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
    effect: str = "shell"
    plan_access: PlanAccess = "deny"
    label: str = "bash"
    description: str = (
        f"Execute a bash command in the current working directory. Returns "
        f"stdout and stderr. Output is truncated to the last {DEFAULT_MAX_LINES} "
        f"lines. Optionally provide a timeout in seconds."
    )
    parameters: dict = BASH_SCHEMA
    prompt_snippet: str = "Execute bash commands (prefer rg --files for repository scans)"
    prompt_guidelines: list[str] = [
        "When scanning repositories, exclude dependency/build directories during traversal; "
        "prefer `rg --files -g '!**/node_modules/**'` over filtering them after `find`",
    ]

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
        on_update: Callable[[AgentToolResult], None] | None = None,
    ) -> AgentToolResult:
        del tool_call_id
        command = params["command"]
        timeout = params.get("timeout")
        timeout_s = float(timeout) if timeout is not None else float(DEFAULT_TIMEOUT)

        proc: asyncio.subprocess.Process | None = None
        try:
            async with asyncio.timeout(timeout_s):
                proc = await self._spawn(command)
                stdout_bytes, dropped_bytes = await self._collect_output(
                    proc, signal=signal, on_update=on_update,
                )
        except TimeoutError:
            if proc is not None:
                await self._terminate_process_tree(proc)
            raise RuntimeError(
                f"Command timed out after {int(timeout_s)}s: {command}"
            )
        except asyncio.CancelledError:
            if proc is not None:
                await self._terminate_process_tree(proc)
            raise

        output = _decode_output(stdout_bytes) if stdout_bytes else ""
        if dropped_bytes:
            output = f"... ({dropped_bytes} earlier bytes, truncated)\n{output}"
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

    async def _collect_output(
        self,
        proc: asyncio.subprocess.Process,
        *,
        signal: Any = None,
        on_update: Callable[[AgentToolResult], None] | None = None,
    ) -> tuple[bytes, int]:
        """Read stdout incrementally while watching the Agent abort signal."""
        stdout = proc.stdout
        if stdout is None:
            await proc.wait()
            return b"", 0

        retained = bytearray()
        dropped_bytes = 0
        last_update = 0.0

        async def read_stream() -> None:
            nonlocal dropped_bytes, last_update
            while True:
                chunk = await stdout.read(8192)
                if not chunk:
                    return
                retained.extend(chunk)
                overflow = len(retained) - self.max_bytes
                if overflow > 0:
                    del retained[:overflow]
                    dropped_bytes += overflow
                now = time.monotonic()
                if on_update is not None and (last_update == 0.0 or now - last_update >= 0.05):
                    preview = _decode_output(bytes(retained))
                    if dropped_bytes:
                        preview = f"... ({dropped_bytes} earlier bytes, truncated)\n{preview}"
                    preview = self._tail_truncate(preview)
                    on_update(AgentToolResult(content=[TextContent(text=preview)]))
                    last_update = now

        read_task = asyncio.create_task(read_stream())
        signal_task: asyncio.Task[Any] | None = None
        if signal is not None and hasattr(signal, "wait"):
            signal_task = asyncio.create_task(signal.wait())
        try:
            if signal_task is None:
                await read_task
            else:
                done, _ = await asyncio.wait(
                    {read_task, signal_task}, return_when=asyncio.FIRST_COMPLETED,
                )
                if signal_task in done and getattr(signal, "is_set", lambda: False)():
                    await self._terminate_process_tree(proc)
                    read_task.cancel()
                    await asyncio.gather(read_task, return_exceptions=True)
                    raise RuntimeError("Operation aborted")
                await read_task
            await proc.wait()
        finally:
            if signal_task is not None:
                signal_task.cancel()
                await asyncio.gather(signal_task, return_exceptions=True)
            if not read_task.done():
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
        return bytes(retained), dropped_bytes

    async def _spawn(self, command: str):
        """Spawn the resolved shell with ``command``.

        Bash gets ``bash -c "<command>"``; the WSL legacy launcher gets the
        command via stdin. The cmd.exe fallback uses ``cmd /c <command>``.
        """
        cfg = self.shell_config
        cwd = self.cwd
        process_options: dict[str, Any] = {}
        if sys.platform == "win32":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            if cfg.shell_kind == "bash":
                shell_dir = os.path.dirname(os.path.abspath(cfg.shell))
                env = os.environ.copy()
                env["PATH"] = os.pathsep.join((shell_dir, env.get("PATH", "")))
                env.pop("BASH_ENV", None)
                env.pop("ENV", None)
                env.update({
                    "CI": "1",
                    "GIT_PAGER": "cat",
                    "GIT_TERMINAL_PROMPT": "0",
                    "PAGER": "cat",
                    "TERM": "dumb",
                })
                process_options["env"] = env
        else:
            process_options["start_new_session"] = True

        if cfg.command_transport == "stdin":
            proc = await asyncio.create_subprocess_exec(
                cfg.shell, *cfg.args,
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **process_options,
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
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **process_options,
        )

    async def _terminate_process_tree(self, proc: asyncio.subprocess.Process) -> None:
        """Terminate the shell and its descendants, then reap the shell."""
        if proc.returncode is not None:
            return
        if sys.platform == "win32" and proc.pid:
            killer: asyncio.subprocess.Process | None = None
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(proc.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=3.0)
            except (OSError, TimeoutError):
                if killer is not None and killer.returncode is None:
                    try:
                        killer.kill()
                    except ProcessLookupError:
                        pass
        elif proc.pid:
            killpg = getattr(os, "killpg", None)
            sigkill = getattr(os_signal, "SIGKILL", None)
            if killpg is not None and sigkill is not None:
                try:
                    killpg(proc.pid, sigkill)
                except (ProcessLookupError, PermissionError):
                    pass
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except TimeoutError:
            pass

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
        proc: asyncio.subprocess.Process | None = None
        try:
            async with asyncio.timeout(timeout_s):
                proc = await self._spawn(command)
                stdout_bytes, dropped_bytes = await self._collect_output(proc)
        except TimeoutError:
            if proc is not None:
                await self._terminate_process_tree(proc)
            return BashRawResult(
                output=f"Command timed out after {int(timeout_s)}s",
                exit_code=124, timed_out=True,
            )
        except asyncio.CancelledError:
            if proc is not None:
                await self._terminate_process_tree(proc)
            raise
        except Exception as e:
            return BashRawResult(output=f"Failed to run shell: {e}", exit_code=-1)

        output = _decode_output(stdout_bytes) if stdout_bytes else ""
        if dropped_bytes:
            output = f"... ({dropped_bytes} earlier bytes, truncated)\n{output}"
        exit_code = proc.returncode if proc.returncode is not None else -1
        lines = output.splitlines()
        line_truncated = len(lines) > self.max_lines
        truncated = bool(dropped_bytes) or line_truncated
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
