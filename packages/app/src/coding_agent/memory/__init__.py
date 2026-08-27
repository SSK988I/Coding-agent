"""File-backed long-term memory for Coding Agent."""

from coding_agent.memory.interfaces import (
    MemoryConsolidator,
    MemoryExtractor,
    MemoryJobQueue,
    MemoryStore,
)
from coding_agent.memory.types import (
    ApplyResult,
    CompletedTask,
    ConsolidationDecision,
    ExtractionResult,
    ExtractedMemory,
    MemoryCandidate,
    MemoryConflict,
    MemoryContext,
    MemoryEvidence,
    MemoryIdentity,
    MemoryJobCounts,
    MemoryOverview,
    MemoryRecord,
    MemorySnapshot,
    MemoryTombstone,
    PendingMemoryJob,
)

__all__ = [
    "ApplyResult",
    "CompletedTask",
    "ConsolidationDecision",
    "ExtractionResult",
    "ExtractedMemory",
    "MemoryCandidate",
    "MemoryConsolidator",
    "MemoryConflict",
    "MemoryContext",
    "MemoryEvidence",
    "MemoryExtractor",
    "MemoryIdentity",
    "MemoryJobCounts",
    "MemoryJobQueue",
    "MemoryOverview",
    "MemoryRecord",
    "MemorySnapshot",
    "MemoryStore",
    "MemoryTombstone",
    "PendingMemoryJob",
]
