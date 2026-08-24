from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path

import pytest
import yaml

from coding_agent.memory.retriever import MemoryRetriever
from coding_agent.memory.store import FileMemoryStore, MemoryStoreError, _canonical_hash
from coding_agent.memory.types import MemoryCandidate, MemoryIdentity, MemorySnapshot


def _run(value):
    return asyncio.run(value)


def _candidate(
    key: str,
    value: object,
    *,
    source_hash: str,
    authority: str = "explicit_user",
    scope: str = "project",
    timestamp: str = "2026-08-24T10:00:00Z",
) -> MemoryCandidate:
    return MemoryCandidate(
        operation="upsert",
        kind="preference" if key.startswith("response.") else "decision",
        scope=scope,  # type: ignore[arg-type]
        key=key,
        value=value,
        summary=f"{key} uses {value}",
        confidence=1.0,
        source_kind=authority,  # type: ignore[arg-type]
        evidence_entry_ids=[f"entry-{source_hash}"],
        source_timestamp=timestamp,
        source_hash=source_hash,
    )


def _apply_in_process(root: str, key: str, source_hash: str) -> str:
    store = FileMemoryStore(Path(root))
    identity = MemoryIdentity("process-user", "process-project")
    result = asyncio.run(store.apply(
        identity,
        _candidate(key, source_hash, source_hash=source_hash),
        session_id=source_hash,
    ))
    return result.action


def test_store_writes_fact_log_and_rebuildable_yaml(tmp_path: Path) -> None:
    store = FileMemoryStore(tmp_path / "memory")
    identity = MemoryIdentity("user-a", "project-a")
    result = _run(store.apply(
        identity,
        _candidate("tooling.package_manager", "pnpm", source_hash="source-1"),
        session_id="session-1",
    ))

    assert result.action == "created"
    snapshot = _run(store.load(identity, "project"))
    assert snapshot.memories["tooling.package_manager"].value == "pnpm"
    assert snapshot.revision == 1

    project_dir = next((tmp_path / "memory" / "users").glob("*/projects/*"))
    yaml_path = project_dir / "memory.yaml"
    events_path = project_dir / "events.jsonl"
    assert yaml_path.exists() and events_path.exists()
    assert yaml.safe_load(yaml_path.read_text(encoding="utf-8"))["content_hash"].startswith("sha256:")
    assert "memory_created" in events_path.read_text(encoding="utf-8")

    yaml_path.write_text("broken: [", encoding="utf-8")
    rebuilt = _run(store.load(identity, "project"))
    assert rebuilt.memories["tooling.package_manager"].value == "pnpm"
    assert yaml.safe_load(yaml_path.read_text(encoding="utf-8"))["revision"] == 1
    assert list(project_dir.glob("memory.corrupt-*.yaml"))


def test_duplicate_conflict_correction_and_tombstone(tmp_path: Path) -> None:
    store = FileMemoryStore(tmp_path / "memory")
    identity = MemoryIdentity("user-a", "project-a")
    first = _candidate("tooling.package_manager", "npm", source_hash="source-1")
    assert _run(store.apply(identity, first, session_id="s")).action == "created"
    assert _run(store.apply(identity, first, session_id="s")).action == "duplicate"

    conflicting = _candidate(
        "tooling.package_manager", "pnpm", source_hash="source-2",
        timestamp="2026-08-24T11:00:00Z",
    )
    assert _run(store.apply(identity, conflicting, session_id="s")).action == "conflict"
    snapshot = _run(store.load(identity, "project"))
    assert "tooling.package_manager" not in snapshot.memories
    assert len(snapshot.conflicts["tooling.package_manager"].candidates) == 2

    correction = _candidate(
        "tooling.package_manager", "pnpm", source_hash="source-3",
        authority="explicit_correction", timestamp="2026-08-24T12:00:00Z",
    )
    assert _run(store.apply(identity, correction, session_id="s")).action == "conflict_resolved"
    corrected = _run(store.load(identity, "project")).memories["tooling.package_manager"]
    assert corrected.value == "pnpm"
    assert corrected.authority == "explicit_correction"
    assert corrected.source_hash == "source-3"

    assert _run(store.forget(
        identity,
        scope="project",
        key="tooling.package_manager",
        kind="decision",
        session_id="s",
        source_hash="forget-1",
    )).action == "tombstoned"
    old_replay = _candidate(
        "tooling.package_manager", "npm", source_hash="old-replay",
        timestamp="2025-01-01T00:00:00Z",
    )
    assert _run(store.apply(identity, old_replay, session_id="old")).action == "rejected"
    snapshot = _run(store.load(identity, "project"))
    assert "tooling.package_manager" not in snapshot.memories
    assert "tooling.package_manager" in snapshot.tombstones


