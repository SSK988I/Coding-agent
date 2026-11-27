"""File integrity guarantees for the built-in edit tool."""
from __future__ import annotations

import asyncio
import builtins
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agent_core.tools.edit import EditTool
from agent_core.types import ToolExecutionError


CRASH_EXIT_CODE = 73


def _edit(target: Path):
    return asyncio.run(EditTool(cwd=str(target.parent)).execute("call-1", {
        "path": target.name,
        "edits": [{"oldText": "a = 1", "newText": "a = 10"}],
    }))


def _crash_before_commit(target: Path) -> None:
    # An in-place writer has already truncated the file when open() returns.
    # An atomic writer still has the old destination immediately before rename.
    real_open = builtins.open

    def open_then_interrupt(file, mode="r", *args, **kwargs):
        stream = real_open(file, mode, *args, **kwargs)
        if "w" in mode and not isinstance(file, int) and Path(file) == target:
            os._exit(CRASH_EXIT_CODE)
        return stream

    def interrupt_rename(event, args):
        # CPython emits os.rename for both os.rename() and os.replace().
        if event == "os.rename" and Path(os.fsdecode(args[1])) == target:
            os._exit(CRASH_EXIT_CODE)

    builtins.open = open_then_interrupt
    sys.addaudithook(interrupt_rename)
    _edit(target)


def test_interrupted_edit_preserves_original_file(tmp_path: Path):
    target = tmp_path / "source.py"
    original = b"\xef\xbb\xbfa = 1\r\n"
    target.write_bytes(original)

    child = subprocess.run(
        [sys.executable, __file__, str(target)], capture_output=True, text=True, timeout=30,
    )

    assert child.returncode == CRASH_EXIT_CODE, (child.stdout, child.stderr)
    assert target.read_bytes() == original


def test_edit_refuses_target_with_multiple_hard_links(tmp_path: Path):
    target = tmp_path / "source.py"
    alias = tmp_path / "alias.py"
    original = b"a = 1\n"
    target.write_bytes(original)
    try:
        alias.hardlink_to(target)
    except OSError as exc:
        pytest.skip(f"Hard links unavailable on the test filesystem: {exc}")

    try:
        result = _edit(target)
    except (OSError, RuntimeError, ValueError) as exc:
        reason = str(exc)
    else:
        assert result.status != "completed", "editing one of multiple hard links must be refused"
        reason = result.content[0].text

    assert "link" in reason.lower() or "链接" in reason
    assert target.samefile(alias)
    assert target.read_bytes() == original


def test_inspect_detects_file_replaced_by_directory(tmp_path: Path):
    target = tmp_path / "source.py"
    target.write_bytes(b"a = 1")
    tool = EditTool(str(tmp_path))
    tool.prepare_edit(target.name, [{"oldText": "1", "newText": "2"}])
    target.unlink()
    target.mkdir()
    with pytest.raises(ToolExecutionError) as error:
        tool.inspect_file(target.name)
    assert error.value.details == {"code": "EDIT_CONFLICT"}
    assert target.is_dir()


def test_symlink_edit_preserves_link_and_refuses_retargeting(tmp_path: Path):
    source = tmp_path / "source.py"
    other = tmp_path / "other.py"
    link = tmp_path / "link.py"
    source.write_bytes(b"a = 1")
    other.write_bytes(b"a = 10")
    try:
        link.symlink_to(source)
    except OSError as exc:
        pytest.skip(f"Cannot create a symbolic link on this filesystem: {exc}")

    assert _edit(link).status == "completed"
    assert link.is_symlink() and source.read_bytes() == b"a = 10"
    tool = EditTool(str(tmp_path))
    prepared = tool.prepare_edit(link.name, [{"oldText": "10", "newText": "20"}])
    link.unlink()
    link.symlink_to(other)
    with pytest.raises(ToolExecutionError) as error:
        tool.commit_edit(prepared)
    assert error.value.details == {"code": "EDIT_CONFLICT"}
    assert source.read_bytes() == other.read_bytes() == b"a = 10"


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable and group mode bits")
def test_edit_preserves_executable_mode(tmp_path: Path):
    target = tmp_path / "program"
    target.write_bytes(b"a = 1")
    target.chmod(0o750)
    assert _edit(target).status == "completed"
    assert stat.S_IMODE(target.stat().st_mode) == 0o750


if __name__ == "__main__":
    _crash_before_commit(Path(sys.argv[1]))
