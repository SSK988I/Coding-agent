"""Structural interfaces for the long-term memory runtime."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from coding_agent.memory.types import (
    ApplyResult,
    CompletedTask,
    MemoryCandidate,
    ConsolidationDecision,
    ExtractedMemory,
    MemoryIdentity,
    MemoryJobCounts,
    MemoryJobStage,
    MemoryRecord,
    MemoryScope,
    MemorySnapshot,
    PendingMemoryJob,
)


class MemoryStore(Protocol):
    root: Path

    async def load(
        self, identity: MemoryIdentity, scope: MemoryScope,
    ) -> MemorySnapshot: ...

    async def apply(
        self,
        identity: MemoryIdentity,
        candidate: MemoryCandidate,
        *,
        session_id: str,
        expected_record_id: str | None = None,
    ) -> ApplyResult: ...

    async def forget(
        self,
        identity: MemoryIdentity,
        *,
        scope: MemoryScope,
        key: str,
        kind: str | None,
        session_id: str,
        source_hash: str,
    ) -> ApplyResult: ...

    async def clear(
        self,
        identity: MemoryIdentity,
        *,
        scopes: list[MemoryScope],
        session_id: str,
    ) -> int: ...


class MemoryExtractor(Protocol):
    async def extract(self, task: CompletedTask) -> list[ExtractedMemory]: ...


class MemoryConsolidator(Protocol):
    async def consolidate(
        self,
        task: CompletedTask,
        extracted: list[ExtractedMemory],
        existing: list[tuple[MemoryScope, MemoryRecord]],
    ) -> list[ConsolidationDecision]: ...


class MemoryJobQueue(Protocol):
    async def enqueue(self, task: CompletedTask) -> PendingMemoryJob: ...

    async def claim(
        self, identity: MemoryIdentity, *, owner_id: str,
    ) -> PendingMemoryJob | None: ...

    async def checkpoint(
        self,
        job: PendingMemoryJob,
        *,
        owner_id: str,
        stage: MemoryJobStage,
        extracted_payload: list[dict[str, Any]] | None = None,
        consolidation_payload: list[dict[str, Any]] | None = None,
    ) -> PendingMemoryJob: ...

    async def complete(
        self,
        job: PendingMemoryJob,
        *,
        owner_id: str,
        result: dict[str, Any],
    ) -> None: ...

    async def fail(
        self,
        job: PendingMemoryJob,
        *,
        owner_id: str,
        error: str,
        retryable: bool = True,
        reset_stage: MemoryJobStage | None = None,
    ) -> PendingMemoryJob: ...

    async def release(self, job: PendingMemoryJob, *, owner_id: str) -> None: ...

    async def stats(self, identity: MemoryIdentity) -> MemoryJobCounts: ...

    async def ready_count(self, identity: MemoryIdentity) -> int: ...


__all__ = [
    "MemoryConsolidator", "MemoryExtractor", "MemoryJobQueue", "MemoryStore",
]
