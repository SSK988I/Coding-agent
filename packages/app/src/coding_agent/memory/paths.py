"""Identity-safe paths and project identity resolution for memory files."""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from coding_agent.core.config import get_memory_dir
from coding_agent.memory.types import MemoryIdentity, MemoryScope


@dataclass(frozen=True)
class MemoryPaths:
    directory: Path
    snapshot: Path
    events: Path
    lock: Path


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _storage_component(prefix: str, value: str) -> str:
    return f"{prefix}-{_digest(value)[:32]}"


def paths_for(
    identity: MemoryIdentity,
    scope: MemoryScope,
    *,
    root: Path | None = None,
) -> MemoryPaths:
    memory_root = (root or get_memory_dir()).expanduser().resolve()
    user_dir = memory_root / "users" / _storage_component("u", identity.user_id)
    if scope == "global":
        directory = user_dir
        snapshot = directory / "global.yaml"
    else:
        if not identity.project_id:
            raise ValueError("project scope requires project_id")
        directory = user_dir / "projects" / _storage_component("p", identity.project_id)
        snapshot = directory / "memory.yaml"
    return MemoryPaths(
        directory=directory,
        snapshot=snapshot,
        events=directory / "events.jsonl",
        lock=directory / ".memory.lock",
    )


def _run_git(cwd: Path, *args: str) -> str | None:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=False,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _sanitize_remote(remote: str) -> str:
    value = remote.strip()
    if "://" in value:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if parsed.port:
            host = f"{host}:{parsed.port}"
        path = re.sub(r"\.git$", "", parsed.path.rstrip("/"), flags=re.IGNORECASE)
        if host:
            return f"{host}/{path.lstrip('/')}"
        return f"{parsed.scheme.lower()}:{path}"
    scp_match = re.match(r"(?:[^@]+@)?([^:]+):(.+)", value)
    if scp_match:
        host, path = scp_match.groups()
        normalized = re.sub(r"\.git$", "", path.rstrip("/"), flags=re.IGNORECASE)
        return f"{host.lower()}/{normalized.lstrip('/')}"
    return re.sub(r"\.git$", "", value.rstrip("/"), flags=re.IGNORECASE)


def resolve_project_id(cwd: str | Path) -> str:
    workspace = Path(cwd).expanduser().resolve()
    root_value = _run_git(workspace, "rev-parse", "--show-toplevel")
    root = Path(root_value).resolve() if root_value else workspace
    remote = _run_git(root, "config", "--get", "remote.origin.url")
    basis = f"git:{_sanitize_remote(remote)}" if remote else f"path:{root.as_posix().casefold()}"
    return f"sha256:{_digest(basis)}"


__all__ = ["MemoryPaths", "paths_for", "resolve_project_id"]
