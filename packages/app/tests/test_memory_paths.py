from __future__ import annotations

from pathlib import Path

from coding_agent.memory.paths import _sanitize_remote, paths_for
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
