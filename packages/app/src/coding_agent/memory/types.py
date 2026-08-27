"""Domain types for file-backed long-term memory."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
import json
import re
from typing import Any, Literal

MemoryKind = Literal[
    "preference", "decision", "constraint", "convention", "fact", "lesson",
]
MemoryScope = Literal["global", "project"]
MemoryStatus = Literal["active", "superseded", "conflicted", "tombstoned"]
MemoryOperationName = Literal["upsert", "retract", "noop"]
ConsolidationActionName = Literal[
    "add", "reinforce", "supersede", "conflict", "ignore",
]
MemoryJobStatus = Literal["pending", "processing", "failed", "completed"]
MemoryJobStage = Literal["extraction", "consolidation", "apply"]
MemorySourceKind = Literal[
    "explicit_correction",
    "explicit_user",
    "accepted_plan",
    "inferred_user",
]

_KINDS = {"preference", "decision", "constraint", "convention", "fact", "lesson"}
_SCOPES = {"global", "project"}
_STATUSES = {"active", "superseded", "conflicted", "tombstoned"}
_AUTHORITIES = {"explicit_correction", "explicit_user", "accepted_plan", "inferred_user"}
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_JOB_ID_RE = re.compile(r"^job_[0-9a-f]{32}$")
_LEGACY_SESSION_TIMESTAMP_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T"
    r"(?P<hour>\d{2})-(?P<minute>\d{2})-(?P<second>\d{2})"
    r"(?:-(?P<fraction>\d{1,6}))?Z$"
)
_SENSITIVE_KEY_PARTS = {
    "api_key", "authorization", "biometric", "card", "cookie", "credential",
    "credentials", "ethnicity", "health", "medical", "password", "payment",
    "private_key", "recovery_code", "religion", "secret", "sexuality", "ssn", "token",
}
_SECRET_PATTERNS = [
    re.compile(
        r"-----BEGIN (?P<pem_type>[A-Z0-9 ]*PRIVATE KEY)-----.*?"
        r"-----END (?P=pem_type)-----",
        re.IGNORECASE | re.DOTALL,
    ),
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


def normalize_timestamp(value: Any) -> str | None:
    """Return a parseable timestamp, migrating legacy session timestamps.

    Older session entries used dashes in the time portion so their values were
    safe to embed directly in filenames.  Session filenames are sanitized by a
    separate function, but those legacy values may still appear in persisted
    memory jobs and must remain readable during the upgrade.
    """
    if not isinstance(value, str) or not value:
        return None
    normalized = value
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        match = _LEGACY_SESSION_TIMESTAMP_RE.fullmatch(value)
        if match is None:
            return None
        fraction = match.group("fraction")
        normalized = (
            f"{match.group('date')}T{match.group('hour')}:"
            f"{match.group('minute')}:{match.group('second')}"
            f"{f'.{fraction}' if fraction else ''}Z"
        )
        try:
            datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            return None
    return normalized


def _valid_timestamp(value: Any) -> bool:
    return normalize_timestamp(value) is not None


def contains_sensitive_data(value: Any) -> bool:
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return True
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def redact_sensitive_text(value: str) -> str:
    """Best-effort secret redaction before transient evidence enters the job queue."""
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


def contains_sensitive_key(key: str) -> bool:
    return any(part in _SENSITIVE_KEY_PARTS for part in key.casefold().split("."))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class MemoryIdentity:
    user_id: str
    project_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryIdentity":
        payload = _strict_dataclass_payload(value, cls, "memory identity")
        try:
            identity = cls(**payload)
        except TypeError as exc:
            raise ValueError(f"invalid memory identity: {exc}") from exc
        if not isinstance(identity.user_id, str) or not identity.user_id:
            raise ValueError("memory identity user id is invalid")
        if identity.project_id is not None and not isinstance(identity.project_id, str):
            raise ValueError("memory identity project id is invalid")
        return identity


@dataclass
class MemoryEvidence:
    id: str
    text: str
    source_kind: Literal["user", "accepted_plan", "plan_completed"]
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryEvidence":
        payload = _strict_dataclass_payload(value, cls, "memory evidence")
        timestamp = normalize_timestamp(payload.get("timestamp"))
        if timestamp is None:
            raise ValueError("memory evidence timestamp is invalid")
        payload["timestamp"] = timestamp
        try:
            evidence = cls(**payload)
        except TypeError as exc:
            raise ValueError(f"invalid memory evidence: {exc}") from exc
        if not isinstance(evidence.id, str) or not evidence.id:
            raise ValueError("memory evidence id is invalid")
        if not isinstance(evidence.text, str) or len(evidence.text) > 100_000:
            raise ValueError("memory evidence text is invalid")
        if evidence.source_kind not in {"user", "accepted_plan", "plan_completed"}:
            raise ValueError("memory evidence source kind is invalid")
        if not _valid_timestamp(evidence.timestamp):
            raise ValueError("memory evidence timestamp is invalid")
        return evidence


@dataclass
class CompletedTask:
    identity: MemoryIdentity
    session_id: str
    evidence: list[MemoryEvidence]
    final_response: str = ""
    mode: str = "default"
    success: bool = True
    ended_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["identity"] = self.identity.to_dict()
        result["evidence"] = [item.to_dict() for item in self.evidence]
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CompletedTask":
        payload = _strict_dataclass_payload(value, cls, "completed memory task")
        ended_at = normalize_timestamp(payload.get("ended_at"))
        if ended_at is None:
            raise ValueError("completed task timestamp is invalid")
        payload["ended_at"] = ended_at
        payload["identity"] = MemoryIdentity.from_dict(
            _mapping(payload.get("identity"), "completed task identity")
        )
        raw_evidence = payload.get("evidence")
        if not isinstance(raw_evidence, list):
            raise ValueError("completed task evidence is invalid")
        payload["evidence"] = [
            MemoryEvidence.from_dict(_mapping(item, "completed task evidence item"))
            for item in raw_evidence
        ]
        try:
            task = cls(**payload)
        except TypeError as exc:
            raise ValueError(f"invalid completed memory task: {exc}") from exc
        if not isinstance(task.session_id, str) or not task.session_id:
            raise ValueError("completed task session id is invalid")
        if not isinstance(task.final_response, str) or len(task.final_response) > 100_000:
            raise ValueError("completed task final response is invalid")
        if not isinstance(task.mode, str) or not task.mode:
            raise ValueError("completed task mode is invalid")
        if not isinstance(task.success, bool) or not _valid_timestamp(task.ended_at):
            raise ValueError("completed task status is invalid")
        return task


@dataclass
class ExtractedMemory:
    """One durable, evidence-backed statement before store consolidation."""

    kind: MemoryKind
    scope: MemoryScope
    content: str
    value: Any
    confidence: float
    source_kind: MemorySourceKind
    evidence_entry_ids: list[str]
    source_timestamp: str
    relation_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ExtractedMemory":
        payload = _strict_dataclass_payload(value, cls, "extracted memory")
        source_timestamp = normalize_timestamp(payload.get("source_timestamp"))
        if source_timestamp is None:
            raise ValueError("extracted memory timestamp is invalid")
        payload["source_timestamp"] = source_timestamp
        try:
            memory = cls(**payload)
        except TypeError as exc:
            raise ValueError(f"invalid extracted memory: {exc}") from exc
        validate_extracted_memory(memory)
        return memory


def validate_extracted_memory(memory: ExtractedMemory) -> None:
    if memory.kind not in _KINDS or memory.scope not in _SCOPES:
        raise ValueError("extracted memory kind or scope is invalid")
    if not isinstance(memory.content, str) or not memory.content.strip() or len(memory.content) > 300:
        raise ValueError("extracted memory content is invalid")
    if memory.relation_key is not None and (
        len(memory.relation_key) > 120 or not _KEY_RE.fullmatch(memory.relation_key)
    ):
        raise ValueError("extracted memory relation key is invalid")
    if memory.relation_key is not None and contains_sensitive_key(memory.relation_key):
        raise ValueError("extracted memory relation key is sensitive")
    if memory.source_kind not in _AUTHORITIES:
        raise ValueError("extracted memory source kind is invalid")
    if (
        isinstance(memory.confidence, bool)
        or not isinstance(memory.confidence, (int, float))
        or not 0 <= float(memory.confidence) <= 1
    ):
        raise ValueError("extracted memory confidence is invalid")
    if (
        not isinstance(memory.evidence_entry_ids, list)
        or not memory.evidence_entry_ids
        or any(not isinstance(item, str) or not item for item in memory.evidence_entry_ids)
    ):
        raise ValueError("extracted memory evidence ids are invalid")
    if not _valid_timestamp(memory.source_timestamp):
        raise ValueError("extracted memory timestamp is invalid")
    try:
        serialized = json.dumps(memory.value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("extracted memory value is not JSON serializable") from exc
    if len(serialized) > 2000 or contains_sensitive_data({
        "content": memory.content, "value": memory.value,
    }):
        raise ValueError("extracted memory is unsafe or too large")


@dataclass(frozen=True)
class ConsolidationDecision:
    candidate_index: int
    action: ConsolidationActionName
    target_record_id: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ConsolidationDecision":
        payload = _strict_dataclass_payload(value, cls, "memory consolidation decision")
        try:
            decision = cls(**payload)
        except TypeError as exc:
            raise ValueError(f"invalid memory consolidation decision: {exc}") from exc
        if (
            isinstance(decision.candidate_index, bool)
            or not isinstance(decision.candidate_index, int)
            or decision.candidate_index < 0
        ):
            raise ValueError("memory consolidation candidate index is invalid")
        if decision.action not in {
            "add", "reinforce", "supersede", "conflict", "ignore",
        }:
            raise ValueError("memory consolidation action is invalid")
        if decision.target_record_id is not None and not isinstance(
            decision.target_record_id, str,
        ):
            raise ValueError("memory consolidation target is invalid")
        if not isinstance(decision.reason, str) or len(decision.reason) > 300:
            raise ValueError("memory consolidation reason is invalid")
        return decision


@dataclass
class PendingMemoryJob:
    id: str
    task: CompletedTask
    status: MemoryJobStatus = "pending"
    stage: MemoryJobStage = "extraction"
    attempts: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    next_attempt_at: str | None = None
    owner_id: str | None = None
    owner_pid: int | None = None
    lease_expires_at: str | None = None
    last_error: str | None = None
    extracted_payload: list[dict[str, Any]] | None = None
    consolidation_payload: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["task"] = self.task.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PendingMemoryJob":
        payload = _strict_dataclass_payload(value, cls, "pending memory job")
        payload["task"] = CompletedTask.from_dict(
            _mapping(payload.get("task"), "pending memory job task")
        )
        for field_name in ("created_at", "updated_at"):
            timestamp = normalize_timestamp(payload.get(field_name))
            if timestamp is None:
                raise ValueError("pending memory job timestamp is invalid")
            payload[field_name] = timestamp
        for field_name in ("next_attempt_at", "lease_expires_at"):
            raw_timestamp = payload.get(field_name)
            if raw_timestamp is None:
                continue
            timestamp = normalize_timestamp(raw_timestamp)
            if timestamp is None:
                raise ValueError("pending memory job optional timestamp is invalid")
            payload[field_name] = timestamp
        try:
            job = cls(**payload)
        except TypeError as exc:
            raise ValueError(f"invalid pending memory job: {exc}") from exc
        if not isinstance(job.id, str) or not _JOB_ID_RE.fullmatch(job.id):
            raise ValueError("pending memory job id is invalid")
        if job.status not in {"pending", "processing", "failed", "completed"}:
            raise ValueError("pending memory job status is invalid")
        if job.stage not in {"extraction", "consolidation", "apply"}:
            raise ValueError("pending memory job stage is invalid")
        if isinstance(job.attempts, bool) or not isinstance(job.attempts, int) or job.attempts < 0:
            raise ValueError("pending memory job attempts are invalid")
        if not _valid_timestamp(job.created_at) or not _valid_timestamp(job.updated_at):
            raise ValueError("pending memory job timestamp is invalid")
        for timestamp in (job.next_attempt_at, job.lease_expires_at):
            if timestamp is not None and not _valid_timestamp(timestamp):
                raise ValueError("pending memory job optional timestamp is invalid")
        if job.owner_pid is not None and (
            isinstance(job.owner_pid, bool) or not isinstance(job.owner_pid, int) or job.owner_pid <= 0
        ):
            raise ValueError("pending memory job owner pid is invalid")
        if job.last_error is not None and (
            not isinstance(job.last_error, str) or len(job.last_error) > 1000
        ):
            raise ValueError("pending memory job error is invalid")
        for checkpoint in (job.extracted_payload, job.consolidation_payload):
            if checkpoint is not None and (
                not isinstance(checkpoint, list)
                or any(not isinstance(item, dict) for item in checkpoint)
            ):
                raise ValueError("pending memory job checkpoint is invalid")
        return job


@dataclass(frozen=True)
class MemoryJobCounts:
    pending: int = 0
    processing: int = 0
    failed: int = 0
    last_error: str | None = None


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
    relation_key: str | None = None
    fingerprint: str = ""


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
    if candidate.relation_key is not None and (
        len(candidate.relation_key) > 120 or not _KEY_RE.fullmatch(candidate.relation_key)
    ):
        raise ValueError("memory candidate relation key is invalid")
    if candidate.relation_key is not None and contains_sensitive_key(candidate.relation_key):
        raise ValueError("memory candidate relation key is sensitive")
    if not isinstance(candidate.fingerprint, str):
        raise ValueError("memory candidate fingerprint is invalid")
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
    relation_key: str | None = None
    fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        # Optional v2 fields are omitted at their defaults so v1 snapshot hashes
        # remain valid and old YAML caches do not look corrupt after an upgrade.
        if self.relation_key is None:
            result.pop("relation_key", None)
        if not self.fingerprint:
            result.pop("fingerprint", None)
        return result

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
        if record.relation_key is not None and (
            len(record.relation_key) > 120 or not _KEY_RE.fullmatch(record.relation_key)
        ):
            raise ValueError("memory record relation key is invalid")
        if record.relation_key is not None and contains_sensitive_key(record.relation_key):
            raise ValueError("memory record relation key is sensitive")
        if not isinstance(record.fingerprint, str):
            raise ValueError("memory record fingerprint is invalid")
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
            schema_version=2,
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
            or schema_version not in {1, 2}
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
    queued: int = 0
    job_id: str | None = None
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
    auto_extract_enabled: bool = True
    pending_count: int = 0
    processing_count: int = 0
    ready_count: int = 0
    failed_count: int = 0
    last_error: str | None = None


__all__ = [
    "ApplyResult",
    "CompletedTask",
    "ConsolidationActionName",
    "ConsolidationDecision",
    "ExtractionResult",
    "ExtractedMemory",
    "MemoryCandidate",
    "MemoryConflict",
    "MemoryContext",
    "MemoryEvidence",
    "MemoryIdentity",
    "MemoryJobCounts",
    "MemoryJobStage",
    "MemoryJobStatus",
    "MemoryKind",
    "MemoryOperationName",
    "MemoryOverview",
    "MemoryRecord",
    "MemoryScope",
    "MemorySnapshot",
    "MemorySourceKind",
    "MemoryStatus",
    "MemoryTombstone",
    "PendingMemoryJob",
    "normalize_timestamp",
    "utc_now",
    "contains_sensitive_data",
    "contains_sensitive_key",
    "redact_sensitive_text",
    "validate_candidate",
    "validate_extracted_memory",
]
