from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coding_agent.memory.service import MemoryService
from coding_agent.memory.store import FileMemoryStore
from coding_agent.memory.types import (
    CompletedTask,
    ExtractedMemory,
    MemoryCandidate,
    MemoryEvidence,
    MemoryIdentity,
)


def _run(value):
    return asyncio.run(value)


class _Extractor:
    async def extract(self, task: CompletedTask) -> list[MemoryCandidate]:
        return [MemoryCandidate(
            operation="upsert",
            kind="preference",
            scope="global",
            key="response.language",
            value="zh-CN",
            summary="回答使用中文",
            confidence=1.0,
            source_kind="explicit_user",
            evidence_entry_ids=[task.evidence[0].id],
            source_timestamp=task.evidence[0].timestamp,
        )]


class _BrokenExtractor:
    async def extract(self, _task: CompletedTask):
        raise RuntimeError("extract failed")


class _AtomicExtractor:
    async def extract(self, task: CompletedTask) -> list[ExtractedMemory]:
        return [ExtractedMemory(
            kind="preference",
            scope="global",
            content="回答使用中文",
            value="zh-CN",
            confidence=1.0,
            source_kind="explicit_user",
            evidence_entry_ids=[task.evidence[0].id],
            source_timestamp=task.evidence[0].timestamp,
            relation_key="response.language",
        )]


class _SlowExtractor:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def extract(self, _task: CompletedTask) -> list[ExtractedMemory]:
        self.started.set()
        await asyncio.Event().wait()
        return []


class _SequenceExtractor:
    def __init__(self, values: list[tuple[str, str]]) -> None:
        self.values = values

    async def extract(self, task: CompletedTask) -> list[ExtractedMemory]:
        value, source_kind = self.values.pop(0)
        return [ExtractedMemory(
            kind="fact",
            scope="global",
            content=f"用户显示名是 {value}",
            value=value,
            confidence=1.0,
            source_kind=source_kind,  # type: ignore[arg-type]
            evidence_entry_ids=[task.evidence[0].id],
            source_timestamp=task.evidence[0].timestamp,
            relation_key="identity.display_name",
        )]


def _task(identity: MemoryIdentity) -> CompletedTask:
    return CompletedTask(
        identity=identity,
        session_id="session-1",
        evidence=[MemoryEvidence(
            id="entry-1",
            text="以后回答使用中文",
            source_kind="user",
            timestamp="2026-08-24T10:00:00Z",
        )],
        final_response="好的",
    )


def test_task_end_to_next_task_retrieval(tmp_path: Path) -> None:
    events: list[dict] = []
    identity = MemoryIdentity("local-user", "project-a")
    service = MemoryService(
        store=FileMemoryStore(tmp_path / "memory"),
        extractor=_Extractor(),
        event_sink=events.append,
    )
    extraction = _run(service.after_task(_task(identity)))
    # Simulate a new session/process service loading the persisted files.
    next_session_service = MemoryService(
        store=FileMemoryStore(tmp_path / "memory"),
        extractor=_Extractor(),
        event_sink=events.append,
    )
    context = _run(next_session_service.before_task(identity, "继续处理这个项目"))

    assert extraction.queued == 1
    assert extraction.job_id is not None
    assert "response.language" in context.prompt_block
    assert "zh-CN" in context.prompt_block
    assert any(event["type"] == "memory_extraction_completed" for event in events)
    assert any(event["type"] == "memory_retrieval_completed" for event in events)


def test_extraction_failure_is_non_fatal(tmp_path: Path) -> None:
    service = MemoryService(
        store=FileMemoryStore(tmp_path / "memory"),
        extractor=_BrokenExtractor(),
    )
    identity = MemoryIdentity("u", "p")
    async def scenario():
        queued = await service.after_task(_task(identity))
        result = await service.flush_pending(identity)
        return queued, result

    queued, result = _run(scenario())
    assert queued.queued == 1
    assert result.accepted == 0
    assert result.errors == ["extract failed"]


def test_management_forget_and_clear_leave_tombstones(tmp_path: Path) -> None:
    identity = MemoryIdentity("local-user", "project-a")
    store = FileMemoryStore(tmp_path / "memory")
    service = MemoryService(store=store, extractor=_Extractor())
    _run(service.after_task(_task(identity)))
    _run(service.flush_pending(identity))

    record_id = _run(service.list_records(identity, "global"))[0][1].id
    assert _run(service.forget(identity, record_id)) is True
    snapshot = _run(store.load(identity, "global"))
    assert "response.language" in snapshot.tombstones
    assert _run(service.clear(identity, all_scopes=True)) == 0


