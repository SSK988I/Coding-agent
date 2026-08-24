"""Structural interfaces for the long-term memory runtime."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from coding_agent.memory.types import (
    ApplyResult,
    CompletedTask,
    MemoryCandidate,
    MemoryIdentity,
    MemoryScope,
    MemorySnapshot,
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
    async def extract(self, task: CompletedTask) -> list[MemoryCandidate]: ...


__all__ = ["MemoryExtractor", "MemoryStore"]
