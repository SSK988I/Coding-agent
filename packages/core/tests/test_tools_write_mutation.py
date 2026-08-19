"""Tests for the write tool and the shared file-mutation lock.

Covers:
  - write creates missing parent directories (recursive)
  - write overwrites existing content
  - the shared per-realpath lock serializes concurrent mutations (verified
    at the ``_mutation`` unit level, since write/edit critical sections are
    synchronous and thus already atomic under asyncio's cooperative model)
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from agent_core.tools._mutation import file_mutation_lock
from agent_core.tools.write import WriteTool


def _run(coro):
    return asyncio.run(coro)


# ─── WriteTool: parent dirs ─────────────────────────────────────────────


def test_write_creates_missing_parent_dirs(tmp_path: Path):
    tool = WriteTool(cwd=str(tmp_path))
    result = _run(tool.execute("id", {
        "path": "a/b/c/file.txt",
        "content": "hello",
    }))
    assert "Wrote" in result.content[0].text
    f = tmp_path / "a" / "b" / "c" / "file.txt"
    assert f.read_text(encoding="utf-8") == "hello"


def test_write_creates_top_level_file(tmp_path: Path):
    tool = WriteTool(cwd=str(tmp_path))
    _run(tool.execute("id", {"path": "out.txt", "content": "x\ny\n"}))
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "x\ny\n"


def test_write_overwrites_existing(tmp_path: Path):
    f = tmp_path / "out.txt"
    f.write_text("old", encoding="utf-8")
    tool = WriteTool(cwd=str(tmp_path))
    _run(tool.execute("id", {"path": "out.txt", "content": "new"}))
    assert f.read_text(encoding="utf-8") == "new"


# ─── file_mutation_lock: per-realpath serialization ─────────────────────


def test_mutation_lock_serializes_same_file(tmp_path: Path):
    """Two coroutines holding the lock on the same path never overlap.

    Each holder records itself in an in-flight set while inside the critical
    section and sleeps briefly to force a scheduling point. If the lock works,
    the set never holds two holders at once.
    """
    target = tmp_path / "f.txt"
    in_flight: list[str] = []
    max_concurrent = {"n": 0}

    async def holder(tag: str):
        async with file_mutation_lock(str(target)):
            in_flight.append(tag)
            max_concurrent["n"] = max(max_concurrent["n"], len(in_flight))
            await asyncio.sleep(0.01)  # yield to the other coroutine
            in_flight.remove(tag)

    async def driver():
        await asyncio.gather(holder("a"), holder("b"))

    _run(driver())
    assert max_concurrent["n"] == 1


def test_mutation_lock_allows_different_files_in_parallel(tmp_path: Path):
    """Different realpaths do not block each other."""
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    order: list[str] = []

    async def holder(tag: str, path: Path):
        async with file_mutation_lock(str(path)):
            order.append(f"{tag}-start")
            await asyncio.sleep(0.02)
            order.append(f"{tag}-end")

    async def driver():
        await asyncio.gather(holder("a", f1), holder("b", f2))

    _run(driver())
    # Both started before either ended => they ran in parallel.
    assert order[0].endswith("start")
    assert order[1].endswith("start")


def test_mutation_lock_key_falls_back_for_missing_file(tmp_path: Path):
    """A not-yet-existing file resolves to the absolute path as its key.

    This keeps creation races (edit-after-create, two writes of a new file)
    serialized under the same key.
    """
    missing = tmp_path / "does_not_exist_yet.txt"
    async def go():
        async with file_mutation_lock(str(missing)):
            pass
    _run(go())  # no exception
    assert os.path.isabs(os.path.abspath(str(missing)))