def test_v2_atomic_extraction_is_consolidated_and_checkpointed(tmp_path: Path) -> None:
    identity = MemoryIdentity("local-user", "project-a")
    store = FileMemoryStore(tmp_path / "memory")
    service = MemoryService(store=store, extractor=_AtomicExtractor())

    async def scenario():
        queued = await service.after_task(_task(identity))
        completed = await service.flush_pending(identity)
        return queued, completed

    queued, completed = _run(scenario())
    record = _run(store.load(identity, "global")).memories["response.language"]

    assert queued.queued == 1
    assert completed.accepted == 1
    assert record.summary == "回答使用中文"
    assert record.relation_key == "response.language"
    assert record.fingerprint.startswith("sha256:")


def test_explicit_remember_bypasses_extractor_and_rejects_secrets(tmp_path: Path) -> None:
    identity = MemoryIdentity("local-user", "project-a")
    extractor = _BrokenExtractor()
    service = MemoryService(
        store=FileMemoryStore(tmp_path / "memory"), extractor=extractor,
    )

    record = _run(service.remember(identity, "用户喜欢简洁回答", scope="global"))

    assert record is not None
    assert record.kind == "fact"
    assert record.key.startswith("fact.atomic.m")
    with pytest.raises(ValueError, match="unsafe"):
        _run(service.remember(
            identity, "api_key=sk-abcdefghijklmnop", scope="global",
        ))


def test_ambiguous_identity_change_conflicts_until_explicit_correction(tmp_path: Path) -> None:
    identity = MemoryIdentity("local-user", "project-a")
    store = FileMemoryStore(tmp_path / "memory")
    service = MemoryService(
        store=store,
        extractor=_SequenceExtractor([
            ("ssk", "explicit_user"),
            ("sssk", "explicit_user"),
            ("sssk", "explicit_correction"),
        ]),
    )

    async def run_task(entry_id: str) -> None:
        task = _task(identity)
        task.evidence[0].id = entry_id
        task.ended_at = f"2026-08-24T10:0{entry_id[-1]}:00Z"
        await service.after_task(task)
        await service.flush_pending(identity)

    async def scenario() -> None:
        await run_task("entry-1")
        await run_task("entry-2")
        conflicted = await store.load(identity, "global")
        assert "identity.display_name" not in conflicted.memories
        assert "identity.display_name" in conflicted.conflicts

        await run_task("entry-3")

    _run(scenario())
    resolved = _run(store.load(identity, "global"))
    assert resolved.conflicts == {}
    assert resolved.memories["identity.display_name"].value == "sssk"
    assert resolved.memories["identity.display_name"].authority == "explicit_correction"


def test_disabling_auto_extract_releases_running_job(tmp_path: Path) -> None:
    identity = MemoryIdentity("local-user", "project-a")
    extractor = _SlowExtractor()
    service = MemoryService(
        store=FileMemoryStore(tmp_path / "memory"), extractor=extractor,
    )

    async def scenario() -> None:
        await service.after_task(_task(identity))
        await extractor.started.wait()
        service.set_auto_extract(False)
        await asyncio.sleep(0)
        await service.close(grace_seconds=0)
        counts = await service.queue.stats(identity)
        assert counts.pending == 1
        assert counts.processing == 0

    _run(scenario())


def test_queue_events_include_completed_receipt_count(tmp_path: Path) -> None:
    identity = MemoryIdentity("local-user", "project-a")
    events: list[dict] = []
    service = MemoryService(
        store=FileMemoryStore(tmp_path / "memory"),
        extractor=_AtomicExtractor(),
        event_sink=events.append,
    )

    async def scenario() -> None:
        await service.after_task(_task(identity))
        await service.flush_pending(identity)

    _run(scenario())
    queue_events = [item for item in events if item["type"] == "memory_queue_changed"]
    assert queue_events[-1]["ready_count"] == 1


def test_completed_task_is_not_sent_to_models_twice(tmp_path: Path) -> None:
    identity = MemoryIdentity("local-user", "project-a")
    service = MemoryService(
        store=FileMemoryStore(tmp_path / "memory"), extractor=_AtomicExtractor(),
    )
    completed_task = _task(identity)

    async def scenario() -> tuple[int, int]:
        first = await service.after_task(completed_task)
        await service.flush_pending(identity)
        duplicate = await service.after_task(completed_task)
        return first.queued, duplicate.queued

    assert _run(scenario()) == (1, 0)
