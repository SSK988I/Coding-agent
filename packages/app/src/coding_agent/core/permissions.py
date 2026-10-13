"""Shared approval decisions for interactive clients. Plan policy runs first."""
from __future__ import annotations

from pathlib import Path
import os
import re
import shlex
from typing import Callable, Literal

PermissionMode = Literal["ask", "auto", "full_access"]
PERMISSION_MODES = {"ask", "auto", "full_access"}


def normalize_permission_mode(value: str) -> PermissionMode:
    aliases: dict[str, PermissionMode] = {
        "ask": "ask", "default": "ask", "off": "ask",
        "auto": "auto", "acceptedits": "auto",
        "full_access": "full_access", "bypasspermissions": "full_access", "on": "full_access",
    }
    mode = aliases.get(value.strip().casefold())
    if mode is None:
        raise ValueError("权限模式必须是 ask、auto 或 full_access（也支持 default、bypassPermissions、on、off）")
    return mode
_OBSERVATION_AND_CONTROL_TOOLS = {
    "read", "grep", "find", "ls", "git_status", "git_log", "git_diff", "git_show",
    "request_user_input", "submit_plan", "subagent_spawn", "subagent_status",
    "subagent_wait", "subagent_cancel",
}
_SCRIPT_COMMANDS = {"pytest", "ruff", "pyright", "mypy", "npm", "pnpm", "yarn", "uv"}


def requires_approval(
    mode: PermissionMode,
    tool_name: str,
    args: dict,
    workspace: Path | None,
    safe_shell: Callable[[str, str], bool],
) -> bool:
    if mode == "full_access":
        return False
    if tool_name in _OBSERVATION_AND_CONTROL_TOOLS:
        return False
    if tool_name in {"write", "edit"}:
        raw_path = args.get("path")
        if workspace is None or not isinstance(raw_path, str) or not raw_path:
            return True
        try:
            root = workspace.resolve()
            path = Path(raw_path)
            target = (path if path.is_absolute() else root / path).resolve()
            relative = target.relative_to(root)
        except (OSError, ValueError, RuntimeError):
            return True
        # Repository metadata and credentials deserve an explicit decision even
        # when they happen to sit inside the selected workspace.
        return any(part.casefold() in {".git", ".ssh", ".aws"} or part.casefold().startswith(".env") for part in relative.parts)
    if tool_name == "web_search":
        return mode == "ask"
    if tool_name == "bash":
        command = str(args.get("command", ""))
        # These forms can mutate or launch background work despite looking like
        # observation commands to the legacy shortcut classifier.
        if re.search(r"(?<![&>])&(?![&\d])", command) or any(
            flag in command for flag in ("--fix", "--unsafe-fixes", "-fprint", "--textconv", "--ext-diff")
        ):
            return True
        if re.search(r"\bgit\s+(?:remote\s+(?:remove|add|rename|set-url)|tag\s+(?:-d|--delete)|reset|clean|push)\b", command):
            return True
        if not safe_shell(command, str(workspace or Path.cwd())):
            return True
        if mode == "auto":
            return False
        # Scripts/tests/package commands can execute project code or access the
        # network. Strict mode does not silently inherit their old shortcuts.
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            return True
        return any(token.replace("\\", "/").rsplit("/", 1)[-1].casefold().removesuffix(".exe")
                   in _SCRIPT_COMMANDS for token in tokens)
    # Custom tools cannot gain automatic approval by omitting their metadata.
    return True

_DEFAULT_CONTROL_TOKENS = (";", "||", "`", "$(", ">", "<", "\n", "\r")
_DEFAULT_SHELL_SPLIT_RE = re.compile(r"\s*(?:\|\||&&|\|)\s*")
_DEFAULT_SAFE_FD_REDIRECTION_RE = re.compile(r"(?<!\S)(?:2>&1|1>&2)(?=\s|$)")
_DEFAULT_READ_COMMANDS = {
    "pwd", "ls", "dir", "cat", "head", "tail", "wc", "sort", "uniq",
    "cut", "grep", "rg", "fd", "find", "where", "which", "type",
    "get-content", "select-string", "get-childitem", "git",
}
_DEFAULT_READ_ONLY_GIT = {
    "status", "diff", "show", "log", "branch", "rev-parse", "ls-files",
    "ls-tree", "cat-file", "grep", "blame", "remote", "tag", "describe",
}
_DEFAULT_VALIDATION_EXECUTABLES = {"pytest", "ruff", "pyright", "mypy"}
_DEFAULT_SAFE_PACKAGE_SCRIPTS = {"test", "typecheck", "lint", "check", "build"}
_DEFAULT_DANGEROUS_FIND_FLAGS = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}


