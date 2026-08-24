from __future__ import annotations

import asyncio
from pathlib import Path

from coding_agent.memory.service import MemoryService
from coding_agent.memory.store import FileMemoryStore
from coding_agent.memory.types import (
    CompletedTask,
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

    assert extraction.accepted == 1
    assert "response.language" in context.prompt_block
    assert "zh-CN" in context.prompt_block
    assert any(event["type"] == "memory_extraction_completed" for event in events)
    assert any(event["type"] == "memory_retrieval_completed" for event in events)


def test_extraction_failure_is_non_fatal(tmp_path: Path) -> None:
    service = MemoryService(
        store=FileMemoryStore(tmp_path / "memory"),
        extractor=_BrokenExtractor(),
    )
    result = _run(service.after_task(_task(MemoryIdentity("u", "p"))))
    assert result.accepted == 0
    assert result.errors == ["extract failed"]


def test_management_forget_and_clear_leave_tombstones(tmp_path: Path) -> None:
    identity = MemoryIdentity("local-user", "project-a")
    store = FileMemoryStore(tmp_path / "memory")
    service = MemoryService(store=store, extractor=_Extractor())
    _run(service.after_task(_task(identity)))

    record_id = _run(service.list_records(identity, "global"))[0][1].id
    assert _run(service.forget(identity, record_id)) is True
    snapshot = _run(store.load(identity, "global"))
    assert "response.language" in snapshot.tombstones
    assert _run(service.clear(identity, all_scopes=True)) == 0
