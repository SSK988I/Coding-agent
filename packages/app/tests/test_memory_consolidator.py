from __future__ import annotations

import asyncio

import pytest

from coding_agent.memory.consolidator import (
    DeterministicMemoryConsolidator,
    MemoryConsolidationError,
    memory_fingerprint,
    validate_consolidation_payload,
)
from coding_agent.memory.types import (
    CompletedTask,
    ExtractedMemory,
    MemoryIdentity,
    MemoryRecord,
)


def _run(value):
    return asyncio.run(value)


def _task() -> CompletedTask:
    return CompletedTask(
        identity=MemoryIdentity("local-user", "project-a"),
        session_id="session-1",
        evidence=[],
        ended_at="2026-08-24T10:01:00Z",
    )


def _memory(
    content: str = "回答使用中文",
    *,
    value: object = "zh-CN",
    relation_key: str | None = "response.language",
    source_kind: str = "explicit_user",
    scope: str = "global",
    kind: str = "preference",
) -> ExtractedMemory:
    return ExtractedMemory(
        kind=kind,  # type: ignore[arg-type]
        scope=scope,  # type: ignore[arg-type]
        content=content,
        value=value,
        confidence=1.0,
        source_kind=source_kind,  # type: ignore[arg-type]
        evidence_entry_ids=["entry-1"],
        source_timestamp="2026-08-24T10:00:00Z",
        relation_key=relation_key,
    )


def _record(
    memory: ExtractedMemory,
    *,
    record_id: str = "mem_existing",
    key: str | None = None,
) -> MemoryRecord:
    storage_key = key or memory.relation_key or "fact.project.mexisting"
    return MemoryRecord(
        id=record_id,
        kind=memory.kind,
        key=storage_key,
        value=memory.value,
        summary=memory.content,
        status="active",
        authority=memory.source_kind,
        confidence=memory.confidence,
        source_session_id="previous-session",
        source_entry_ids=["previous-entry"],
        source_hash="sha256:previous",
        source_timestamp="2026-08-23T10:00:00Z",
        created_at="2026-08-23T10:00:00Z",
        updated_at="2026-08-23T10:00:00Z",
        relation_key=memory.relation_key,
        fingerprint=memory_fingerprint(memory),
    )


def _decision(
    action: str,
    *,
    candidate_index: int = 0,
    target: str | None = None,
) -> dict[str, object]:
    return {
        "candidateIndex": candidate_index,
        "action": action,
        "targetRecordId": target,
        "reason": "test",
    }


def test_payload_is_strict_and_covers_every_candidate_once() -> None:
    memories = [_memory(), _memory(
        "项目使用 pnpm", value="pnpm", relation_key="tooling.package_manager",
        scope="project", kind="decision",
    )]
    payload = {"decisions": [
        _decision("add", candidate_index=1),
        _decision("ignore", candidate_index=0),
    ]}

    decisions = validate_consolidation_payload(payload, memories, [])

    assert [item.candidate_index for item in decisions] == [0, 1]
    with pytest.raises(MemoryConsolidationError, match="only decisions"):
        validate_consolidation_payload({**payload, "extra": True}, memories, [])
    with pytest.raises(MemoryConsolidationError, match="invalid fields"):
        validate_consolidation_payload(
            {"decisions": [{**_decision("add"), "extra": True}, _decision("add", candidate_index=1)]},
            memories,
            [],
        )
    with pytest.raises(MemoryConsolidationError, match="candidate index"):
        validate_consolidation_payload(
            {"decisions": [_decision("add"), _decision("ignore")]}, memories, [],
        )


def test_payload_rejects_invalid_or_unrelated_targets() -> None:
    existing_memory = _memory()
    existing = [("global", _record(existing_memory))]

    with pytest.raises(MemoryConsolidationError, match="invalid target"):
        validate_consolidation_payload(
            {"decisions": [_decision("reinforce", target="mem_missing")]},
            [existing_memory],
            existing,
        )
    with pytest.raises(MemoryConsolidationError, match="must not include a target"):
        validate_consolidation_payload(
            {"decisions": [_decision("add", target="mem_existing")]},
            [existing_memory],
            existing,
        )
    unrelated = _memory(
        "称呼用户为 ssk", value="ssk", relation_key="identity.display_name",
        kind="fact",
    )
    with pytest.raises(MemoryConsolidationError, match="unrelated record"):
        validate_consolidation_payload(
            {"decisions": [_decision("supersede", target="mem_existing")]},
            [unrelated],
            existing,
        )
    with pytest.raises(MemoryConsolidationError, match="unrelated record"):
        validate_consolidation_payload(
            {"decisions": [_decision("reinforce", target="mem_existing")]},
            [unrelated],
            existing,
        )


def test_payload_rejects_model_driven_retraction() -> None:
    memory = _memory()
    existing = [("global", _record(memory))]

    with pytest.raises(MemoryConsolidationError, match="invalid action"):
        validate_consolidation_payload(
            {"decisions": [_decision("retract", target="mem_existing")]},
            [memory],
            existing,
        )


def test_deterministic_consolidator_adds_new_memory() -> None:
    memory = _memory()

    decision = _run(DeterministicMemoryConsolidator().consolidate(
        _task(), [memory], [],
    ))[0]

    assert decision.action == "add"
    assert decision.target_record_id is None


def test_deterministic_consolidator_reinforces_exact_memory() -> None:
    memory = _memory()
    existing = _record(memory)

    decision = _run(DeterministicMemoryConsolidator().consolidate(
        _task(), [memory], [("global", existing)],
    ))[0]

    assert decision.action == "reinforce"
    assert decision.target_record_id == existing.id


def test_deterministic_consolidator_supersedes_explicit_correction() -> None:
    previous = _record(_memory())
    correction = _memory(
        "回答改用英文", value="en-US", source_kind="explicit_correction",
    )

    decision = _run(DeterministicMemoryConsolidator().consolidate(
        _task(), [correction], [("global", previous)],
    ))[0]

    assert decision.action == "supersede"
    assert decision.target_record_id == previous.id


def test_deterministic_consolidator_preserves_ambiguous_conflict() -> None:
    previous = _record(_memory())
    contradictory = _memory("回答使用英文", value="en-US")

    decision = _run(DeterministicMemoryConsolidator().consolidate(
        _task(), [contradictory], [("global", previous)],
    ))[0]

    assert decision.action == "conflict"
    assert decision.target_record_id == previous.id


def test_unrelated_facts_without_relation_keys_are_additive() -> None:
    first = _memory(
        "用户使用 Windows", value="Windows", relation_key=None, kind="fact",
    )
    second = _memory(
        "用户使用 PowerShell", value="PowerShell", relation_key=None, kind="fact",
    )
    existing = _record(first, key="fact.environment.mwindows")

    decisions = _run(DeterministicMemoryConsolidator().consolidate(
        _task(), [first, second], [("global", existing)],
    ))

    assert decisions[0].action == "reinforce"
    assert decisions[0].target_record_id == existing.id
    assert decisions[1].action == "add"
    assert decisions[1].target_record_id is None


def test_fingerprint_normalizes_case_and_whitespace() -> None:
    first = _memory("Use   PNPM", value="pnpm", relation_key=None)
    second = _memory(" use pnpm ", value="pnpm", relation_key=None)

    assert memory_fingerprint(first) == memory_fingerprint(second)
