"""Operation identity and disk failure cases at the edit coordinator boundary."""
from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

import pytest

from agent_core import SessionManager
from agent_core.edit_recovery import EditRecovery
from agent_core.tools.edit import EditTool


@pytest.fixture
def operation(tmp_path: Path):
    target = tmp_path / "example.py"
    target.write_bytes(b"a = 1\n")
    manager = SessionManager.create(cwd=str(tmp_path), sessions_dir=tmp_path / "s", session_id="edit")
    manager.flush()
    tool = EditTool(cwd=str(tmp_path))
    params = {"path": target.name, "edits": [{"oldText": "a = 1", "newText": "a = 10"}]}
    return target, manager, tool, params


def test_completed_operation_replays_and_rejects_changed_parameters(operation):
    target, manager, tool, params = operation
    journal = EditRecovery(manager)
    result = asyncio.run(journal.execute(tool, "entry", "call", params))
    target.unlink()  # Neither replay nor rejection needs the current file.
    reopened = EditRecovery(manager)
    changed = {**params, "path": "missing.py"}
    rejected = asyncio.run(reopened.execute(tool, "entry", "call", changed))
    assert (rejected.status, rejected.details["code"]) == ("failed", "EDIT_OPERATION_MISMATCH")
    assert asyncio.run(reopened.execute(tool, "entry", "call", params)) == result
    assert not target.exists()


def test_journal_prepare_failure_leaves_original_bytes(operation, monkeypatch):
    target, manager, tool, params = operation
    journal = EditRecovery(manager)

    def fail_insert(*_):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(journal, "_insert", fail_insert)
    result = asyncio.run(journal.execute(tool, "entry", "call", params))
    assert (result.status, result.details["code"]) == ("failed", "EDIT_PREPARE_FAILED")
    assert target.read_bytes() == b"a = 1\n"
    assert journal.peek("entry", "call") is None


def test_missing_journal_does_not_get_created_by_retry_or_history(operation):
    target, manager, tool, params = operation
    journal = EditRecovery(manager)
    assert journal.peek("entry", "call") is None
    assert asyncio.run(journal.recover(tool, "entry", "call", params, retry=True)) is None
    assert journal.path is not None and not journal.path.exists()
    assert target.read_bytes() == b"a = 1\n"


def test_unreadable_journal_is_uncertain_and_preserves_file(operation):
    target, manager, tool, params = operation
    journal = EditRecovery(manager)
    assert journal.path is not None
    journal.path.write_bytes(b"not a database")
    result = asyncio.run(journal.recover(tool, "entry", "call", params, retry=True))
    assert (result.status, result.details["code"]) == ("uncertain", "EDIT_RECOVERY_UNAVAILABLE")
    assert journal.path.read_bytes() == b"not a database"
    assert target.read_bytes() == b"a = 1\n"


@pytest.mark.parametrize("change", ["deleted", "replaced-with-original-bytes", "already-expected"])
def test_prepared_edit_checks_file_identity_before_retry(operation, monkeypatch, change):
    target, manager, tool, params = operation
    journal = EditRecovery(manager)

    def interrupted(_prepared):
        raise OSError("injected interruption at commit")

    with monkeypatch.context() as fault:
        fault.setattr(tool, "commit_edit", interrupted)
        pending = asyncio.run(journal.execute(tool, "entry", "call", params))
    assert pending.status == "uncertain"
    duplicate = asyncio.run(journal.execute(tool, "entry", "call", params))
    assert duplicate.status == "uncertain" and duplicate.details["retryable"]
    assert target.read_bytes() == b"a = 1\n"
    if change == "deleted":
        target.unlink()
    elif change == "replaced-with-original-bytes":
        # Match both the bytes and timestamp; the new file has its own identity.
        before = target.stat()
        other = target.with_suffix(".new")
        other.write_bytes(target.read_bytes())
        os.utime(other, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(other, target)
    else:
        target.write_bytes(b"a = 10\n")
        os.utime(target, (1_600_000_000, 1_600_000_000))
    mtime = target.stat().st_mtime_ns if target.exists() else None
    reopened = EditRecovery(manager)
    result = asyncio.run(reopened.recover(tool, "entry", "call", params, retry=True))
    assert reopened.peek("entry", "call") == result
    if change == "already-expected":
        assert result.status == "completed"
        assert target.read_bytes() == b"a = 10\n"
    else:
        assert (result.status, result.details["code"]) == ("failed", "EDIT_CONFLICT")
    assert (target.stat().st_mtime_ns if target.exists() else None) == mtime
