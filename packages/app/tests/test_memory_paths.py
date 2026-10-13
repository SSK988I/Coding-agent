from __future__ import annotations

from pathlib import Path
import hashlib
import subprocess
from types import SimpleNamespace

from coding_agent.memory.paths import _sanitize_remote, paths_for, resolve_project_id
from coding_agent.memory.types import MemoryIdentity


def test_remote_identity_removes_credentials_and_normalizes_transport() -> None:
    https = _sanitize_remote("https://user:secret@GitHub.com/Org/Repo.git")
    ssh = _sanitize_remote("git@github.com:Org/Repo.git")

    assert https == ssh == "github.com/Org/Repo"
    assert "user" not in https
    assert "secret" not in https


def test_identity_values_cannot_escape_memory_root(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    paths = paths_for(
        MemoryIdentity("../../other-user", "../../../other-project"),
        "project",
        root=root,
    )

    assert paths.directory.is_relative_to(root.resolve())
    assert ".." not in paths.directory.parts
    assert "other-user" not in str(paths.directory)
    assert "other-project" not in str(paths.directory)


def test_project_identity_git_probes_do_not_inherit_rpc_input(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def git_probe(argv, **kwargs):
        calls.append(argv)
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["capture_output"] is True
        assert 0 < kwargs["timeout"] <= 3
        output = str(tmp_path) if argv[-1] == "--show-toplevel" else "https://github.com/owner/repo.git"
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(subprocess, "run", git_probe)
    identity = resolve_project_id(tmp_path)

    assert identity == "sha256:" + hashlib.sha256(b"git:github.com/owner/repo").hexdigest()
    assert [argv[3:] for argv in calls] == [
        ["rev-parse", "--show-toplevel"], ["config", "--get", "remote.origin.url"],
    ]
