"""Tests for shell-free, read-only Git tools."""
from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agent_core.tools._subprocess import find_in_path
from agent_core.tools.git import (
    GitDiffTool,
    GitLogTool,
    GitShowTool,
    GitStatusTool,
)

GIT = find_in_path("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="Git is not installed")


def _run(coro):
    return asyncio.run(coro)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert GIT is not None
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"})
    return subprocess.run(
        [GIT, *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.name", "Plan Tool Tests")
    _git(tmp_path, "config", "user.email", "plan-tools@example.invalid")
    (tmp_path / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "--quiet", "-m", "initial")
    return tmp_path


def test_structured_git_tools_return_expected_views(repository: Path) -> None:
    (repository / "tracked.txt").write_text("after\n", encoding="utf-8")

    status = _run(GitStatusTool(str(repository)).execute("status", {})).content[0].text
    log = _run(GitLogTool(str(repository)).execute("log", {"limit": 1})).content[0].text
    diff = _run(GitDiffTool(str(repository)).execute("diff", {"path": "tracked.txt"})).content[0].text
    show = _run(GitShowTool(str(repository)).execute("show", {"revision": "HEAD"})).content[0].text

    assert "tracked.txt" in status
    assert "initial" in log
    assert "-before" in diff and "+after" in diff
    assert "commit " in show and "initial" in show


@pytest.mark.parametrize(
    ("tool", "params", "message"),
    [
        (GitLogTool(), {"revision": "--all"}, "revision"),
        (GitDiffTool(), {"revision": "HEAD\n--output=x"}, "revision"),
        (GitShowTool(), {"revision": "HEAD:secret"}, "revision"),
        (GitStatusTool(), {"path": "-outside"}, "path"),
        (GitDiffTool(), {"path": "../outside"}, "path"),
        (GitShowTool(), {"path": "C:/outside"}, "path"),
        (GitLogTool(), {"limit": 201}, "limit"),
    ],
)
def test_structured_git_tools_reject_unsafe_inputs(tool, params: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _run(tool.execute("unsafe", params))


def _helper_command(helper: Path) -> str:
    # Git executes configured helpers through its shell. POSIX quoting also
    # works with Git for Windows' bundled sh, and forward slashes avoid escape
    # interpretation in Windows paths.
    return shlex.join([Path(sys.executable).as_posix(), helper.as_posix()])


def _write_sentinel_helper(helper: Path, sentinel: Path) -> None:
    helper.write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('called', encoding='utf-8')\n"
        "print('helper output')\n",
        encoding="utf-8",
    )


def test_git_diff_disables_external_diff_driver(repository: Path) -> None:
    sentinel = repository / "external-diff-called"
    helper = repository / "external_diff.py"
    _write_sentinel_helper(helper, sentinel)
    _git(repository, "config", "diff.external", _helper_command(helper))
    (repository / "tracked.txt").write_text("changed\n", encoding="utf-8")

    _git(repository, "diff", "--ext-diff")
    assert sentinel.exists(), "control check: Git did not invoke configured diff.external"
    sentinel.unlink()

    _run(GitDiffTool(str(repository)).execute("diff", {}))
    assert not sentinel.exists()


def test_git_show_disables_textconv_helper(repository: Path) -> None:
    sentinel = repository / "textconv-called"
    helper = repository / "textconv.py"
    _write_sentinel_helper(helper, sentinel)
    (repository / ".gitattributes").write_text("*.txt diff=unsafe\n", encoding="utf-8")
    _git(repository, "config", "diff.unsafe.textconv", _helper_command(helper))
    (repository / "tracked.txt").write_text("after\n", encoding="utf-8")
    _git(repository, "add", ".gitattributes", "tracked.txt")
    _git(repository, "commit", "--quiet", "-m", "textconv target")

    _git(repository, "show", "--textconv", "HEAD")
    assert sentinel.exists(), "control check: Git did not invoke configured textconv"
    sentinel.unlink()

    _run(GitShowTool(str(repository)).execute("show", {"revision": "HEAD"}))
    assert not sentinel.exists()


class _FakeStream:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, _size: int) -> bytes:
        data, self._data = self._data, b""
        return data


class _FakeProcess:
    def __init__(self, stdout: bytes = b"ok", stderr: bytes = b"") -> None:
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self.returncode = 0

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class _HangingStream:
    async def read(self, _size: int) -> bytes:
        await asyncio.Event().wait()
        return b""


class _HangingProcess(_FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.stdout = _HangingStream()
        self.stderr = _HangingStream()
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        super().kill()


def test_git_runner_uses_fixed_argv_environment_and_output_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    async def fake_create(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakeProcess(stdout=b"0123456789")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    tool = GitDiffTool(str(tmp_path), git_path="trusted-git", max_bytes=5)
    result = _run(tool.execute("diff", {"revision": "HEAD", "path": "safe.txt"}))

    argv = captured["argv"]
    assert argv[:6] == (
        "trusted-git",
        "--no-pager",
        "--literal-pathspecs",
        "-c",
        "core.fsmonitor=false",
        "diff",
    )
    assert "--no-ext-diff" in argv
    assert "--no-textconv" in argv
    assert "--no-color" in argv
    assert argv[-2:] == ("--", "safe.txt")
    env = captured["kwargs"]["env"]
    assert env["GIT_PAGER"] == "cat"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert "output truncated at 5 bytes" in result.content[0].text


def test_git_runner_kills_process_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _HangingProcess()

    async def fake_create(*_argv, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    tool = GitStatusTool(str(tmp_path), git_path="trusted-git", timeout=0.01)
    with pytest.raises(RuntimeError, match="timed out"):
        _run(tool.execute("status", {}))
    assert process.killed is True
