"""Archiving changes visibility while preserving the session and branch history."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from agent_llm import AssistantMessage, TextContent, UserMessage

from agent_core.session.session_manager import SessionManager
from agent_core.session.storage import dict_to_header, header_to_dict, read_header, rewrite_header_line, write_header_line
from agent_core.session.types import SessionHeader


CWD = "/archive/project"


def _saved(tmp_path: Path, text: str = "question") -> SessionManager:
    manager = SessionManager.create(cwd=CWD, agent_dir=tmp_path)
    manager.append_message(UserMessage(content=text))
    manager.append_message(AssistantMessage(content=[TextContent(text="answer")]))
    assert manager.path is not None and manager.path.exists()
    return manager


def _tail(path: Path) -> bytes:
    return path.read_bytes().split(b"\n", 1)[1]


def test_archive_and_restore_preserve_id_path_history_and_branch(tmp_path: Path):
    manager = _saved(tmp_path)
    manager.set_name("保留名称")
    manager.append_collaboration_mode_change("plan", plan_id="plan-1")
    revision = manager.append_plan_revision(
        plan_id="plan-1", revision=1, title="计划", markdown="实施步骤",
        digest="a" * 64, source_message_id="message-1",
    )
    manager.append_message(UserMessage(content="another branch"))
    manager.set_leaf_id(revision.id)
    manager.flush()
    path = manager.path
    assert path is not None
    session_id = manager.header.id
    entries = list(manager.entries)
    branch = manager.get_branch()
    context = manager.build_session_context()
    tail = _tail(path)
    original_log = path.read_bytes()

    manager.set_archived(True)
    assert manager.path == path and manager.header.id == session_id
    assert path.read_bytes() == original_log
    assert _tail(path) == tail
    reopened = SessionManager.open(path, agent_dir=tmp_path)
    assert reopened.header.archived is True
    assert reopened.entries == entries
    assert reopened.leaf_id == revision.id
    assert reopened.get_branch() == branch
    assert reopened.build_session_context() == context
    assert reopened.get_name() == "保留名称"

    reopened.set_archived(False)
    restored = SessionManager.open(path, agent_dir=tmp_path)
    assert restored.header.archived is False
    assert restored.header.id == session_id
    assert restored.path == path and _tail(path) == tail
    assert path.read_bytes() == original_log
    assert restored.get_branch() == branch


def test_list_filters_before_loading_history_and_supports_all(tmp_path: Path, monkeypatch):
    active = _saved(tmp_path, "active")
    archived = _saved(tmp_path, "archived")
    archived.set_archived(True)
    from agent_core.session import storage

    original = storage.read_entries
    scanned = []

    def record_read(path, **kwargs):
        scanned.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(storage, "read_entries", record_read)
    infos = SessionManager.list_sessions(cwd=CWD, agent_dir=tmp_path, archived=False)
    assert [info.id for info in infos] == [active.header.id]
    assert infos[0].archived is False
    assert scanned == [active.path]
    scanned.clear()
    infos = SessionManager.list_sessions(cwd=CWD, agent_dir=tmp_path, archived=True)
    assert [info.id for info in infos] == [archived.header.id]
    assert infos[0].archived is True
    assert scanned == [archived.path]
    all_infos = SessionManager.list_sessions(cwd=CWD, agent_dir=tmp_path, archived=None)
    assert {info.id for info in all_infos} == {active.header.id, archived.header.id}
    assert {info.id for info in SessionManager.list_sessions(cwd=CWD, agent_dir=tmp_path)} == {
        active.header.id, archived.header.id,
    }


def test_continue_recent_skips_archived_without_loading_its_entries(tmp_path: Path, monkeypatch):
    active = _saved(tmp_path, "active")
    archived = _saved(tmp_path, "archived")
    archived.set_archived(True)
    assert active.path is not None and archived.path is not None
    os.utime(active.path, (1, 1))
    os.utime(archived.path, (2, 2))
    from agent_core.session import session_manager

    original = session_manager.read_entries
    scanned = []

    def record_read(path, **kwargs):
        scanned.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(session_manager, "read_entries", record_read)
    recent = SessionManager.continue_recent(cwd=CWD, agent_dir=tmp_path)
    assert recent is not None and recent.header.id == active.header.id
    assert scanned == [active.path]
    active.set_archived(True)
    scanned.clear()
    assert SessionManager.continue_recent(cwd=CWD, agent_dir=tmp_path) is None
    assert scanned == []


def test_legacy_header_defaults_to_active(tmp_path: Path):
    manager = _saved(tmp_path)
    assert manager.path is not None
    raw_header = header_to_dict(manager.header)
    raw_header.pop("archived", None)
    manager.path.write_bytes(json.dumps(raw_header).encode() + b"\n" + _tail(manager.path))
    restored = SessionManager.open(manager.path)
    assert restored.header.archived is False
    assert [item.id for item in SessionManager.list_sessions(cwd=CWD, agent_dir=tmp_path, archived=False)] == [manager.header.id]


@pytest.mark.parametrize("invalid", [None, 0, 1, "false", "true", [], {}])
def test_invalid_archive_metadata_is_rejected(invalid):
    raw = header_to_dict(SessionHeader(id="id", timestamp="timestamp", cwd=CWD))
    raw["archived"] = invalid
    with pytest.raises(ValueError, match="boolean"):
        dict_to_header(raw)


@pytest.mark.parametrize("failure", ["replace", "fsync"])
@pytest.mark.parametrize("archived", [True, False])
def test_failed_archive_is_atomic_and_leaves_no_temporary_file(tmp_path: Path, monkeypatch, failure, archived):
    manager = _saved(tmp_path)
    manager.set_archived(not archived)
    assert manager.path is not None
    before = manager.path.read_bytes()
    header = manager.header
    entries = list(manager.entries)
    leaf = manager.leaf_id

    def fail(*_args):
        raise OSError("disk failure")

    monkeypatch.setattr(f"agent_core.session.storage.os.{failure}", fail)
    with pytest.raises(OSError, match="disk failure"):
        manager.set_archived(archived)
    assert manager.header is header and manager.header.archived is not archived
    assert manager.entries == entries and manager.leaf_id == leaf
    assert manager.path.read_bytes() == before
    persisted_header = read_header(manager.path)
    assert persisted_header is not None and persisted_header.archived is not archived
    assert list(manager.path.parent.glob("*.tmp")) == []
    assert list(manager.path.parent.glob(".*.tmp")) == []


def test_archive_does_not_materialize_empty_or_lose_buffered_entries(tmp_path: Path):
    manager = SessionManager.create(cwd=CWD, agent_dir=tmp_path)
    assert manager.path is not None
    manager.set_archived(True)
    assert not manager.path.exists()
    message = UserMessage(content="buffered question")
    manager.append_message(message)
    manager.set_archived(False)
    manager.set_archived(True)
    assert not manager.path.exists()
    manager.flush()
    restored = SessionManager.open(manager.path)
    assert restored.header.archived is True
    assert restored.entries == manager.entries
    assert restored.build_session_context().messages == [message]


def test_in_memory_archive_is_reversible_without_touching_disk(tmp_path: Path):
    manager = SessionManager.create(cwd=CWD, agent_dir=tmp_path, in_memory=True)
    manager.append_message(UserMessage(content="memory only"))
    manager.set_archived(True)
    manager.flush()
    assert manager.header.archived is True
    assert list(tmp_path.iterdir()) == []
    manager.set_archived(False)
    assert manager.header.archived is False


@pytest.mark.parametrize("invalid", [0, 1, None, "false"])
def test_archive_mutation_requires_boolean(tmp_path: Path, invalid):
    manager = _saved(tmp_path)
    with pytest.raises(ValueError, match="boolean"):
        manager.set_archived(invalid)


def test_repeated_archive_does_not_rewrite_the_header(tmp_path: Path, monkeypatch):
    manager = _saved(tmp_path)
    manager.set_archived(True)

    def unexpected(*_args):
        raise AssertionError("unchanged archive state must not touch the file")

    monkeypatch.setattr("agent_core.session.session_manager.write_archive_state", unexpected)
    manager.set_archived(True)


def test_archive_uses_short_sibling_temp_for_valid_long_windows_path(tmp_path: Path, monkeypatch):
    from agent_core.session import storage

    parent = tmp_path / ("deep-" + "x" * max(1, 185 - len(str(tmp_path)) - 6))
    path = parent / "2026-10-25T13-18-03-696Z_01a17336-09b0-7212-97a7-c6c87396fb13.jsonl"
    assert len(str(path)) < 260
    header = SessionHeader(id="saved-session", timestamp="2026-10-25T13:18:03Z", cwd=CWD)
    write_header_line(path, header)
    with path.open("ab") as target:
        target.write(b'{"preserved":"entry bytes"}\r\n')
    tail = _tail(path)
    mkstemp = storage.tempfile.mkstemp

    def windows_mkstemp(*, prefix, suffix, dir):
        # Apply the real Windows constraint on every platform, including CI.
        if len(str(Path(dir) / f"{prefix}{'x' * 8}{suffix}")) >= 260:
            raise FileNotFoundError("Windows MAX_PATH exceeded")
        return mkstemp(prefix=prefix, suffix=suffix, dir=dir)

    monkeypatch.setattr(storage.tempfile, "mkstemp", windows_mkstemp)
    header.archived = True
    rewrite_header_line(path, header)
    archived_header = read_header(path)
    assert archived_header is not None and archived_header.archived is True
    assert _tail(path) == tail
    assert list(parent.glob(".*.tmp")) == []


@pytest.mark.parametrize("archived", [False, True, None])
def test_list_rejects_other_workspace_when_directory_encoding_collides(tmp_path: Path, archived):
    from agent_core.session.ids import encode_cwd

    cwd, foreign_cwd = "/archive/project/nested", "/archive/project-nested"
    assert encode_cwd(cwd) == encode_cwd(foreign_cwd)
    managers = []
    for workspace in (cwd, foreign_cwd):
        manager = SessionManager.create(cwd=workspace, agent_dir=tmp_path)
        manager.append_message(AssistantMessage())
        manager.set_archived(archived is True)
        managers.append(manager)
    assert [info.id for info in SessionManager.list_sessions(
        cwd=cwd, agent_dir=tmp_path, archived=archived,
    )] == [managers[0].header.id]


def test_recent_rejects_other_workspace_when_directory_encoding_collides(tmp_path: Path):
    cwd, foreign_cwd = "/archive/project/nested", "/archive/project-nested"
    current = SessionManager.create(cwd=cwd, agent_dir=tmp_path)
    foreign = SessionManager.create(cwd=foreign_cwd, agent_dir=tmp_path)
    for manager, modified in ((current, 1), (foreign, 2)):
        manager.append_message(AssistantMessage())
        assert manager.path is not None
        os.utime(manager.path, (modified, modified))
    recent = SessionManager.continue_recent(cwd=cwd, agent_dir=tmp_path)
    assert recent is not None and recent.header.id == current.header.id
    current.set_archived(True)
    assert SessionManager.continue_recent(cwd=cwd, agent_dir=tmp_path) is None


def test_workspace_filter_accepts_normalized_equivalent_cwd(tmp_path: Path):
    manager = SessionManager.create(cwd="/archive/project/../project", agent_dir=tmp_path)
    manager.append_message(AssistantMessage())
    assert [info.id for info in SessionManager.list_sessions(cwd=CWD, agent_dir=tmp_path, archived=False)] == [manager.header.id]
    recent = SessionManager.continue_recent(cwd=CWD, agent_dir=tmp_path)
    assert recent is not None and recent.header.id == manager.header.id


def test_archive_preserves_an_append_from_another_open_manager(tmp_path: Path, monkeypatch):
    from agent_core.session import storage

    manager = _saved(tmp_path)
    path = manager.path
    assert path is not None
    other = SessionManager.open(path)
    original_replace = storage.os.replace
    appended_message = UserMessage(content="concurrent append sentinel")

    def append_then_replace(source, target):
        # The other writer appends after archive preparation and immediately
        # before the atomic visibility update. Its committed line must survive.
        other.append_message(appended_message)
        assert b"concurrent append sentinel" in path.read_bytes()
        original_replace(source, target)

    monkeypatch.setattr(storage.os, "replace", append_then_replace)
    manager.set_archived(True)
    restored = SessionManager.open(path)
    assert restored.header.archived is True
    assert restored.build_session_context().messages[-1] == appended_message
    assert restored.entries == other.entries


def test_false_sidecar_overrides_a_legacy_archived_header(tmp_path: Path):
    manager = _saved(tmp_path)
    assert manager.path is not None
    manager.header.archived = True
    rewrite_header_line(manager.path, manager.header)
    original_log = manager.path.read_bytes()
    assert json.loads(original_log.split(b"\n", 1)[0])["archived"] is True
    reopened = SessionManager.open(manager.path)
    reopened.set_archived(False)
    assert manager.path.read_bytes() == original_log
    assert SessionManager.open(manager.path).header.archived is False
    assert [info.id for info in SessionManager.list_sessions(cwd=CWD, agent_dir=tmp_path, archived=False)] == [manager.header.id]


def test_archive_metadata_is_not_a_session_and_uses_a_short_stable_name(tmp_path: Path):
    from agent_core.session.storage import _archive_state_path

    manager = _saved(tmp_path)
    assert manager.path is not None
    manager.set_archived(True)
    metadata = _archive_state_path(manager.path)
    assert metadata.parent == manager.path.parent
    assert metadata.name.startswith(".archive-") and metadata.suffix == ".json"
    assert len(metadata.name) < len(manager.path.name)
    assert json.loads(metadata.read_text(encoding="utf-8")) is True
    assert _archive_state_path(tmp_path / manager.path.name).name == metadata.name
    manager.path.unlink()
    assert metadata.exists()
    assert SessionManager.list_sessions(cwd=CWD, agent_dir=tmp_path, archived=None) == []
    assert SessionManager.continue_recent(cwd=CWD, agent_dir=tmp_path) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows filenames are case insensitive")
def test_archive_state_uses_the_same_identity_for_windows_filename_case_variants(tmp_path: Path):
    manager = _saved(tmp_path)
    assert manager.path is not None
    manager.set_archived(True)
    variant = manager.path.with_name(manager.path.name.upper())
    assert variant.is_file()
    reopened = SessionManager.open(variant)
    assert reopened.header.archived is True
    reopened.set_archived(False)
    assert SessionManager.open(manager.path).header.archived is False


def test_archive_sidecar_handles_a_valid_long_windows_path(tmp_path: Path, monkeypatch):
    from agent_core.session import storage

    parent = tmp_path / ("deep-" + "x" * max(1, 185 - len(str(tmp_path)) - 6))
    path = parent / "2026-10-25T13-18-03-696Z_01a17336-09b0-7212-97a7-c6c87396fb13.jsonl"
    assert len(str(path)) < 260
    header = SessionHeader(id="saved-session", timestamp="2026-10-25T13:18:03Z", cwd=CWD)
    write_header_line(path, header)
    original_log = path.read_bytes()
    mkstemp = storage.tempfile.mkstemp

    def windows_mkstemp(*, prefix, suffix, dir):
        if len(str(Path(dir) / f"{prefix}{'x' * 8}{suffix}")) >= 260:
            raise FileNotFoundError("Windows MAX_PATH exceeded")
        return mkstemp(prefix=prefix, suffix=suffix, dir=dir)

    monkeypatch.setattr(storage.tempfile, "mkstemp", windows_mkstemp)
    storage.write_archive_state(path, True)
    assert len(str(storage._archive_state_path(path))) < 260
    restored = read_header(path)
    assert restored is not None and restored.archived is True
    assert path.read_bytes() == original_log
    storage.write_archive_state(path, False)
    restored = read_header(path)
    assert restored is not None and restored.archived is False
    assert list(parent.glob(".*.tmp")) == []


@pytest.mark.parametrize("invalid", [None, 1, "true", {}, []])
def test_invalid_sidecar_state_does_not_resume_a_possibly_archived_session(tmp_path: Path, invalid):
    from agent_core.session.storage import _archive_state_path

    manager = _saved(tmp_path)
    assert manager.path is not None
    _archive_state_path(manager.path).write_text(json.dumps(invalid), encoding="utf-8")
    assert read_header(manager.path) is None
    assert SessionManager.continue_recent(cwd=CWD, agent_dir=tmp_path) is None
