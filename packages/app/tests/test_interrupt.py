"""Tests for the interactive-mode interrupt model.

Verifies:
  - Esc while responding aborts the session.
  - Esc during compaction aborts the compaction.
  - Esc while idle does nothing (returns None).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coding_agent.modes.interactive.interactive_mode import InteractiveMode


class _FakeSession:
    def __init__(self):
        self.aborted = False
        self.compaction_aborted = False

    async def abort(self):
        self.aborted = True

    def abort_compaction(self):
        self.compaction_aborted = True


def _make_mode():
    """Build a minimal InteractiveMode-like object with the interrupt surface."""
    session = _FakeSession()
    obj = SimpleNamespace()
    obj._session = session
    obj._is_responding = False
    obj._active_status_indicator = None
    obj._last_sigint_time = 0.0
    obj._background_tasks = set()

    def _spawn(coro):
        # Drive the coroutine inline so the test observes the side effect.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return None
        task = asyncio.ensure_future(coro)
        obj._background_tasks.add(task)
        task.add_done_callback(obj._background_tasks.discard)
        return task

    obj._spawn = _spawn
    obj._on_escape = InteractiveMode._on_escape.__get__(obj, SimpleNamespace)
    return obj, session


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_escape_while_responding_aborts_session():
    mode, session = _make_mode()
    mode._is_responding = True
    # _on_escape schedules abort() via ensure_future, which needs a running loop.
    async def _driver():
        result = mode._on_escape()
        assert result is True
        # Yield so the scheduled abort() coroutine runs.
        await asyncio.sleep(0)
    asyncio.run(_driver())
    assert session.aborted is True


def test_escape_during_compaction_aborts_compaction():
    mode, session = _make_mode()
    mode._is_responding = False
    # Simulate an active compaction indicator.
    mode._active_status_indicator = SimpleNamespace(kind="compaction")
    result = mode._on_escape()
    assert result is True
    assert session.compaction_aborted is True


def test_escape_while_idle_does_nothing():
    mode, session = _make_mode()
    mode._is_responding = False
    mode._active_status_indicator = None
    result = mode._on_escape()
    assert result is None
    assert session.aborted is False
    assert session.compaction_aborted is False


def test_escape_during_working_does_not_abort_compaction():
    """A 'working' indicator (not compaction) should not trigger abort_compaction."""
    mode, session = _make_mode()
    mode._is_responding = False
    mode._active_status_indicator = SimpleNamespace(kind="working")
    result = mode._on_escape()
    assert result is None
    assert session.compaction_aborted is False
