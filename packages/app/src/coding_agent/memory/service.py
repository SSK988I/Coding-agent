"""Application lifecycle service for retrieval, extraction and management."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Callable

from coding_agent.memory.interfaces import MemoryExtractor, MemoryStore
from coding_agent.memory.retriever import MemoryRetriever
from coding_agent.memory.types import (
    CompletedTask,
    ExtractionResult,
    MemoryCandidate,
    MemoryConflict,
    MemoryContext,
    MemoryIdentity,
    MemoryOverview,
    MemoryRecord,
    MemoryScope,
)

EventSink = Callable[[dict[str, Any]], Any]


def _operation_hash(identity: MemoryIdentity, candidate: MemoryCandidate) -> str:
    payload = {
        "user_id": identity.user_id,
        "project_id": identity.project_id if candidate.scope == "project" else None,
        "scope": candidate.scope,
        "operation": candidate.operation,
        "kind": candidate.kind,
        "key": candidate.key,
        "value": candidate.value,
        "source_kind": candidate.source_kind,
        "evidence": sorted(candidate.evidence_entry_ids),
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class MemoryService:
    def __init__(
        self,
        *,
        store: MemoryStore,
        extractor: MemoryExtractor,
        enabled: bool = True,
        max_records: int = 8,
        token_budget: int = 800,
        event_sink: EventSink | None = None,
    ) -> None:
        self.store = store
        self.extractor = extractor
        self.enabled = enabled
        self.retriever = MemoryRetriever(
            store, max_records=max_records, token_budget=token_budget,
        )
        self._event_sink = event_sink
        self._lifecycle_lock = asyncio.Lock()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    async def before_task(self, identity: MemoryIdentity, user_message: str) -> MemoryContext:
        if not self.enabled:
            return MemoryContext(records=[], prompt_block="", estimated_tokens=0)
        # Wait for the previous task's extraction transaction before reading.
        async with self._lifecycle_lock:
            self._emit("memory_retrieval_started", user_id=identity.user_id)
            try:
                context = await self.retriever.retrieve(identity, user_message)
            except Exception as exc:
                self._emit("memory_retrieval_failed", error_code=type(exc).__name__)
                return MemoryContext(records=[], prompt_block="", estimated_tokens=0)
        self._emit(
            "memory_retrieval_completed",
            count=len(context.records),
            estimated_tokens=context.estimated_tokens,
            record_ids=[record.id for record in context.records],
        )
        return context

    async def after_task(self, task: CompletedTask) -> ExtractionResult:
        result = ExtractionResult()
        if not self.enabled or not task.success or not task.evidence:
            return result
        async with self._lifecycle_lock:
            self._emit("memory_extraction_started", evidence_count=len(task.evidence))
            try:
                candidates = await self.extractor.extract(task)
            except Exception as exc:
                result.errors.append(str(exc))
                self._emit("memory_extraction_failed", error_code=type(exc).__name__)
                return result
            result.candidates = len(candidates)
            for candidate in candidates:
                candidate.source_hash = _operation_hash(task.identity, candidate)
                try:
                    applied = await self.store.apply(
                        task.identity, candidate, session_id=task.session_id,
                    )
                except Exception as exc:
                    result.rejected += 1
                    result.errors.append(str(exc))
                    self._emit("memory_extraction_failed", error_code=type(exc).__name__)
                    continue
                if applied.action == "duplicate":
                    result.duplicated += 1
                elif applied.action == "conflict":
                    result.conflicts += 1
                    self._emit(
                        "memory_conflict_detected",
                        key=candidate.key,
                        record_ids=[item.id for item in (applied.conflict.candidates if applied.conflict else [])],
                    )
                elif applied.action == "rejected":
                    result.rejected += 1
                elif applied.action not in {"noop"}:
                    result.accepted += 1
                    self._emit(
                        "memory_record_changed",
                        action=applied.action,
                        key=candidate.key,
                        record_id=applied.record.id if applied.record else None,
                    )
            self._emit(
                "memory_extraction_completed",
                candidates=result.candidates,
                accepted=result.accepted,
                rejected=result.rejected,
                duplicated=result.duplicated,
                conflicts=result.conflicts,
            )
        return result

    async def overview(self, identity: MemoryIdentity) -> MemoryOverview:
        async with self._lifecycle_lock:
            global_snapshot = await self.store.load(identity, "global")
            project_snapshot = (
                await self.store.load(identity, "project") if identity.project_id else None
            )
        return MemoryOverview(
            enabled=self.enabled,
            user_id=identity.user_id,
            project_id=identity.project_id,
            global_count=len(global_snapshot.memories),
            project_count=len(project_snapshot.memories) if project_snapshot else 0,
            conflict_count=len(global_snapshot.conflicts) + (
                len(project_snapshot.conflicts) if project_snapshot else 0
            ),
            root=str(Path(self.store.root)),
        )

    async def list_records(
        self, identity: MemoryIdentity, scope: MemoryScope | None = None,
    ) -> list[tuple[MemoryScope, MemoryRecord]]:
        scopes: list[MemoryScope] = [scope] if scope else ["global", "project"]
        result: list[tuple[MemoryScope, MemoryRecord]] = []
        async with self._lifecycle_lock:
            for current in scopes:
                if current == "project" and not identity.project_id:
                    continue
                snapshot = await self.store.load(identity, current)
                result.extend((current, record) for record in snapshot.memories.values())
        result.sort(key=lambda item: (item[0], item[1].key))
        return result

    async def list_conflicts(
        self, identity: MemoryIdentity,
    ) -> list[tuple[MemoryScope, MemoryConflict]]:
        result: list[tuple[MemoryScope, MemoryConflict]] = []
        async with self._lifecycle_lock:
            for scope in ("global", "project"):
                if scope == "project" and not identity.project_id:
                    continue
                snapshot = await self.store.load(identity, scope)
                result.extend((scope, item) for item in snapshot.conflicts.values())
        return result

    async def forget(
        self,
        identity: MemoryIdentity,
        key: str,
        *,
        scope: MemoryScope | None = None,
        session_id: str = "memory-command",
    ) -> bool:
        async with self._lifecycle_lock:
            scopes: list[MemoryScope] = [scope] if scope else ["project", "global"]
            for current in scopes:
                if current == "project" and not identity.project_id:
                    continue
                snapshot = await self.store.load(identity, current)
                target_key = key
                record = snapshot.memories.get(target_key)
                conflict = snapshot.conflicts.get(target_key)
                if record is None and conflict is None:
                    for record_key, candidate in snapshot.memories.items():
                        if candidate.id == key:
                            target_key, record = record_key, candidate
                            break
                    if record is None:
                        for conflict_key, candidate in snapshot.conflicts.items():
                            if any(item.id == key for item in candidate.candidates):
                                target_key, conflict = conflict_key, candidate
                                break
                if record is None and conflict is None:
                    continue
                if record is not None:
                    kind = record.kind
                else:
                    assert conflict is not None
                    kind = conflict.kind
                command_id = uuid.uuid4().hex
                await self.store.forget(
                    identity,
                    scope=current,
                    key=target_key,
                    kind=kind,
                    session_id=session_id,
                    source_hash=f"command:{command_id}",
                )
                self._emit("memory_record_changed", action="tombstoned", key=target_key)
                return True
        return False

    async def clear(
        self,
        identity: MemoryIdentity,
        *,
        all_scopes: bool,
        session_id: str = "memory-command",
    ) -> int:
        scopes: list[MemoryScope] = ["global", "project"] if all_scopes else ["project"]
        if not identity.project_id:
            scopes = ["global"] if all_scopes else []
        async with self._lifecycle_lock:
            count = await self.store.clear(identity, scopes=scopes, session_id=session_id)
        if count:
            self._emit("memory_record_changed", action="cleared", count=count)
        return count

    def _emit(self, event_type: str, **payload: Any) -> None:
        if self._event_sink is None:
            return
        try:
            result = self._event_sink({"type": event_type, **payload})
            if hasattr(result, "__await__"):
                asyncio.create_task(result)
        except Exception:
            pass


__all__ = ["MemoryService"]
