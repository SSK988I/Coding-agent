"""Domain types for file-backed long-term memory."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
import json
import re
from typing import Any, Literal

MemoryKind = Literal["preference", "decision", "constraint", "convention"]
MemoryScope = Literal["global", "project"]
MemoryStatus = Literal["active", "superseded", "conflicted", "tombstoned"]
MemoryOperationName = Literal["upsert", "retract", "noop"]
MemorySourceKind = Literal[
    "explicit_correction",
    "explicit_user",
    "accepted_plan",
    "inferred_user",
]

_KINDS = {"preference", "decision", "constraint", "convention"}
_SCOPES = {"global", "project"}
_STATUSES = {"active", "superseded", "conflicted", "tombstoned"}
_AUTHORITIES = {"explicit_correction", "explicit_user", "accepted_plan", "inferred_user"}
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_SENSITIVE_KEY_PARTS = {
    "api_key", "authorization", "biometric", "card", "cookie", "credential",
    "credentials", "ethnicity", "health", "medical", "password", "payment",
    "private_key", "recovery_code", "religion", "secret", "sexuality", "ssn", "token",
}
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),
    re.compile(
        r"\b(?:password|passwd|api[_ -]?key|access[_ -]?token|secret|cookie|set-cookie|"
        r"recovery[_ -]?code)\s*[:=]\s*\S{8,}",
        re.IGNORECASE,
    ),
    re.compile(r"\bauthorization\s*:\s*bearer\s+\S{8,}", re.IGNORECASE),
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
]


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _strict_dataclass_payload(value: Any, cls: type[Any], label: str) -> dict[str, Any]:
    payload = dict(_mapping(value, label))
    allowed = {item.name for item in fields(cls)}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"{label} has unknown fields: {sorted(unknown)}")
    return payload


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def contains_sensitive_data(value: Any) -> bool:
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return True
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def contains_sensitive_key(key: str) -> bool:
    return any(part in _SENSITIVE_KEY_PARTS for part in key.casefold().split("."))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class MemoryIdentity:
    user_id: str
    project_id: str | None = None


@dataclass
class MemoryEvidence:
    id: str
    text: str
    source_kind: Literal["user", "accepted_plan", "plan_completed"]
    timestamp: str


@dataclass
class CompletedTask:
    identity: MemoryIdentity
    session_id: str
    evidence: list[MemoryEvidence]
    final_response: str = ""
    mode: str = "default"
    success: bool = True
    ended_at: str = field(default_factory=utc_now)


@dataclass
class MemoryCandidate:
    operation: MemoryOperationName
    kind: MemoryKind | None = None
    scope: MemoryScope | None = None
    key: str = ""
    value: Any = None
    summary: str = ""
    confidence: float = 0.0
    source_kind: MemorySourceKind = "inferred_user"
    evidence_entry_ids: list[str] = field(default_factory=list)
    source_timestamp: str = field(default_factory=utc_now)
    source_hash: str = ""


def validate_candidate(candidate: MemoryCandidate, *, require_source_hash: bool = False) -> None:
    if candidate.operation not in {"upsert", "retract", "noop"}:
        raise ValueError("memory candidate operation is invalid")
    if candidate.scope not in _SCOPES:
        raise ValueError("memory candidate scope is invalid")
    if candidate.kind is not None and candidate.kind not in _KINDS:
        raise ValueError("memory candidate kind is invalid")
    if candidate.operation == "upsert" and candidate.kind is None:
        raise ValueError("upsert memory candidate requires kind")
    if (
        not isinstance(candidate.key, str)
        or len(candidate.key) > 120
        or not _KEY_RE.fullmatch(candidate.key)
    ):
        raise ValueError("memory candidate key is invalid")
    if contains_sensitive_key(candidate.key):
        raise ValueError("memory candidate key is sensitive")
    if not isinstance(candidate.summary, str) or len(candidate.summary) > 300:
        raise ValueError("memory candidate summary is invalid")
    if candidate.source_kind not in _AUTHORITIES:
        raise ValueError("memory candidate authority is invalid")
    if candidate.operation == "retract" and candidate.source_kind not in {
        "explicit_correction", "explicit_user",
    }:
        raise ValueError("memory retraction requires direct user authority")
    if (
        isinstance(candidate.confidence, bool)
        or not isinstance(candidate.confidence, (int, float))
        or not 0 <= float(candidate.confidence) <= 1
    ):
        raise ValueError("memory candidate confidence is invalid")
    if (
        not isinstance(candidate.evidence_entry_ids, list)
        or not candidate.evidence_entry_ids
        or any(not isinstance(item, str) or not item for item in candidate.evidence_entry_ids)
    ):
        raise ValueError("memory candidate evidence IDs are invalid")
    if not _valid_timestamp(candidate.source_timestamp):
        raise ValueError("memory candidate source timestamp is invalid")
    if require_source_hash and (not isinstance(candidate.source_hash, str) or not candidate.source_hash):
        raise ValueError("memory candidate source hash is required")
    try:
        serialized = json.dumps(
            candidate.value, ensure_ascii=False, sort_keys=True, allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("memory candidate value is not JSON serializable") from exc
    if len(serialized) > 2000:
        raise ValueError("memory candidate value is too large")
    if contains_sensitive_data({"value": candidate.value, "summary": candidate.summary}):
        raise ValueError("memory candidate contains sensitive data")


@dataclass
class MemoryRecord:
    id: str
    kind: MemoryKind
    key: str
    value: Any
    summary: str
    status: MemoryStatus
    authority: MemorySourceKind
    confidence: float
    source_session_id: str
    source_entry_ids: list[str]
    source_hash: str
    source_timestamp: str
    supersedes: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_used_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryRecord":
        try:
            record = cls(**_strict_dataclass_payload(value, cls, "memory record"))
        except TypeError as exc:
            raise ValueError(f"invalid memory record: {exc}") from exc
        if not isinstance(record.id, str) or not record.id:
            raise ValueError("memory record id must be non-empty")
        if record.kind not in _KINDS or record.status not in _STATUSES:
            raise ValueError("memory record kind or status is invalid")
        if (
            not isinstance(record.key, str)
            or len(record.key) > 120
            or not _KEY_RE.fullmatch(record.key)
        ):
            raise ValueError("memory record key is invalid")
        if contains_sensitive_key(record.key):
            raise ValueError("memory record key is sensitive")
        if record.authority not in _AUTHORITIES:
            raise ValueError("memory record authority is invalid")
        if (
            isinstance(record.confidence, bool)
            or not isinstance(record.confidence, (int, float))
            or not 0 <= float(record.confidence) <= 1
        ):
            raise ValueError("memory record confidence is invalid")
        if not isinstance(record.summary, str) or not record.summary.strip() or len(record.summary) > 300:
            raise ValueError("memory record summary is invalid")
        if not isinstance(record.source_session_id, str):
            raise ValueError("memory record source session is invalid")
        if (
            not isinstance(record.source_entry_ids, list)
            or any(not isinstance(item, str) for item in record.source_entry_ids)
        ):
            raise ValueError("memory record source entries are invalid")
        if not isinstance(record.source_hash, str) or not record.source_hash:
            raise ValueError("memory record source hash is invalid")
        if record.supersedes is not None and not isinstance(record.supersedes, str):
            raise ValueError("memory record supersedes is invalid")
        if not all(_valid_timestamp(item) for item in (
            record.source_timestamp, record.created_at, record.updated_at,
        )):
            raise ValueError("memory record timestamp is invalid")
        if record.last_used_at is not None and not _valid_timestamp(record.last_used_at):
            raise ValueError("memory record last_used_at is invalid")
        try:
            serialized = json.dumps(
                record.value, ensure_ascii=False, sort_keys=True, allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("memory record value is not JSON serializable") from exc
        if len(serialized) > 2000 or contains_sensitive_data({
            "value": record.value, "summary": record.summary,
        }):
            raise ValueError("memory record value is unsafe or too large")
        return record


@dataclass
class MemoryTombstone:
    key: str
    kind: MemoryKind
    deleted_at: str
    source_session_id: str
    source_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryTombstone":
        try:
            tombstone = cls(**_strict_dataclass_payload(value, cls, "memory tombstone"))
        except TypeError as exc:
            raise ValueError(f"invalid memory tombstone: {exc}") from exc
        if (
            not isinstance(tombstone.key, str)
            or len(tombstone.key) > 120
            or not _KEY_RE.fullmatch(tombstone.key)
        ):
            raise ValueError("memory tombstone key is invalid")
        if tombstone.kind not in _KINDS or not _valid_timestamp(tombstone.deleted_at):
            raise ValueError("memory tombstone kind or timestamp is invalid")
        if not isinstance(tombstone.source_session_id, str) or not isinstance(tombstone.source_hash, str):
            raise ValueError("memory tombstone source is invalid")
        return tombstone


@dataclass
class MemoryConflict:
    key: str
    kind: MemoryKind
    candidates: list[MemoryRecord]
    detected_at: str
    status: Literal["open", "resolved"] = "open"
    resolved_record_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["candidates"] = [candidate.to_dict() for candidate in self.candidates]
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryConflict":
        payload = _strict_dataclass_payload(value, cls, "memory conflict")
        payload["candidates"] = [
            MemoryRecord.from_dict(item) for item in payload.get("candidates", [])
        ]
        try:
            conflict = cls(**payload)
        except TypeError as exc:
            raise ValueError(f"invalid memory conflict: {exc}") from exc
        if (
            not isinstance(conflict.key, str)
            or len(conflict.key) > 120
            or not _KEY_RE.fullmatch(conflict.key)
        ):
            raise ValueError("memory conflict key is invalid")
        if conflict.kind not in _KINDS or conflict.status not in {"open", "resolved"}:
            raise ValueError("memory conflict kind or status is invalid")
        if len(conflict.candidates) < 2 or any(
            candidate.key != conflict.key or candidate.status != "conflicted"
            for candidate in conflict.candidates
        ):
            raise ValueError("memory conflict candidates are invalid")
        if not _valid_timestamp(conflict.detected_at):
            raise ValueError("memory conflict timestamp is invalid")
        return conflict


@dataclass
class MemorySnapshot:
    schema_version: int
    revision: int
    user_id: str
    scope: MemoryScope
    project_id: str | None
    updated_at: str
    memories: dict[str, MemoryRecord] = field(default_factory=dict)
    tombstones: dict[str, MemoryTombstone] = field(default_factory=dict)
    conflicts: dict[str, MemoryConflict] = field(default_factory=dict)
    applied_source_hashes: list[str] = field(default_factory=list)
    content_hash: str = ""

    @classmethod
    def empty(cls, identity: MemoryIdentity, scope: MemoryScope) -> "MemorySnapshot":
        return cls(
            schema_version=1,
            revision=0,
            user_id=identity.user_id,
            scope=scope,
            project_id=identity.project_id if scope == "project" else None,
            updated_at=utc_now(),
        )

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "user_id": self.user_id,
            "scope": self.scope,
            "project_id": self.project_id,
            "updated_at": self.updated_at,
            "memories": {key: record.to_dict() for key, record in self.memories.items()},
            "tombstones": {key: item.to_dict() for key, item in self.tombstones.items()},
            "conflicts": {key: item.to_dict() for key, item in self.conflicts.items()},
            "applied_source_hashes": list(self.applied_source_hashes),
        }
        if include_hash:
            result["content_hash"] = self.content_hash
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemorySnapshot":
        payload = _mapping(value, "memory snapshot")
        allowed = {
            "schema_version", "revision", "user_id", "scope", "project_id", "updated_at",
            "memories", "tombstones", "conflicts", "applied_source_hashes", "content_hash",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"memory snapshot has unknown fields: {sorted(unknown)}")
        try:
            schema_version = payload["schema_version"]
            revision = payload["revision"]
            user_id = payload["user_id"]
            scope = payload["scope"]
            project_id = payload["project_id"]
            updated_at = payload["updated_at"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("memory snapshot metadata is invalid") from exc
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != 1
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
        ):
            raise ValueError("memory snapshot version or revision is invalid")
        if not isinstance(user_id, str) or not user_id or scope not in _SCOPES:
            raise ValueError("memory snapshot identity or scope is invalid")
        if project_id is not None and not isinstance(project_id, str):
            raise ValueError("memory snapshot project id is invalid")
        if (scope == "global" and project_id is not None) or (
            scope == "project" and (not isinstance(project_id, str) or not project_id)
        ):
            raise ValueError("memory snapshot project scope is invalid")
        if not _valid_timestamp(updated_at):
            raise ValueError("memory snapshot timestamp is invalid")
        memories_raw = _mapping(payload.get("memories"), "memory snapshot memories")
        tombstones_raw = _mapping(payload.get("tombstones"), "memory snapshot tombstones")
        conflicts_raw = _mapping(payload.get("conflicts"), "memory snapshot conflicts")
        applied = payload.get("applied_source_hashes")
        if not isinstance(applied, list) or any(not isinstance(item, str) for item in applied):
            raise ValueError("memory snapshot applied hashes are invalid")
        content_hash = payload.get("content_hash")
        if not isinstance(content_hash, str) or not content_hash.startswith("sha256:"):
            raise ValueError("memory snapshot content hash is invalid")
        snapshot = cls(
            schema_version=schema_version,
            revision=revision,
            user_id=user_id,
            scope=scope,
            project_id=project_id,
            updated_at=updated_at,
            memories={
                str(key): MemoryRecord.from_dict(item)
                for key, item in memories_raw.items()
            },
            tombstones={
                str(key): MemoryTombstone.from_dict(item)
                for key, item in tombstones_raw.items()
            },
            conflicts={
                str(key): MemoryConflict.from_dict(item)
                for key, item in conflicts_raw.items()
            },
            applied_source_hashes=list(applied),
            content_hash=content_hash,
        )
        if any(key != record.key or record.status != "active" for key, record in snapshot.memories.items()):
            raise ValueError("memory snapshot active records are invalid")
        if any(key != item.key for key, item in snapshot.tombstones.items()):
            raise ValueError("memory snapshot tombstone keys are invalid")
        if any(key != item.key or item.status != "open" for key, item in snapshot.conflicts.items()):
            raise ValueError("memory snapshot conflict keys are invalid")
        if set(snapshot.memories) & (set(snapshot.tombstones) | set(snapshot.conflicts)):
            raise ValueError("memory snapshot key exists in multiple states")
        return snapshot


@dataclass
class ApplyResult:
    action: str
    record: MemoryRecord | None = None
    conflict: MemoryConflict | None = None
    reason: str | None = None


@dataclass
class MemoryContext:
    records: list[MemoryRecord]
    prompt_block: str
    estimated_tokens: int


@dataclass
class ExtractionResult:
    candidates: int = 0
    accepted: int = 0
    rejected: int = 0
    duplicated: int = 0
    conflicts: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class MemoryOverview:
    enabled: bool
    user_id: str
    project_id: str | None
    global_count: int
    project_count: int
    conflict_count: int
    root: str


__all__ = [
    "ApplyResult",
    "CompletedTask",
    "ExtractionResult",
    "MemoryCandidate",
    "MemoryConflict",
    "MemoryContext",
    "MemoryEvidence",
    "MemoryIdentity",
    "MemoryKind",
    "MemoryOperationName",
    "MemoryOverview",
    "MemoryRecord",
    "MemoryScope",
    "MemorySnapshot",
    "MemorySourceKind",
    "MemoryStatus",
    "MemoryTombstone",
    "utc_now",
    "contains_sensitive_data",
    "contains_sensitive_key",
    "validate_candidate",
]
