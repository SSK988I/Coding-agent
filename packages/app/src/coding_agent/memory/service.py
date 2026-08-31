"""Long-term memory lifecycle: queue, extract, consolidate, apply and retrieve."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import uuid
from typing import Any, Callable, cast

from coding_agent.memory.consolidator import (
    DeterministicMemoryConsolidator,
    memory_fingerprint,
    validate_consolidation_payload,
)
from coding_agent.memory.interfaces import (
    MemoryConsolidator,
    MemoryExtractor,
    MemoryJobQueue,
    MemoryStore,
)
from coding_agent.memory.jobs import FileMemoryJobQueue
from coding_agent.memory.retriever import MemoryRetriever
from coding_agent.memory.store import StaleMemoryStoreWriteError
from coding_agent.memory.types import (
    CompletedTask,
    ConsolidationDecision,
    ExtractionResult,
    ExtractedMemory,
    MemoryCandidate,
    MemoryConflict,
    MemoryContext,
    MemoryIdentity,
    MemoryOverview,
    MemoryRecord,
    MemoryScope,
    PendingMemoryJob,
    redact_sensitive_text,
    utc_now,
    validate_extracted_memory,
)

EventSink = Callable[[dict[str, Any]], Any]


class StaleMemoryConsolidationError(RuntimeError):
    pass


def _operation_hash(
    identity: MemoryIdentity,
    candidate: MemoryCandidate,
    *,
    job_id: str = "",
    candidate_index: int = -1,
) -> str:
    payload = {
        "job_id": job_id,
        "candidate_index": candidate_index,
        "user_id": identity.user_id,
        "project_id": identity.project_id if candidate.scope == "project" else None,
        "scope": candidate.scope,
        "operation": candidate.operation,
        "kind": candidate.kind,
        "key": candidate.key,
        "value": candidate.value,
        "source_kind": candidate.source_kind,
        "evidence": sorted(candidate.evidence_entry_ids),
        "relation_key": candidate.relation_key,
        "fingerprint": candidate.fingerprint,
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _generated_storage_key(memory: ExtractedMemory) -> str:
    digest = memory_fingerprint(memory).removeprefix("sha256:")[:16]
    return f"{memory.kind}.atomic.m{digest}"


class MemoryService:
    def __init__(
        self,
        *,
        store: MemoryStore,
        extractor: MemoryExtractor,
        consolidator: MemoryConsolidator | None = None,
        queue: MemoryJobQueue | None = None,
        enabled: bool = True,
        auto_extract: bool = True,
        max_records: int = 8,
        token_budget: int = 800,
        event_sink: EventSink | None = None,
    ) -> None:
        self.store = store
        self.extractor = extractor
        self.consolidator = consolidator or DeterministicMemoryConsolidator()
        self.queue = queue or FileMemoryJobQueue(store.root)
        self.enabled = enabled
        self.auto_extract = auto_extract
        self.retriever = MemoryRetriever(
            store, max_records=max_records, token_budget=token_budget,
        )
        self._event_sink = event_sink
        self._management_lock = asyncio.Lock()
        self._worker_task: asyncio.Task[ExtractionResult] | None = None
        self._worker_identity: MemoryIdentity | None = None
        self._worker_id = f"worker_{uuid.uuid4().hex}"
        self._event_tasks: set[asyncio.Task[Any]] = set()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if not enabled:
            self.request_stop()

    def set_auto_extract(self, enabled: bool) -> None:
        self.auto_extract = enabled
        if not enabled:
            self.request_stop()

    async def before_task(self, identity: MemoryIdentity, user_message: str) -> MemoryContext:
        if not self.enabled:
            return MemoryContext(records=[], prompt_block="", estimated_tokens=0)
        # A new process drains persisted work before retrieval so the next task
        # can observe a memory queued by a terminal that has already exited.
        if self.auto_extract:
            try:
                await self.flush_pending(identity, max_jobs=4)
            except Exception as exc:
                self._emit("memory_queue_failed", error_code=type(exc).__name__)
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
        if (
            not self.enabled
            or not self.auto_extract
            or not task.success
            or not task.evidence
        ):
            return result
        job = await self.queue.enqueue(task)
        result.job_id = job.id
        if job.status == "completed":
            self._emit("memory_job_duplicate", job_id=job.id)
            await self._emit_queue_state(task.identity)
            return result
        result.queued = 1
        self._emit(
            "memory_job_queued", job_id=job.id, evidence_count=len(task.evidence),
        )
        await self._emit_queue_state(task.identity)
        self._start_worker(task.identity)
        return result

    async def flush_pending(
        self, identity: MemoryIdentity, *, max_jobs: int | None = None,
    ) -> ExtractionResult:
        task = self._worker_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            if self._worker_identity == identity:
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
        return await self.process_pending(identity, max_jobs=max_jobs)

    async def process_pending(
        self, identity: MemoryIdentity, *, max_jobs: int | None = None,
    ) -> ExtractionResult:
        aggregate = ExtractionResult()
        processed = 0
        while max_jobs is None or processed < max_jobs:
            if not self.enabled or not self.auto_extract:
                break
            job: PendingMemoryJob | None = None
            claim_task = asyncio.create_task(
                self.queue.claim(identity, owner_id=self._worker_id)
            )
            try:
                job = await asyncio.shield(claim_task)
            except asyncio.CancelledError:
                job = await claim_task
                if job is not None:
                    await self._release_claimed_job(job)
                raise
            if job is None:
                break
            processed += 1
            await self._emit_queue_state(identity)
            current = ExtractionResult(job_id=job.id)
            try:
                current = await self._process_job(job)
            except asyncio.CancelledError:
                await self._release_claimed_job(job)
                await self._emit_queue_state(identity)
                raise
            except Exception as exc:
                safe_error = redact_sensitive_text(str(exc))
                current.errors.append(safe_error)
                failed = await self.queue.fail(
                    job,
                    owner_id=self._worker_id,
                    error=safe_error,
                    retryable=True,
                    reset_stage=(
                        "consolidation"
                        if isinstance(exc, StaleMemoryConsolidationError) else None
                    ),
                )
                self._emit(
                    "memory_extraction_failed",
                    job_id=job.id,
                    stage=job.stage,
                    final=failed.status == "failed",
                    error_code=type(exc).__name__,
                )
            self._merge_result(aggregate, current)
            await self._emit_queue_state(identity)
        return aggregate

    async def _process_job(self, job: PendingMemoryJob) -> ExtractionResult:
        result = ExtractionResult(job_id=job.id)
        self._emit(
            "memory_extraction_started",
            job_id=job.id,
            stage=job.stage,
            evidence_count=len(job.task.evidence),
        )

        if job.extracted_payload is None:
            extracted_raw = await self.extractor.extract(job.task)
            # Compatibility for programmatic v1 extractors: their store commands
            # are already consolidated and can be applied idempotently as-is.
            if extracted_raw and all(isinstance(item, MemoryCandidate) for item in extracted_raw):
                legacy = [cast(MemoryCandidate, item) for item in extracted_raw]
                return await self._apply_legacy_job(job, legacy)
            if any(not isinstance(item, ExtractedMemory) for item in extracted_raw):
                raise TypeError("memory extractor returned an unsupported candidate type")
            extracted = [item for item in extracted_raw if isinstance(item, ExtractedMemory)]
            for memory in extracted:
                validate_extracted_memory(memory)
            job = await self.queue.checkpoint(
                job,
                owner_id=self._worker_id,
                stage="consolidation",
                extracted_payload=[item.to_dict() for item in extracted],
            )
        else:
            extracted = [ExtractedMemory.from_dict(item) for item in job.extracted_payload]

        result.candidates = len(extracted)
        existing = await self._records_for_consolidation(job.task.identity)
        if job.consolidation_payload is None:
            if extracted:
                decisions = await self.consolidator.consolidate(
                    job.task, extracted, existing,
                )
            else:
                decisions = []
            decisions = self._validate_decisions(decisions, extracted, existing)
            job = await self.queue.checkpoint(
                job,
                owner_id=self._worker_id,
                stage="apply",
                consolidation_payload=[item.to_dict() for item in decisions],
            )
        else:
            decisions = [
                ConsolidationDecision.from_dict(item) for item in job.consolidation_payload
            ]
            try:
                decisions = self._validate_decisions(decisions, extracted, existing)
            except Exception as exc:
                raise StaleMemoryConsolidationError(
                    "memory consolidation checkpoint became stale"
                ) from exc

        target_by_id = {record.id: record for _, record in existing}
        for decision in decisions:
            memory = extracted[decision.candidate_index]
            target = (
                target_by_id.get(decision.target_record_id)
                if decision.target_record_id is not None else None
            )
            if decision.target_record_id is not None and target is None:
                raise StaleMemoryConsolidationError(
                    "memory consolidation target became stale"
                )
            candidate = self._candidate_from_decision(memory, decision, target)
            if candidate is None:
                continue
            candidate.source_hash = _operation_hash(
                job.task.identity,
                candidate,
                job_id=job.id,
                candidate_index=decision.candidate_index,
            )
            try:
                applied = await self.store.apply(
                    job.task.identity,
                    candidate,
                    session_id=job.task.session_id,
                    expected_record_id=decision.target_record_id,
                )
            except StaleMemoryStoreWriteError as exc:
                raise StaleMemoryConsolidationError(
                    "memory consolidation target changed before apply"
                ) from exc
            self._count_apply_result(result, applied.action)
            if applied.action == "conflict":
                self._emit(
                    "memory_conflict_detected",
                    key=candidate.key,
                    record_ids=[
                        item.id for item in (
                            applied.conflict.candidates if applied.conflict else []
                        )
                    ],
                )
            elif applied.action not in {"noop", "duplicate", "rejected"}:
                self._emit(
                    "memory_record_changed",
                    action=applied.action,
                    key=candidate.key,
                    record_id=applied.record.id if applied.record else None,
                )

        receipt = {
            "candidates": result.candidates,
            "accepted": result.accepted,
            "rejected": result.rejected,
            "duplicated": result.duplicated,
            "conflicts": result.conflicts,
        }
        await self.queue.complete(
            job, owner_id=self._worker_id, result=receipt,
        )
        self._emit("memory_extraction_completed", job_id=job.id, **receipt)
        return result

    async def _apply_legacy_job(
        self, job: PendingMemoryJob, candidates: list[MemoryCandidate],
    ) -> ExtractionResult:
        result = ExtractionResult(job_id=job.id, candidates=len(candidates))
        for index, candidate in enumerate(candidates):
            candidate.source_hash = candidate.source_hash or _operation_hash(
                job.task.identity, candidate, job_id=job.id, candidate_index=index,
            )
            applied = await self.store.apply(
                job.task.identity, candidate, session_id=job.task.session_id,
            )
            self._count_apply_result(result, applied.action)
        receipt = {
            "candidates": result.candidates,
            "accepted": result.accepted,
            "rejected": result.rejected,
            "duplicated": result.duplicated,
            "conflicts": result.conflicts,
        }
        await self.queue.complete(job, owner_id=self._worker_id, result=receipt)
        self._emit("memory_extraction_completed", job_id=job.id, **receipt)
        return result

    async def _records_for_consolidation(
        self, identity: MemoryIdentity,
    ) -> list[tuple[MemoryScope, MemoryRecord]]:
        result: list[tuple[MemoryScope, MemoryRecord]] = []
        for scope in ("global", "project"):
            if scope == "project" and not identity.project_id:
                continue
            snapshot = await self.store.load(identity, scope)
            result.extend((scope, item) for item in snapshot.memories.values())
            for conflict in snapshot.conflicts.values():
                result.extend((scope, item) for item in conflict.candidates)
        return result

    @staticmethod
    def _validate_decisions(
        decisions: list[ConsolidationDecision],
        extracted: list[ExtractedMemory],
        existing: list[tuple[MemoryScope, MemoryRecord]],
    ) -> list[ConsolidationDecision]:
        payload = {
            "decisions": [
                {
                    "candidateIndex": item.candidate_index,
                    "action": item.action,
                    "targetRecordId": item.target_record_id,
                    "reason": item.reason,
                }
                for item in decisions
            ]
        }
        return validate_consolidation_payload(payload, extracted, existing)

    @staticmethod
    def _candidate_from_decision(
        memory: ExtractedMemory,
        decision: ConsolidationDecision,
        target: MemoryRecord | None,
    ) -> MemoryCandidate | None:
        if decision.action == "ignore":
            return None
        if decision.action in {"reinforce", "supersede", "conflict"} and target is None:
            raise ValueError(f"memory {decision.action} requires a target")
        key = (
            target.key if target is not None
            else memory.relation_key or _generated_storage_key(memory)
        )
        value = target.value if decision.action == "reinforce" and target is not None else memory.value
        relation_key = (
            target.relation_key or target.key if target is not None else memory.relation_key
        )
        fingerprint = (
            target.fingerprint
            if decision.action == "reinforce" and target is not None and target.fingerprint
            else memory_fingerprint(memory)
        )
        return MemoryCandidate(
            operation="upsert",
            kind=memory.kind,
            scope=memory.scope,
            key=key,
            value=value,
            summary=(
                target.summary
                if decision.action == "reinforce" and target is not None
                else memory.content
            ),
            confidence=memory.confidence,
            source_kind=memory.source_kind,
            evidence_entry_ids=memory.evidence_entry_ids,
            source_timestamp=memory.source_timestamp,
            relation_key=relation_key,
            fingerprint=fingerprint,
        )

    async def remember(
        self,
        identity: MemoryIdentity,
        content: str,
        *,
        scope: MemoryScope,
        session_id: str = "memory-command",
        relation_key: str | None = None,
        kind: str = "fact",
        value: Any = None,
        correction: bool = False,
    ) -> MemoryRecord | None:
        content = content.strip()
        if not content:
            raise ValueError("memory content cannot be empty")
        memory = ExtractedMemory(
            kind=kind,  # type: ignore[arg-type]
            scope=scope,
            content=content,
            value=content if value is None else value,
            confidence=1.0,
            source_kind="explicit_correction" if correction else "explicit_user",
            evidence_entry_ids=[f"command:{uuid.uuid4().hex}"],
            source_timestamp=utc_now(),
            relation_key=relation_key,
        )
        validate_extracted_memory(memory)
        applied = None
        candidate = None
        for _ in range(2):
            existing = await self._records_for_consolidation(identity)
            decisions = await DeterministicMemoryConsolidator().consolidate(
                CompletedTask(identity, session_id, []), [memory], existing,
            )
            decision = decisions[0]
            target_by_id = {record.id: record for _, record in existing}
            target = (
                target_by_id.get(decision.target_record_id)
                if decision.target_record_id is not None else None
            )
            candidate = self._candidate_from_decision(memory, decision, target)
            if candidate is None:
                return None
            candidate.source_hash = f"command:{uuid.uuid4().hex}"
            try:
                applied = await self.store.apply(
                    identity,
                    candidate,
                    session_id=session_id,
                    expected_record_id=decision.target_record_id,
                )
                break
            except StaleMemoryStoreWriteError:
                continue
        if applied is None or candidate is None:
            raise StaleMemoryConsolidationError("memory changed while remembering")
        self._emit(
            "memory_record_changed", action=applied.action, key=candidate.key,
            record_id=applied.record.id if applied.record else None,
        )
        return applied.record

    async def overview(self, identity: MemoryIdentity) -> MemoryOverview:
        global_snapshot, job_counts, ready_count = await asyncio.gather(
            self.store.load(identity, "global"),
            self.queue.stats(identity),
            self.queue.ready_count(identity),
        )
        project_snapshot = (
            await self.store.load(identity, "project") if identity.project_id else None
        )
        return MemoryOverview(
            enabled=self.enabled,
            auto_extract_enabled=self.auto_extract,
            user_id=identity.user_id,
            project_id=identity.project_id,
            global_count=len(global_snapshot.memories),
            project_count=len(project_snapshot.memories) if project_snapshot else 0,
            conflict_count=len(global_snapshot.conflicts) + (
                len(project_snapshot.conflicts) if project_snapshot else 0
            ),
            pending_count=job_counts.pending,
            processing_count=job_counts.processing,
            ready_count=ready_count,
            failed_count=job_counts.failed,
            last_error=job_counts.last_error,
            root=str(Path(self.store.root)),
        )

    async def list_records(
        self, identity: MemoryIdentity, scope: MemoryScope | None = None,
    ) -> list[tuple[MemoryScope, MemoryRecord]]:
        scopes: list[MemoryScope] = [scope] if scope else ["global", "project"]
        result: list[tuple[MemoryScope, MemoryRecord]] = []
        async with self._management_lock:
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
        async with self._management_lock:
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
        async with self._management_lock:
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
                kind = record.kind if record is not None else conflict.kind  # type: ignore[union-attr]
                await self.store.forget(
                    identity,
                    scope=current,
                    key=target_key,
                    kind=kind,
                    session_id=session_id,
                    source_hash=f"command:{uuid.uuid4().hex}",
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
        async with self._management_lock:
            count = await self.store.clear(identity, scopes=scopes, session_id=session_id)
        if count:
            self._emit("memory_record_changed", action="cleared", count=count)
        return count

    async def close(self, *, grace_seconds: float = 0.25) -> None:
        task = self._worker_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, grace_seconds))
            except TimeoutError:
                if task.cancelling() == 0:
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                task.cancel()
                raise
        event_tasks = [item for item in self._event_tasks if not item.done()]
        if event_tasks:
            await asyncio.gather(*event_tasks, return_exceptions=True)

    def request_stop(self) -> None:
        task = self._worker_task
        if task is not None and not task.done():
            task.cancel()

    async def _release_claimed_job(self, job: PendingMemoryJob) -> None:
        """Finish the durable release even if shutdown cancels us a second time."""
        release_task = asyncio.create_task(
            self.queue.release(job, owner_id=self._worker_id)
        )
        try:
            await asyncio.shield(release_task)
        except asyncio.CancelledError:
            await release_task
            raise

    def _start_worker(self, identity: MemoryIdentity) -> None:
        if not self.enabled or not self.auto_extract:
            return
        task = self._worker_task
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._worker_identity = identity
        task = loop.create_task(self.process_pending(identity, max_jobs=4))
        self._worker_task = task

        def _done(completed: asyncio.Task[ExtractionResult]) -> None:
            if self._worker_task is completed:
                self._worker_task = None
                self._worker_identity = None
            try:
                completed.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(_done)

    async def _emit_queue_state(self, identity: MemoryIdentity) -> None:
        try:
            counts, ready_count = await asyncio.gather(
                self.queue.stats(identity), self.queue.ready_count(identity),
            )
        except Exception:
            return
        self._emit(
            "memory_queue_changed",
            pending_count=counts.pending,
            processing_count=counts.processing,
            ready_count=ready_count,
            failed_count=counts.failed,
            last_error=counts.last_error,
        )

    @staticmethod
    def _count_apply_result(result: ExtractionResult, action: str) -> None:
        if action == "duplicate":
            result.duplicated += 1
        elif action == "conflict":
            result.conflicts += 1
        elif action == "rejected":
            result.rejected += 1
        elif action != "noop":
            result.accepted += 1

    @staticmethod
    def _merge_result(target: ExtractionResult, source: ExtractionResult) -> None:
        target.queued += source.queued
        target.candidates += source.candidates
        target.accepted += source.accepted
        target.rejected += source.rejected
        target.duplicated += source.duplicated
        target.conflicts += source.conflicts
        target.errors.extend(source.errors)

    def _emit(self, event_type: str, **payload: Any) -> None:
        if self._event_sink is None:
            return
        try:
            result = self._event_sink({"type": event_type, **payload})
            if hasattr(result, "__await__"):
                task = asyncio.create_task(result)
                self._event_tasks.add(task)
                task.add_done_callback(self._event_tasks.discard)
        except Exception:
            pass


__all__ = ["MemoryService"]
