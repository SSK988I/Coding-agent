"""Cross-process serialization of the built-in edit and write tools."""
from __future__ import annotations

import asyncio
import multiprocessing
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from agent_core.tools import edit
from agent_core.tools.write import WriteTool


def _run_writer(target: Path, channel: Connection) -> None:
    with channel:
        tool = WriteTool(cwd=str(target.parent))
        assert channel.recv() == "write"
        channel.send("starting")
        asyncio.run(tool.execute("writer", {"path": str(target), "content": "from write\n"}))
        channel.send("finished")


def test_write_in_another_process_waits_for_inflight_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "shared.txt"
    target.write_bytes(b"original\n")
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    writer = context.Process(target=_run_writer, args=(target, child))
    apply_edits = edit._apply_edits

    def pause_after_read(content, edits, path):
        replacement = apply_edits(content, edits, path)
        # The edit has read the old version and has yet to commit. An independent
        # writer must wait here, otherwise the edit will overwrite its result.
        parent.send("write")
        assert parent.poll(20) and parent.recv() == "starting"
        assert not parent.poll(2), "write finished while edit still held its original snapshot"
        return replacement

    monkeypatch.setattr(edit, "_apply_edits", pause_after_read)
    with parent, child:
        writer.start()
        child.close()
        try:
            asyncio.run(edit.EditTool(cwd=str(tmp_path)).execute("editor", {
                "path": target.name,
                "edits": [{"oldText": "original", "newText": "from edit"}],
            }))
            assert parent.poll(20) and parent.recv() == "finished"
            writer.join(20)
            assert writer.exitcode == 0
            assert target.read_bytes() == b"from write\n"
        finally:
            if writer.is_alive():
                writer.terminate()
                writer.join(10)
            writer.close()
