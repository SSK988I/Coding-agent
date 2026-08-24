"""File-backed long-term memory for Coding Agent."""

from coding_agent.memory.interfaces import MemoryExtractor, MemoryStore
from coding_agent.memory.types import (
    ApplyResult,
    CompletedTask,
    ExtractionResult,
    MemoryCandidate,
    MemoryConflict,
    MemoryContext,
    MemoryEvidence,
    MemoryIdentity,
    MemoryOverview,
    MemoryRecord,
    MemorySnapshot,
    MemoryTombstone,
)

__all__ = [
    "ApplyResult",
    "CompletedTask",
    "ExtractionResult",
    "MemoryCandidate",
    "MemoryConflict",
    "MemoryContext",
    "MemoryEvidence",
    "MemoryExtractor",
    "MemoryIdentity",
    "MemoryOverview",
    "MemoryRecord",
    "MemorySnapshot",
    "MemoryStore",
    "MemoryTombstone",
]
