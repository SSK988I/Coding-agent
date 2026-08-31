"""Deterministic conflict resolution for structured memories."""
from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from coding_agent.memory.types import (
    ApplyResult,
    MemoryCandidate,
    MemoryConflict,
    MemoryRecord,
    MemorySnapshot,
    MemorySourceKind,
    MemoryTombstone,
    utc_now,
)

AUTHORITY: dict[MemorySourceKind, int] = {
    "explicit_correction": 400,
    "explicit_user": 300,
    "accepted_plan": 200,
    "inferred_user": 100,
}


@dataclass
class Resolution:
    snapshot: MemorySnapshot
    event_type: str | None
    payload: dict[str, Any]
    result: ApplyResult


def _normalized_value(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


def _after(left: str, right: str) -> bool:
    try:
        return datetime.fromisoformat(left.replace("Z", "+00:00")) > datetime.fromisoformat(
            right.replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return left > right


def _new_record(candidate: MemoryCandidate, session_id: str, now: str) -> MemoryRecord:
    assert candidate.kind is not None
    return MemoryRecord(
        id=f"mem_{uuid.uuid4().hex}",
        kind=candidate.kind,
        key=candidate.key,
        value=copy.deepcopy(candidate.value),
        summary=candidate.summary,
        status="active",
        authority=candidate.source_kind,
        confidence=candidate.confidence,
        source_session_id=session_id,
        source_entry_ids=list(dict.fromkeys(candidate.evidence_entry_ids)),
        source_hash=candidate.source_hash,
        source_timestamp=candidate.source_timestamp,
        relation_key=candidate.relation_key,
        fingerprint=candidate.fingerprint,
        created_at=now,
        updated_at=now,
    )


def _mark_applied(snapshot: MemorySnapshot, source_hash: str) -> None:
    if source_hash and source_hash not in snapshot.applied_source_hashes:
        snapshot.applied_source_hashes.append(source_hash)


def _newest(records: list[MemoryRecord]) -> MemoryRecord | None:
    newest: MemoryRecord | None = None
    for record in records:
        if newest is None or _after(record.source_timestamp, newest.source_timestamp):
            newest = record
    return newest


def _reject_if_stale(
    snapshot: MemorySnapshot,
    candidate: MemoryCandidate,
    records: list[MemoryRecord],
) -> Resolution | None:
    newest = _newest(records)
    if newest is None or not _after(newest.source_timestamp, candidate.source_timestamp):
        return None
    _mark_applied(snapshot, candidate.source_hash)
    return Resolution(
        snapshot,
        "candidate_rejected",
        {
            "key": candidate.key,
            "reason": "stale_source_timestamp",
            "active_record_id": newest.id,
        },
        ApplyResult(
            action="rejected",
            record=newest,
            reason="stale_source_timestamp",
        ),
    )


def resolve_candidate(
    source: MemorySnapshot,
    candidate: MemoryCandidate,
    *,
    session_id: str,
    now: str | None = None,
) -> Resolution:
    """Resolve one validated candidate against a snapshot without I/O."""
    snapshot = copy.deepcopy(source)
    timestamp = now or utc_now()
    key = candidate.key

    if candidate.source_hash and candidate.source_hash in snapshot.applied_source_hashes:
        return Resolution(snapshot, None, {}, ApplyResult(action="duplicate", reason="source_hash"))

    if candidate.operation == "retract":
        kind = candidate.kind
        existing = snapshot.memories.get(key)
        conflict = snapshot.conflicts.get(key)
        if kind is None:
            kind = existing.kind if existing else (conflict.kind if conflict else "preference")
        tombstone = MemoryTombstone(
            key=key,
            kind=kind,
            deleted_at=timestamp,
            source_session_id=session_id,
            source_hash=candidate.source_hash,
        )
        snapshot.memories.pop(key, None)
        snapshot.conflicts.pop(key, None)
        snapshot.tombstones[key] = tombstone
        _mark_applied(snapshot, candidate.source_hash)
        return Resolution(
            snapshot,
            "memory_tombstoned",
            {"key": key, "tombstone": tombstone.to_dict()},
            ApplyResult(action="tombstoned"),
        )

    if candidate.operation != "upsert" or candidate.kind is None:
        return Resolution(snapshot, None, {}, ApplyResult(action="noop"))

    tombstone = snapshot.tombstones.get(key)
    if tombstone is not None:
        can_recreate = (
            candidate.source_kind in {"explicit_correction", "explicit_user"}
            and _after(candidate.source_timestamp, tombstone.deleted_at)
        )
        if not can_recreate:
            _mark_applied(snapshot, candidate.source_hash)
            return Resolution(
                snapshot,
                "candidate_rejected",
                {"key": key, "reason": "tombstoned"},
                ApplyResult(action="rejected", reason="tombstoned"),
            )
        snapshot.tombstones.pop(key, None)

    incoming = _new_record(candidate, session_id, timestamp)
    open_conflict = snapshot.conflicts.get(key)
    if open_conflict is not None:
        stale = _reject_if_stale(snapshot, candidate, list(open_conflict.candidates))
        if stale is not None:
            return stale
        highest = max((AUTHORITY[item.authority] for item in open_conflict.candidates), default=0)
        matching = next(
            (
                item for item in open_conflict.candidates
                if _normalized_value(item.value) == _normalized_value(incoming.value)
            ),
            None,
        )
        direct_choice = matching is not None and candidate.source_kind in {
            "explicit_correction", "explicit_user",
        }
        if direct_choice or AUTHORITY[candidate.source_kind] > highest:
            matching_entry_ids = (
                list(dict.fromkeys(matching.source_entry_ids + candidate.evidence_entry_ids))
                if matching is not None and matching.source_session_id == session_id
                else list(dict.fromkeys(candidate.evidence_entry_ids))
            )
            chosen = incoming if matching is None else replace(
                matching,
                status="active",
                authority=candidate.source_kind,
                confidence=max(matching.confidence, candidate.confidence),
                summary=candidate.summary or matching.summary,
                source_session_id=session_id,
                source_entry_ids=matching_entry_ids,
                source_hash=candidate.source_hash,
                source_timestamp=candidate.source_timestamp,
                relation_key=candidate.relation_key or matching.relation_key,
                fingerprint=candidate.fingerprint or matching.fingerprint,
                updated_at=timestamp,
            )
            snapshot.conflicts.pop(key, None)
            snapshot.memories[key] = chosen
            _mark_applied(snapshot, candidate.source_hash)
            return Resolution(
                snapshot,
                "memory_conflict_resolved",
                {"key": key, "record": chosen.to_dict()},
                ApplyResult(action="conflict_resolved", record=chosen),
            )
        if matching is None:
            incoming.status = "conflicted"
            open_conflict.candidates.append(incoming)
        open_conflict.detected_at = timestamp
        _mark_applied(snapshot, candidate.source_hash)
        return Resolution(
            snapshot,
            "memory_conflict_detected",
            {"key": key, "conflict": open_conflict.to_dict()},
            ApplyResult(action="conflict", conflict=open_conflict),
        )

    existing = snapshot.memories.get(key)
    if existing is None:
        snapshot.memories[key] = incoming
        _mark_applied(snapshot, candidate.source_hash)
        return Resolution(
            snapshot,
            "memory_created",
            {"key": key, "record": incoming.to_dict()},
            ApplyResult(action="created", record=incoming),
        )

    stale = _reject_if_stale(snapshot, candidate, [existing])
    if stale is not None:
        return stale

    if existing.kind != incoming.kind:
        incoming.status = "conflicted"
        current = replace(existing, status="conflicted")
        conflict = MemoryConflict(
            key=key,
            kind=existing.kind,
            candidates=[current, incoming],
            detected_at=timestamp,
        )
        snapshot.memories.pop(key, None)
        snapshot.conflicts[key] = conflict
        _mark_applied(snapshot, candidate.source_hash)
        return Resolution(
            snapshot,
            "memory_conflict_detected",
            {"key": key, "conflict": conflict.to_dict()},
            ApplyResult(action="conflict", conflict=conflict, reason="kind_mismatch"),
        )

    if _normalized_value(existing.value) == _normalized_value(incoming.value):
        incoming_is_stronger = AUTHORITY[incoming.authority] >= AUTHORITY[existing.authority]
        same_source_session = existing.source_session_id == incoming.source_session_id
        if incoming_is_stronger:
            source_entry_ids = (
                list(dict.fromkeys(existing.source_entry_ids + incoming.source_entry_ids))
                if same_source_session else list(incoming.source_entry_ids)
            )
        else:
            source_entry_ids = (
                list(dict.fromkeys(existing.source_entry_ids + incoming.source_entry_ids))
                if same_source_session else list(existing.source_entry_ids)
            )
        reinforced = replace(
            existing,
            summary=incoming.summary or existing.summary,
            authority=incoming.authority if incoming_is_stronger else existing.authority,
            confidence=max(existing.confidence, incoming.confidence),
            source_session_id=incoming.source_session_id if incoming_is_stronger else existing.source_session_id,
            source_entry_ids=source_entry_ids,
            source_hash=incoming.source_hash if incoming_is_stronger else existing.source_hash,
            source_timestamp=(
                incoming.source_timestamp if incoming_is_stronger else existing.source_timestamp
            ),
            relation_key=incoming.relation_key or existing.relation_key,
            fingerprint=incoming.fingerprint or existing.fingerprint,
            updated_at=timestamp,
        )
        snapshot.memories[key] = reinforced
        _mark_applied(snapshot, candidate.source_hash)
        return Resolution(
            snapshot,
            "memory_reinforced",
            {"key": key, "record": reinforced.to_dict()},
            ApplyResult(action="reinforced", record=reinforced),
        )

    old_authority = AUTHORITY[existing.authority]
    new_authority = AUTHORITY[incoming.authority]
    if new_authority > old_authority or incoming.authority == "explicit_correction":
        incoming.supersedes = existing.id
        snapshot.memories[key] = incoming
        _mark_applied(snapshot, candidate.source_hash)
        return Resolution(
            snapshot,
            "memory_superseded",
            {"key": key, "previous_id": existing.id, "record": incoming.to_dict()},
            ApplyResult(action="superseded", record=incoming),
        )

    if new_authority < old_authority:
        _mark_applied(snapshot, candidate.source_hash)
        return Resolution(
            snapshot,
            "candidate_rejected",
            {"key": key, "reason": "lower_authority", "active_record_id": existing.id},
            ApplyResult(action="rejected", record=existing, reason="lower_authority"),
        )

    current = replace(existing, status="conflicted")
    incoming.status = "conflicted"
    conflict = MemoryConflict(
        key=key,
        kind=existing.kind,
        candidates=[current, incoming],
        detected_at=timestamp,
    )
    snapshot.memories.pop(key, None)
    snapshot.conflicts[key] = conflict
    _mark_applied(snapshot, candidate.source_hash)
    return Resolution(
        snapshot,
        "memory_conflict_detected",
        {"key": key, "conflict": conflict.to_dict()},
        ApplyResult(action="conflict", conflict=conflict),
    )


__all__ = ["AUTHORITY", "Resolution", "resolve_candidate"]
