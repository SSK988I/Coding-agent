"""Bash shell resolution.

Finds a real ``bash`` executable cross-platform so the agent always runs
commands in a bash environment (ls / pwd / grep work everywhere), instead of
the system default shell (cmd.exe on Windows, where those commands fail).

Resolution order:
  1. Git Bash in known locations          (Windows only)
  2. ``bash`` / ``bash.exe`` on PATH      (where/which)
  3. Fallback to the system shell         (cmd.exe on Windows, sh on Unix)

When no bash is found we return ``shell_kind="system"`` so callers can inject
platform hints into the system prompt (e.g. tell the model it's on cmd.exe and
should use ``dir`` instead of ``ls``). On Windows legacy WSL ``bash.exe`` the
command must be fed via stdin (``-s``) rather than argv (``-c``); we detect
that path shape and switch transport accordingly.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class ShellConfig:
    """Resolved shell configuration.

    ``shell_kind`` is ``"bash"`` when a real bash was found, ``"system"`` when
    we fell back to the platform default. Callers (e.g. prompt builder) use it
    to decide whether to inject platform hints.
    """

    shell: str
    args: tuple[str, ...]
    command_transport: str  # "argv" | "stdin"
    shell_kind: str         # "bash" | "system"


def _is_legacy_wsl_bash(path: str) -> bool:
    """True for the WSL launcher at System32.

    That binary can't take ``-c "<cmd>"``; commands must go via stdin (``-s``).
    """
    norm = path.replace("/", "\\").lower()
    return norm.startswith((
        r"c:\windows\system32\bash.exe",
        r"c:\windows\sysnative\bash.exe",
    ))


def _bash_shell_config(shell: str) -> ShellConfig:
    if _is_legacy_wsl_bash(shell):
        return ShellConfig(shell, ("-s",), "stdin", "bash")
    return ShellConfig(shell, ("-c",), "argv", "bash")


def _find_bash_on_path() -> str | None:
    """Locate ``bash`` on PATH."""
    found = shutil.which("bash") or shutil.which("bash.exe")
    if found and os.path.isfile(found):
        return found
    return None


def get_shell_config(custom_shell_path: str | None = None) -> ShellConfig:
    """Resolve the shell to use for bash-tool execution.

    Order:
      1. explicit ``custom_shell_path`` (if it exists)
      2. Git Bash in known Windows locations
      3. ``bash`` on PATH (Cygwin / MSYS2 / WSL / Unix)
      4. fallback system shell (cmd.exe / sh) with ``shell_kind="system"``
    """
    if custom_shell_path:
        if os.path.isfile(custom_shell_path):
            return _bash_shell_config(custom_shell_path)
        raise FileNotFoundError(f"Custom shell path not found: {custom_shell_path}")

    if sys.platform == "win32":
        # 2. Git Bash in known locations.
        candidates: list[str] = []
        pf = os.environ.get("ProgramFiles")
        if pf:
            candidates.append(os.path.join(pf, "Git", "bin", "bash.exe"))
        pf86 = os.environ.get("ProgramFiles(x86)")
        if pf86:
            candidates.append(os.path.join(pf86, "Git", "bin", "bash.exe"))
        for path in candidates:
            if os.path.isfile(path):
                return _bash_shell_config(path)

        # 3. bash on PATH (Cygwin, MSYS2, WSL).
        on_path = _find_bash_on_path()
        if on_path:
            return _bash_shell_config(on_path)

        # 4. Fallback: system shell (cmd.exe). Signal non-bash to callers.
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return ShellConfig(comspec, ("/c",), "argv", "system")

    # Unix: /bin/bash -> bash on PATH -> sh.
    if os.path.isfile("/bin/bash"):
        return _bash_shell_config("/bin/bash")
    on_path = _find_bash_on_path()
    if on_path:
        return _bash_shell_config(on_path)
    return ShellConfig("sh", ("-c",), "argv", "system")