def test_global_project_precedence_and_user_isolation(tmp_path: Path) -> None:
    store = FileMemoryStore(tmp_path / "memory")
    user_a = MemoryIdentity("user-a", "project-a")
    user_b = MemoryIdentity("user-b", "project-a")
    _run(store.apply(
        user_a,
        _candidate("response.language", "zh-CN", source_hash="global", scope="global"),
        session_id="s",
    ))
    _run(store.apply(
        user_a,
        _candidate("response.language", "en-US", source_hash="project", scope="project"),
        session_id="s",
    ))
    _run(store.apply(
        user_a,
        _candidate("tooling.package_manager", "pnpm", source_hash="project-package"),
        session_id="s",
    ))

    context = _run(MemoryRetriever(store).retrieve(user_a, "language"))
    assert len([item for item in context.records if item.key == "response.language"]) == 1
    assert next(item for item in context.records if item.key == "response.language").value == "en-US"
    assert _run(MemoryRetriever(store).retrieve(user_b, "language")).records == []

    other_project = MemoryIdentity("user-a", "project-b")
    other_context = _run(MemoryRetriever(store).retrieve(other_project, "package manager"))
    assert all(item.key != "tooling.package_manager" for item in other_context.records)


def test_event_written_before_snapshot_failure_is_recovered(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "memory"
    store = FileMemoryStore(root)
    identity = MemoryIdentity("user-a", "project-a")

    def fail_snapshot(*_args, **_kwargs) -> None:
        raise OSError("simulated crash before snapshot replace")

    monkeypatch.setattr(store, "_write_snapshot", fail_snapshot)
    with pytest.raises(OSError, match="simulated crash"):
        _run(store.apply(
            identity,
            _candidate("tooling.package_manager", "pnpm", source_hash="crash-event"),
            session_id="session-crash",
        ))

    recovered = _run(FileMemoryStore(root).load(identity, "project"))
    assert recovered.revision == 1
    assert recovered.memories["tooling.package_manager"].value == "pnpm"


def test_snapshot_ahead_of_fact_log_is_backed_up_and_safely_reset(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    store = FileMemoryStore(root)
    identity = MemoryIdentity("user-a", "project-a")
    _run(store.apply(
        identity,
        _candidate("tooling.package_manager", "pnpm", source_hash="source-1"),
        session_id="s",
    ))
    project_dir = next((root / "users").glob("*/projects/*"))
    (project_dir / "events.jsonl").write_text("", encoding="utf-8")

    recovered = _run(FileMemoryStore(root).load(identity, "project"))

    assert recovered.revision == 0
    assert recovered.memories == {}
    assert list(project_dir.glob("memory.corrupt-*.yaml"))
    assert yaml.safe_load((project_dir / "memory.yaml").read_text(encoding="utf-8"))["revision"] == 0


def test_invalid_snapshot_schema_is_backed_up_and_rebuilt(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    store = FileMemoryStore(root)
    identity = MemoryIdentity("user-a", "project-a")
    _run(store.apply(
        identity,
        _candidate("tooling.package_manager", "pnpm", source_hash="source-1"),
        session_id="s",
    ))
    project_dir = next((root / "users").glob("*/projects/*"))
    snapshot_path = project_dir / "memory.yaml"
    payload = yaml.safe_load(snapshot_path.read_text(encoding="utf-8"))
    payload["memories"]["tooling.package_manager"]["status"] = "unknown"
    snapshot_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    rebuilt = _run(FileMemoryStore(root).load(identity, "project"))

    assert rebuilt.memories["tooling.package_manager"].status == "active"
    assert list(project_dir.glob("memory.corrupt-*.yaml"))


def test_self_consistent_snapshot_tampering_is_overruled_by_fact_log(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    store = FileMemoryStore(root)
    identity = MemoryIdentity("user-a", "project-a")
    _run(store.apply(
        identity,
        _candidate("tooling.package_manager", "pnpm", source_hash="source-1"),
        session_id="s",
    ))
    project_dir = next((root / "users").glob("*/projects/*"))
    snapshot_path = project_dir / "memory.yaml"
    payload = yaml.safe_load(snapshot_path.read_text(encoding="utf-8"))
    payload["memories"]["tooling.package_manager"]["value"] = "npm"
    tampered = MemorySnapshot.from_dict(payload)
    tampered.content_hash = _canonical_hash(tampered)
    snapshot_path.write_text(yaml.safe_dump(tampered.to_dict()), encoding="utf-8")

    rebuilt = _run(FileMemoryStore(root).load(identity, "project"))

    assert rebuilt.memories["tooling.package_manager"].value == "pnpm"
    assert list(project_dir.glob("memory.corrupt-*.yaml"))


def test_store_rejects_sensitive_candidate_before_writing(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    store = FileMemoryStore(root)
    identity = MemoryIdentity("user-a", "project-a")
    secret = _candidate(
        "credentials.api_key", "sk-abcdefghijklmnop", source_hash="secret-source",
    )
    secret.summary = "provider credential"

    with pytest.raises(ValueError, match="sensitive"):
        _run(store.apply(identity, secret, session_id="s"))

    assert not root.exists()


def test_unknown_fact_event_schema_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    store = FileMemoryStore(root)
    identity = MemoryIdentity("user-a", "project-a")
    _run(store.apply(
        identity,
        _candidate("tooling.package_manager", "pnpm", source_hash="source-1"),
        session_id="s",
    ))
    project_dir = next((root / "users").glob("*/projects/*"))
    events_path = project_dir / "events.jsonl"
    invalid = {
        "schema_version": 99,
        "revision": 2,
        "type": "memory_created",
        "timestamp": "2026-08-24T12:00:00Z",
        "source_hash": "source-2",
        "key": "tooling.runtime",
        "record": {},
    }
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(invalid) + "\n")

    with pytest.raises(MemoryStoreError, match="unsupported memory event schema"):
        _run(FileMemoryStore(root).load(identity, "project"))


def test_two_processes_do_not_overwrite_same_scope(tmp_path: Path) -> None:
    root = str(tmp_path / "memory")
    with ProcessPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            _apply_in_process, root, "tooling.package_manager", "process-source-1",
        )
        second = executor.submit(
            _apply_in_process, root, "architecture.memory_storage", "process-source-2",
        )
        assert sorted((first.result(timeout=20), second.result(timeout=20))) == ["created", "created"]

    snapshot = _run(FileMemoryStore(Path(root)).load(
        MemoryIdentity("process-user", "process-project"), "project",
    ))
    assert snapshot.revision == 2
    assert set(snapshot.memories) == {
        "architecture.memory_storage",
        "tooling.package_manager",
    }


def test_same_value_reinforcement_upgrades_authority_and_source(tmp_path: Path) -> None:
    store = FileMemoryStore(tmp_path / "memory")
    identity = MemoryIdentity("user-a", "project-a")
    inferred = _candidate(
        "tooling.package_manager", "pnpm", source_hash="inferred",
        authority="inferred_user",
    )
    explicit = _candidate(
        "tooling.package_manager", "pnpm", source_hash="explicit",
        authority="explicit_user", timestamp="2026-08-24T11:00:00Z",
    )

    _run(store.apply(identity, inferred, session_id="inferred-session"))
    assert _run(store.apply(identity, explicit, session_id="explicit-session")).action == "reinforced"

    record = _run(store.load(identity, "project")).memories["tooling.package_manager"]
    assert record.authority == "explicit_user"
    assert record.source_hash == "explicit"
    assert record.source_session_id == "explicit-session"


def test_retrieval_estimate_respects_multilingual_token_budget(tmp_path: Path) -> None:
    store = FileMemoryStore(tmp_path / "memory")
    identity = MemoryIdentity("user-a", "project-a")
    long_candidate = _candidate(
        "architecture.memory_storage",
        "中文说明" * 80,
        source_hash="long-memory",
    )
    long_candidate.summary = "包含大量中文的长期项目说明"
    _run(store.apply(
        identity,
        long_candidate,
        session_id="s",
    ))

    context = _run(MemoryRetriever(store, token_budget=100).retrieve(identity, "memory"))
    assert context.estimated_tokens <= 100
    assert context.records == []


def test_project_conflict_blocks_global_fallback_for_same_key(tmp_path: Path) -> None:
    store = FileMemoryStore(tmp_path / "memory")
    identity = MemoryIdentity("user-a", "project-a")
    _run(store.apply(
        identity,
        _candidate("response.language", "zh-CN", source_hash="global", scope="global"),
        session_id="s",
    ))
    _run(store.apply(
        identity,
        _candidate("response.language", "en-US", source_hash="project-1"),
        session_id="s",
    ))
    assert _run(store.apply(
        identity,
        _candidate(
            "response.language", "fr-FR", source_hash="project-2",
            timestamp="2026-08-24T11:00:00Z",
        ),
        session_id="s",
    )).action == "conflict"

    context = _run(MemoryRetriever(store).retrieve(identity, "language"))
    assert all(record.key != "response.language" for record in context.records)

    direct_choice = _candidate(
        "response.language", "fr-FR", source_hash="project-choice",
        timestamp="2026-08-24T12:00:00Z",
    )
    assert _run(store.apply(identity, direct_choice, session_id="s")).action == "conflict_resolved"
    resolved = _run(MemoryRetriever(store).retrieve(identity, "language"))
    assert next(record for record in resolved.records if record.key == "response.language").value == "fr-FR"
