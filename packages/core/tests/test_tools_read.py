"""Read-tool output compatibility and bounded line iteration."""
from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path

import pytest

from agent_core.tools.read import ReadTool


def _legacy_output(content: str, offset: float | None, limit: int | None, max_lines: int) -> str:
    lines = StringIO(content, newline=None).readlines()
    start = int(offset) - 1 if offset is not None and offset > 0 else 0
    lines = lines[start:]
    cap = int(limit) if limit is not None else max_lines
    truncated = len(lines) > cap
    text = "\n".join(f"{start + i + 1:>6}\t{line.rstrip()}" for i, line in enumerate(lines[:cap]))
    if truncated:
        text += f"\n\n... ({cap} lines shown; use offset={start + cap + 1} to continue)"
    return text or "(empty file)"


@pytest.mark.parametrize("offset", [None, -2, 0, 0.5, 1, 2, 4, 99, 10**30])
@pytest.mark.parametrize("limit", [None, -3, -1, 0, 1, 2, 9, 10**30])
def test_read_preserves_offset_limit_output(tmp_path: Path, offset: float | None, limit: int | None):
    content = "第一行\r\nsecond \t\r\n\r\nlast"
    (tmp_path / "text.txt").write_bytes(content.encode("utf-8"))
    tool = ReadTool(cwd=str(tmp_path), max_lines=3)

    result = asyncio.run(tool.execute("id", {"path": "text.txt", "offset": offset, "limit": limit}))

    assert result.content[0].text == _legacy_output(content, offset, limit, 3)


@pytest.mark.parametrize("offset,limit,expected_reads", [(1, 1, 2), (3, 2, 5), (1, 0, 1)])
def test_read_only_iterates_requested_window_and_lookahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offset: int, limit: int, expected_reads: int,
):
    (tmp_path / "text.txt").touch()
    consumed: list[str] = []

    class CountingFile(StringIO):
        def __next__(self):
            line = super().__next__()
            consumed.append(line)
            return line

        def readlines(self, *args, **kwargs):
            pytest.fail("Bounded reads must not load the whole file")

    monkeypatch.setattr(
        "agent_core.tools.read.open", lambda *args, **kwargs: CountingFile("line\n" * 1000), raising=False,
    )
    tool = ReadTool(cwd=str(tmp_path))

    result = asyncio.run(tool.execute("id", {"path": "text.txt", "offset": offset, "limit": limit}))

    assert len(consumed) == expected_reads
    assert f"{limit} lines shown" in result.content[0].text


@pytest.mark.parametrize("content", [b"", b"one\r\nbad\xff\n"])
def test_read_empty_and_invalid_utf8(tmp_path: Path, content: bytes):
    (tmp_path / "text.txt").write_bytes(content)
    result = asyncio.run(ReadTool(cwd=str(tmp_path)).execute("id", {"path": "text.txt"}))
    assert result.content[0].text == _legacy_output(content.decode("utf-8", errors="replace"), None, None, 2000)


@pytest.mark.parametrize("path", ["missing.txt", "."])
def test_read_rejects_missing_file_and_directory(tmp_path: Path, path: str):
    with pytest.raises(FileNotFoundError, match="File not found"):
        asyncio.run(ReadTool(cwd=str(tmp_path)).execute("id", {"path": path}))
