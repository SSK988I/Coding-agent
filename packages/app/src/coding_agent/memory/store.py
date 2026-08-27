"""YAML materialized snapshots backed by append-only JSONL fact logs."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time
from typing import Any, Iterator

import yaml  # pyright: ignore[reportMissingModuleSource]

from coding_agent.memory.conflicts import resolve_candidate
from coding_agent.memory.paths import MemoryPaths, paths_for
from coding_agent.memory.types import (
    ApplyResult,
    MemoryCandidate,
    MemoryConflict,
    MemoryIdentity,
    MemoryRecord,
    MemoryScope,
    MemorySnapshot,
    MemoryTombstone,
    utc_now,
    validate_candidate,
)


class MemoryStoreError(RuntimeError):
    pass


class StaleMemoryStoreWriteError(MemoryStoreError):
    """Raised when a consolidation target changed before its write committed."""


_EVENT_TYPES = {
    "candidate_rejected",
    "memory_created",
    "memory_reinforced",
    "memory_superseded",
    "memory_conflict_detected",
    "memory_conflict_resolved",
    "memory_tombstoned",
}
_EVENT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


def _validate_event(event: dict[str, Any], path: Path, line_number: int) -> None:
    label = f"{path}:{line_number}"
    event_type = event.get("type")
    revision = event.get("revision")
    schema_version = event.get("schema_version")
    if isinstance(schema_version, bool) or schema_version not in {1, 2}:
        raise MemoryStoreError(f"unsupported memory event schema at {label}")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise MemoryStoreError(f"invalid memory event revision at {label}")
    if event_type not in _EVENT_TYPES:
        raise MemoryStoreError(f"unknown memory event type at {label}: {event_type!r}")
    key = event.get("key")
    if not isinstance(key, str) or len(key) > 120 or not _EVENT_KEY_RE.fullmatch(key):
        raise MemoryStoreError(f"invalid memory event key at {label}")
    source_hash = event.get("source_hash")
    if not isinstance(source_hash, str) or not source_hash:
        raise MemoryStoreError(f"invalid memory event source hash at {label}")
    timestamp = event.get("timestamp")
    try:
        if not isinstance(timestamp, str):
            raise ValueError
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryStoreError(f"invalid memory event timestamp at {label}") from exc

    common = {"schema_version", "revision", "type", "timestamp", "source_hash", "key"}
    extra_by_type = {
        "candidate_rejected": {"reason", "active_record_id"},
        "memory_created": {"record"},
        "memory_reinforced": {"record"},
        "memory_superseded": {"previous_id", "record"},
        "memory_conflict_detected": {"conflict"},
        "memory_conflict_resolved": {"record"},
        "memory_tombstoned": {"tombstone"},
    }
    allowed = common | extra_by_type[str(event_type)]
    if set(event) - allowed:
        raise MemoryStoreError(f"memory event has unknown fields at {label}")
    required_by_type = {
        "candidate_rejected": {"reason"},
        "memory_created": {"record"},
        "memory_reinforced": {"record"},
        "memory_superseded": {"previous_id", "record"},
        "memory_conflict_detected": {"conflict"},
        "memory_conflict_resolved": {"record"},
        "memory_tombstoned": {"tombstone"},
    }
    if not required_by_type[str(event_type)] <= set(event):
        raise MemoryStoreError(f"memory event is missing fields at {label}")
    if event_type == "candidate_rejected" and not isinstance(event.get("reason"), str):
        raise MemoryStoreError(f"memory rejection reason is invalid at {label}")
    if event_type == "memory_superseded" and not isinstance(event.get("previous_id"), str):
        raise MemoryStoreError(f"memory superseded source is invalid at {label}")


class _CrossProcessLock:
    def __init__(self, path: Path, timeout: float = 5.0) -> None:
        self.path = path
        self.timeout = timeout
        self._handle: Any = None

    def __enter__(self) -> "_CrossProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._handle.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise TimeoutError(f"memory lock timed out: {self.path}") from exc
                time.sleep(0.05)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve()).casefold()
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _canonical_hash(snapshot: MemorySnapshot) -> str:
    payload = json.dumps(
        snapshot.to_dict(include_hash=False),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _apply_event(snapshot: MemorySnapshot, event: dict[str, Any]) -> MemorySnapshot:
    result = copy.deepcopy(snapshot)
    event_type = str(event.get("type", ""))
    key = str(event.get("key", ""))
    if event_type in {
        "memory_created",
        "memory_reinforced",
        "memory_superseded",
        "memory_conflict_resolved",
    }:
        record = MemoryRecord.from_dict(event["record"])
        if record.key != key or record.status != "active":
            raise MemoryStoreError("memory event record does not match its key or state")
        result.memories[key] = record
        result.conflicts.pop(key, None)
        result.tombstones.pop(key, None)
    elif event_type == "memory_conflict_detected":
        conflict = MemoryConflict.from_dict(event["conflict"])
        if conflict.key != key or conflict.status != "open":
            raise MemoryStoreError("memory conflict event does not match its key or state")
        result.memories.pop(key, None)
        result.conflicts[key] = conflict
    elif event_type == "memory_tombstoned":
        tombstone = MemoryTombstone.from_dict(event["tombstone"])
        if tombstone.key != key:
            raise MemoryStoreError("memory tombstone event does not match its key")
        result.memories.pop(key, None)
        result.conflicts.pop(key, None)
        result.tombstones[key] = tombstone
    source_hash = str(event.get("source_hash", ""))
    if source_hash and source_hash not in result.applied_source_hashes:
        result.applied_source_hashes.append(source_hash)
    result.schema_version = max(result.schema_version, int(event.get("schema_version", 1)))
    result.revision = int(event.get("revision", result.revision))
    result.updated_at = str(event.get("timestamp", result.updated_at))
    result.content_hash = _canonical_hash(result)
    return result


class FileMemoryStore:
    """Transactional-enough local store using lock + fsync + atomic replace."""

    def __init__(self, root: Path, *, lock_timeout: float = 5.0) -> None:
        self.root = root.expanduser().resolve()
        self.lock_timeout = lock_timeout

    async def load(self, identity: MemoryIdentity, scope: MemoryScope) -> MemorySnapshot:
        return await asyncio.to_thread(self._load_with_lock, identity, scope)

    async def apply(
        self,
        identity: MemoryIdentity,
        candidate: MemoryCandidate,
        *,
        session_id: str,
        expected_record_id: str | None = None,
    ) -> ApplyResult:
        return await asyncio.to_thread(
            self._apply_with_lock,
            identity,
            candidate,
            session_id,
            expected_record_id,
        )

    async def forget(
        self,
        identity: MemoryIdentity,
        *,
        scope: MemoryScope,
        key: str,
        kind: str | None,
        session_id: str,
        source_hash: str,
    ) -> ApplyResult:
        candidate = MemoryCandidate(
            operation="retract",
            kind=kind,  # type: ignore[arg-type]
            scope=scope,
            key=key,
            confidence=1.0,
            source_kind="explicit_correction",
            evidence_entry_ids=[f"command:{source_hash}"],
            source_timestamp=utc_now(),
            source_hash=source_hash,
        )
        return await self.apply(identity, candidate, session_id=session_id)

    async def clear(
        self,
        identity: MemoryIdentity,
        *,
        scopes: list[MemoryScope],
        session_id: str,
    ) -> int:
        count = 0
        for scope in scopes:
            snapshot = await self.load(identity, scope)
            keys = sorted(set(snapshot.memories) | set(snapshot.conflicts))
            for key in keys:
                record = snapshot.memories.get(key)
                conflict = snapshot.conflicts.get(key)
                kind = record.kind if record else (conflict.kind if conflict else "preference")
                digest = hashlib.sha256(
                    f"clear:{identity.user_id}:{identity.project_id}:{scope}:{key}:{utc_now()}".encode()
                ).hexdigest()
                await self.forget(
                    identity,
                    scope=scope,
                    key=key,
                    kind=kind,
                    session_id=session_id,
                    source_hash=f"sha256:{digest}",
                )
                count += 1
        return count

    @contextmanager
    def _with_lock(self, paths: MemoryPaths) -> Iterator[None]:
        thread_lock = _thread_lock(paths.lock)
        thread_lock.acquire()
        try:
            with _CrossProcessLock(paths.lock, self.lock_timeout):
                yield
        finally:
            thread_lock.release()

    def _load_with_lock(self, identity: MemoryIdentity, scope: MemoryScope) -> MemorySnapshot:
        paths = paths_for(identity, scope, root=self.root)
        with self._with_lock(paths):
            return self._load_locked(identity, scope, paths)

    def _apply_with_lock(
        self,
        identity: MemoryIdentity,
        candidate: MemoryCandidate,
        session_id: str,
        expected_record_id: str | None,
    ) -> ApplyResult:
        validate_candidate(candidate, require_source_hash=True)
        if candidate.scope not in {"global", "project"}:
            raise ValueError("candidate scope is required")
        scope = candidate.scope
        paths = paths_for(identity, scope, root=self.root)
        with self._with_lock(paths):
            # A crash can leave an incomplete final JSON object. Repair it
            # while holding the same lock used by append, otherwise the next
            # event would be joined to that fragment and corrupt the log.
            self._repair_event_tail(paths.events)
            snapshot = self._load_locked(identity, scope, paths)
            if expected_record_id is not None:
                active = snapshot.memories.get(candidate.key)
                conflict = snapshot.conflicts.get(candidate.key)
                target_ids = {active.id} if active is not None else set()
                if conflict is not None:
                    target_ids.update(item.id for item in conflict.candidates)
                if expected_record_id not in target_ids:
                    raise StaleMemoryStoreWriteError(
                        "memory consolidation target changed before apply: "
                        f"{expected_record_id}"
                    )
            resolution = resolve_candidate(snapshot, candidate, session_id=session_id)
            if resolution.event_type is None:
                return resolution.result
            next_revision = snapshot.revision + 1
            timestamp = utc_now()
            event = {
                "schema_version": 2,
                "revision": next_revision,
                "type": resolution.event_type,
                "timestamp": timestamp,
                "source_hash": candidate.source_hash,
                **resolution.payload,
            }
            self._ensure_layout(paths)
            self._append_event(paths.events, event)
            updated = resolution.snapshot
            updated.schema_version = 2
            updated.revision = next_revision
            updated.updated_at = timestamp
            updated.content_hash = _canonical_hash(updated)
            self._write_snapshot(paths.snapshot, updated)
            return resolution.result

    def _load_locked(
        self,
        identity: MemoryIdentity,
        scope: MemoryScope,
        paths: MemoryPaths,
    ) -> MemorySnapshot:
        version_path = self.root / "version"
        if version_path.exists():
            try:
                version = version_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise MemoryStoreError(f"cannot read memory store version: {version_path}") from exc
            if version not in {"1", "2"}:
                raise MemoryStoreError(f"unsupported memory store version: {version!r}")
            if version == "1":
                self._ensure_layout(paths)
        events = list(self._read_events(paths.events))
        snapshot: MemorySnapshot | None = None
        snapshot_existed = paths.snapshot.exists()
        snapshot_invalid = False
        if snapshot_existed:
            try:
                raw = yaml.safe_load(paths.snapshot.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("snapshot root must be a mapping")
                candidate = MemorySnapshot.from_dict(raw)
                if candidate.schema_version not in {1, 2}:
                    raise ValueError(f"unsupported memory schema: {candidate.schema_version}")
                if candidate.user_id != identity.user_id or candidate.scope != scope:
                    raise ValueError("snapshot identity or scope mismatch")
                expected_project = identity.project_id if scope == "project" else None
                if candidate.project_id != expected_project:
                    raise ValueError("snapshot project mismatch")
                if candidate.content_hash != _canonical_hash(candidate):
                    raise ValueError("snapshot content hash mismatch")
                snapshot = candidate
            except (OSError, TypeError, ValueError, yaml.YAMLError):
                self._backup_corrupt(paths.snapshot)
                snapshot_invalid = True

        # JSONL is authoritative. Replay it from revision zero even when the
        # YAML cache is self-consistent, then compare the materialized hashes.
        authoritative = MemorySnapshot.empty(identity, scope)
        for event in events:
            revision = int(event.get("revision", 0))
            if revision != authoritative.revision + 1:
                raise MemoryStoreError(
                    f"memory event revision gap in {paths.events}: "
                    f"expected {authoritative.revision + 1}, got {revision}"
                )
            authoritative = _apply_event(authoritative, event)

        if not events:
            valid_empty_snapshot = (
                snapshot is not None
                and snapshot.revision == 0
                and not snapshot.memories
                and not snapshot.tombstones
                and not snapshot.conflicts
                and not snapshot.applied_source_hashes
            )
            if valid_empty_snapshot:
                assert snapshot is not None
                if snapshot.schema_version == 1:
                    snapshot.schema_version = 2
                    snapshot.content_hash = _canonical_hash(snapshot)
                    self._ensure_layout(paths)
                    self._write_snapshot(paths.snapshot, snapshot)
                return snapshot
            if snapshot is not None and not snapshot_invalid:
                self._backup_corrupt(paths.snapshot)
            if snapshot_existed:
                authoritative.content_hash = _canonical_hash(authoritative)
                self._ensure_layout(paths)
                self._write_snapshot(paths.snapshot, authoritative)
            return authoritative

        if (
            snapshot is not None
            and snapshot.revision == authoritative.revision
            and snapshot.content_hash == authoritative.content_hash
        ):
            return snapshot

        if (
            snapshot is not None
            and snapshot.schema_version == 1
            and snapshot.revision == authoritative.revision
        ):
            migrated = copy.deepcopy(snapshot)
            migrated.schema_version = 2
            migrated.content_hash = _canonical_hash(migrated)
            if migrated.content_hash == authoritative.content_hash:
                self._ensure_layout(paths)
                self._write_snapshot(paths.snapshot, authoritative)
                return authoritative

        if snapshot is not None and not snapshot_invalid:
            self._backup_corrupt(paths.snapshot)
        authoritative.content_hash = _canonical_hash(authoritative)
        self._ensure_layout(paths)
        self._write_snapshot(paths.snapshot, authoritative)
        return authoritative

    def _ensure_layout(self, paths: MemoryPaths) -> None:
        paths.directory.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        version_path = self.root / "version"
        current_version = None
        if version_path.exists():
            current_version = version_path.read_text(encoding="utf-8").strip()
            if current_version not in {"1", "2"}:
                raise MemoryStoreError(
                    f"unsupported memory store version: {current_version!r}"
                )
        if current_version != "2":
            temporary = version_path.with_name(
                f".{version_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            temporary.write_text("2\n", encoding="utf-8")
            os.replace(temporary, version_path)
        if sys.platform != "win32":
            for path in (self.root, paths.directory):
                try:
                    path.chmod(0o700)
                except OSError:
                    pass

    @staticmethod
    def _append_event(path: Path, event: dict[str, Any]) -> None:
        line = json.dumps(
            event, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ) + "\n"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _repair_event_tail(path: Path) -> None:
        """Make the final JSONL record append-safe after an interrupted write.

        A syntactically complete event without its trailing newline is durable
        data, so retain it and add the delimiter. A syntactically incomplete
        tail is the only recoverable corruption and is truncated back to the
        last newline. Structurally invalid complete events still fail closed.
        """
        if not path.exists():
            return
        with path.open("r+b") as handle:
            data = handle.read()
            if not data or data.endswith(b"\n"):
                return
            last_newline = data.rfind(b"\n")
            tail_start = last_newline + 1
            tail = data[tail_start:]
            if not tail.strip():
                handle.seek(tail_start)
                handle.truncate()
            else:
                try:
                    value = json.loads(tail.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    handle.seek(tail_start)
                    handle.truncate()
                else:
                    line_number = data[:tail_start].count(b"\n") + 1
                    if not isinstance(value, dict):
                        raise MemoryStoreError(
                            f"memory event must be an object at {path}:{line_number}"
                        )
                    _validate_event(value, path, line_number)
                    handle.seek(0, os.SEEK_END)
                    handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _read_events(path: Path) -> Iterator[dict[str, Any]]:
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                if index == len(lines) - 1:
                    break
                raise MemoryStoreError(f"invalid memory event at {path}:{index + 1}") from exc
            if not isinstance(value, dict):
                raise MemoryStoreError(f"memory event must be an object at {path}:{index + 1}")
            _validate_event(value, path, index + 1)
            yield value

    @staticmethod
    def _write_snapshot(path: Path, snapshot: MemorySnapshot) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        payload = yaml.safe_dump(
            snapshot.to_dict(),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if sys.platform != "win32":
            try:
                path.chmod(0o600)
            except OSError:
                pass

    @staticmethod
    def _backup_corrupt(path: Path) -> Path | None:
        try:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            backup = path.with_name(
                f"{path.stem}.corrupt-{stamp}-{os.getpid()}-{threading.get_ident()}{path.suffix}"
            )
            shutil.copy2(path, backup)
            return backup
        except OSError:
            return None


__all__ = [
    "FileMemoryStore", "MemoryStoreError", "StaleMemoryStoreWriteError",
]
