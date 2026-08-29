"""Bash streaming, timeout, and cancellation regression tests."""
from __future__ import annotations

import asyncio

import pytest

from agent_core.tools.bash import BashTool


def test_bash_execute_streams_partial_output(tmp_path) -> None:
    updates = []
    tool = BashTool(cwd=str(tmp_path))

    result = asyncio.run(tool.execute(
        "call-1",
        {"command": "printf streamed-output"},
        on_update=updates.append,
    ))

    assert result.content[0].text == "streamed-output"
    assert updates
    assert "streamed-output" in updates[-1].content[0].text


def test_bash_does_not_inherit_sidecar_stdin(tmp_path) -> None:
    tool = BashTool(cwd=str(tmp_path))

    result = asyncio.run(tool.execute(
        "call-1",
        {"command": "read value; printf stdin-closed", "timeout": 2},
    ))

    assert result.content[0].text == "stdin-closed"


def test_bash_timeout_covers_process_startup(tmp_path, monkeypatch) -> None:
    tool = BashTool(cwd=str(tmp_path))

    async def slow_spawn(_command):
        await asyncio.sleep(10)

    monkeypatch.setattr(tool, "_spawn", slow_spawn)

    with pytest.raises(RuntimeError, match="timed out"):
        asyncio.run(tool.execute(
            "call-1",
            {"command": "slow command", "timeout": 0.01},
        ))


def test_bash_honors_agent_abort_signal(tmp_path, monkeypatch) -> None:
    class BlockingOutput:
        async def read(self, _size):
            await asyncio.sleep(10)
            return b""

    class FakeProcess:
        stdout = BlockingOutput()
        returncode = None

        async def wait(self):
            return self.returncode

    async def exercise() -> None:
        tool = BashTool(cwd=str(tmp_path))
        process = FakeProcess()
        terminated = asyncio.Event()

        async def spawn(_command):
            return process

        async def terminate(_process):
            process.returncode = 1
            terminated.set()

        monkeypatch.setattr(tool, "_spawn", spawn)
        monkeypatch.setattr(tool, "_terminate_process_tree", terminate)
        abort = asyncio.Event()
        abort.set()
        with pytest.raises(RuntimeError, match="Operation aborted"):
            await tool.execute(
                "call-1", {"command": "blocked command"}, signal=abort,
            )
        assert terminated.is_set()

    asyncio.run(exercise())