def _is_default_approval_safe_shell_command(command: str, cwd: str) -> bool:
    """Recognize the existing Default-mode approval shortcuts for both clients.

    Plan Mode no longer uses a shell allowlist: its runtime tool policy prevents
    Bash from being registered and blocks denied tools again before execution.
    This classifier only decides approval prompts; Plan Mode uses its separate
    runtime capability boundary.
    """
    command = _DEFAULT_SAFE_FD_REDIRECTION_RE.sub("", command.strip())
    if not command or any(token in command for token in _DEFAULT_CONTROL_TOKENS):
        return False
    if re.search(r"(?:^|\s)\.\.(?:[\\/]|(?:\s|$))", command):
        return False
    segments = _DEFAULT_SHELL_SPLIT_RE.split(command)
    if not segments or any(not segment.strip() for segment in segments):
        return False
    for segment in segments:
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            return False
        if not tokens or not _is_default_approval_safe_segment(tokens, cwd):
            return False
    return True


def _is_default_approval_safe_segment(tokens: list[str], cwd: str) -> bool:
    executable = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    args = tokens[1:]
    if executable == "cd":
        return len(args) == 1 and _default_path_stays_in_workspace(args[0], cwd)
    if executable == "git":
        positional = [item for item in args if not item.startswith("-")]
        if not positional or positional[0].casefold() not in _DEFAULT_READ_ONLY_GIT:
            return False
        if positional[0].casefold() == "branch" and len(positional) > 1:
            return False
        if any(
            item in {"-c", "-o", "--paginate"}
            or item.startswith(("--output", "--exec-path", "--open-files-in-pager"))
            for item in args
        ):
            return False
    elif executable in _DEFAULT_READ_COMMANDS:
        if executable == "find" and any(
            item.casefold() in _DEFAULT_DANGEROUS_FIND_FLAGS for item in args
        ):
            return False
        if executable == "rg" and any(
            item == "--pre" or item.startswith("--pre=") for item in args
        ):
            return False
        if executable == "sort" and any(
            item == "-o" or item.startswith("--output") for item in args
        ):
            return False
        if executable == "uniq" and len(
            [item for item in args if not item.startswith("-")]
        ) > 1:
            return False
    elif executable in _DEFAULT_VALIDATION_EXECUTABLES:
        pass
    elif executable in {"pnpm", "npm", "yarn"}:
        lowered = {item.casefold() for item in args if not item.startswith("-")}
        if (
            lowered.isdisjoint(_DEFAULT_SAFE_PACKAGE_SCRIPTS)
            or lowered & {"add", "install", "exec", "publish"}
        ):
            return False
    elif executable == "uv":
        if not _is_default_approval_safe_uv(args):
            return False
    else:
        return False
    return all(
        _default_path_stays_in_workspace(token, cwd)
        for token in args
        if _default_looks_like_path(token)
    )


def _is_default_approval_safe_uv(args: list[str]) -> bool:
    lowered = [item.casefold() for item in args if not item.startswith("-")]
    if not lowered:
        return False
    if lowered[0] == "build":
        return True
    if lowered[0] != "run" or len(lowered) < 2:
        return False
    if lowered[1] in _DEFAULT_VALIDATION_EXECUTABLES:
        return True
    return lowered[1:3] == ["python", "scripts/check_versions.py"]


def _default_looks_like_path(token: str) -> bool:
    if token.startswith("-"):
        return False
    return (
        "/" in token or "\\" in token or token in {".", ".."}
        or bool(re.match(r"^[A-Za-z]:", token))
    )


def _default_path_stays_in_workspace(token: str, cwd: str) -> bool:
    if any(char in token for char in "*?[]{}"):
        token = token.split("*", 1)[0].split("?", 1)[0] or "."
    try:
        root = Path(cwd).resolve()
        candidate = Path(token)
        resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        return os.path.commonpath((str(root), str(resolved))) == str(root)
    except (OSError, ValueError):
        return False


def is_approval_safe_shell_command(command: str, cwd: str) -> bool:
    """Recognize trusted observation/validation commands for approval shortcuts."""
    return _is_default_approval_safe_shell_command(command, cwd)
