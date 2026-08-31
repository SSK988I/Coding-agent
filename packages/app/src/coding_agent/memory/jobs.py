"""Durable, lease-based background jobs for long-term memory generation."""
from __future__ import annotations

import asyncio
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
from typing import Any, Iterator

from coding_agent.memory.paths import paths_for
from coding_agent.memory.store import _CrossProcessLock, _thread_lock
from coding_agent.memory.types import (
    CompletedTask,
    MemoryEvidence,
    MemoryIdentity,
    MemoryJobCounts,
    MemoryJobStage,
    PendingMemoryJob,
    normalize_timestamp,
    redact_sensitive_text,
    utc_now,
)


class MemoryJobQueueError(RuntimeError):
    pass


_JOB_FILE_RE = re.compile(r"^job_[0-9a-f]{32}\.json$")


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp_after(seconds: float) -> str:
    value = datetime.now(timezone.utc) + timedelta(seconds=max(0.0, seconds))
    return value.isoformat().replace("+00:00", "Z")


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_pid_alive(pid: int) -> bool:
    """Probe a Windows process without sending a destructive signal."""
    if pid <= 0 or pid > 0xFFFFFFFF:
        return False

    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    error_access_denied = 5

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        # A protected process may deny SYNCHRONIZE access while still running.
        return ctypes.get_last_error() == error_access_denied
    try:
        result = kernel32.WaitForSingleObject(handle, 0)
        if result == wait_timeout:
            return True
        if result == wait_object_0:
            return False
        # Be conservative on an unexpected API failure: a live lease will
        # expire naturally, whereas treating it as dead permits double work.
        return True
    finally:
        kernel32.CloseHandle(handle)


def _safe_task(task: CompletedTask) -> CompletedTask:
    """Bound and redact the evidence copy persisted outside the session log."""
    selected = task.evidence if len(task.evidence) <= 12 else [task.evidence[0], *task.evidence[-11:]]
    remaining = 24_000
    evidence: list[MemoryEvidence] = []
    for item in selected:
        if remaining <= 0:
            break
        text = redact_sensitive_text(item.text[:min(8_000, remaining)])
        remaining -= len(text)
        evidence.append(MemoryEvidence(
            id=item.id,
            text=text,
            source_kind=item.source_kind,
            timestamp=normalize_timestamp(item.timestamp) or item.timestamp,
        ))
    return CompletedTask(
        identity=task.identity,
        session_id=task.session_id,
        evidence=evidence,
        final_response=redact_sensitive_text(task.final_response[:6_000]),
        mode=task.mode,
        success=task.success,
        ended_at=task.ended_at,
    )


def _job_id(task: CompletedTask) -> str:
    payload = {
        "user_id": task.identity.user_id,
        "project_id": task.identity.project_id,
        "session_id": task.session_id,
        "mode": task.mode,
        "evidence_ids": [item.id for item in task.evidence],
        "ended_at": task.ended_at,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "job_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class FileMemoryJobQueue:
    """One atomic JSON file per job, protected by a per-user cross-process lock."""

    def __init__(
        self,
        root: Path,
        *,
        lock_timeout: float = 5.0,
        lease_seconds: float = 300.0,
        max_attempts: int = 3,
        retry_delay_seconds: float = 5.0,
        receipt_limit: int = 100,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.lock_timeout = lock_timeout
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.receipt_limit = receipt_limit

    async def enqueue(self, task: CompletedTask) -> PendingMemoryJob:
        return await asyncio.to_thread(self._enqueue, _safe_task(task))

    async def claim(
        self, identity: MemoryIdentity, *, owner_id: str,
    ) -> PendingMemoryJob | None:
        return await asyncio.to_thread(self._claim, identity, owner_id)

    async def checkpoint(
        self,
        job: PendingMemoryJob,
        *,
        owner_id: str,
        stage: MemoryJobStage,
        extracted_payload: list[dict[str, Any]] | None = None,
        consolidation_payload: list[dict[str, Any]] | None = None,
    ) -> PendingMemoryJob:
        return await asyncio.to_thread(
            self._checkpoint,
            job,
            owner_id,
            stage,
            extracted_payload,
            consolidation_payload,
        )

    async def complete(
        self,
        job: PendingMemoryJob,
        *,
        owner_id: str,
        result: dict[str, Any],
    ) -> None:
        await asyncio.to_thread(self._complete, job, owner_id, result)

    async def fail(
        self,
        job: PendingMemoryJob,
        *,
        owner_id: str,
        error: str,
        retryable: bool = True,
        reset_stage: MemoryJobStage | None = None,
    ) -> PendingMemoryJob:
        return await asyncio.to_thread(
            self._fail, job, owner_id, error, retryable, reset_stage,
        )

    async def release(self, job: PendingMemoryJob, *, owner_id: str) -> None:
        await asyncio.to_thread(self._release, job, owner_id)

    async def stats(self, identity: MemoryIdentity) -> MemoryJobCounts:
        return await asyncio.to_thread(self._stats, identity)

    async def ready_count(self, identity: MemoryIdentity) -> int:
        return await asyncio.to_thread(self._ready_count, identity)

    def _paths(self, identity: MemoryIdentity) -> tuple[Path, Path, Path]:
        user_dir = paths_for(identity, "global", root=self.root).directory
        return user_dir / "jobs", user_dir / "job-receipts", user_dir / ".jobs.lock"

    @contextmanager
    def _with_lock(self, identity: MemoryIdentity) -> Iterator[None]:
        _, _, lock_path = self._paths(identity)
        thread_lock = _thread_lock(lock_path)
        thread_lock.acquire()
        try:
            with _CrossProcessLock(lock_path, self.lock_timeout):
                yield
        finally:
            thread_lock.release()

    def _enqueue(self, task: CompletedTask) -> PendingMemoryJob:
        job = PendingMemoryJob(id=_job_id(task), task=task)
        jobs_dir, receipts_dir, _ = self._paths(task.identity)
        with self._with_lock(task.identity):
            path = jobs_dir / f"{job.id}.json"
            receipt = self._read_valid_receipt(
                receipts_dir / f"{job.id}.json",
                job_id=job.id,
                project_id=task.identity.project_id,
            )
            if receipt is not None:
                # A receipt is the durable completion marker.  Clean up a job
                # left behind by a crash between receipt write and unlink.
                path.unlink(missing_ok=True)
                job.status = "completed"
                job.stage = "apply"
                job.updated_at = str(receipt["completed_at"])
                return job
            if path.exists():
                try:
                    return self._read_job(path)
                except MemoryJobQueueError:
                    # The invalid copy has been quarantined; recreate the
                    # deterministic job from the completed task below.
                    pass
            jobs_dir.mkdir(parents=True, exist_ok=True)
            self._write_job(path, job)
        return job

    def _claim(self, identity: MemoryIdentity, owner_id: str) -> PendingMemoryJob | None:
        jobs_dir, receipts_dir, _ = self._paths(identity)
        with self._with_lock(identity):
            if not jobs_dir.exists():
                return None
            self._recover_legacy_quarantines(jobs_dir, receipts_dir)
            now = datetime.now(timezone.utc)
            jobs: list[tuple[Path, PendingMemoryJob]] = []
            for path in self._job_paths(jobs_dir):
                try:
                    job = self._read_job(path)
                except MemoryJobQueueError:
                    # One damaged job must not block the rest of the queue.
                    continue
                if job.task.identity != identity:
                    continue
                receipt = self._read_valid_receipt(
                    receipts_dir / f"{job.id}.json",
                    job_id=job.id,
                    project_id=identity.project_id,
                )
                if receipt is not None:
                    path.unlink(missing_ok=True)
                    continue
                if job.status == "processing" and self._recover_stale(job, now):
                    self._write_job(path, job)
                jobs.append((path, job))

            # Preserve per-identity ordering: a second worker may not claim a
            # later job while an earlier lease is still valid.
            if any(job.status == "processing" for _, job in jobs):
                return None

            ready: list[tuple[Path, PendingMemoryJob]] = []
            for path, job in jobs:
                if job.status != "pending":
                    continue
                next_attempt = _parse_timestamp(job.next_attempt_at)
                if next_attempt is not None and next_attempt > now:
                    continue
                ready.append((path, job))
            if not ready:
                return None

            path, job = min(ready, key=self._job_order_key)
            job.status = "processing"
            job.attempts += 1
            job.owner_id = owner_id
            job.owner_pid = os.getpid()
            job.lease_expires_at = _timestamp_after(self.lease_seconds)
            job.next_attempt_at = None
            job.updated_at = utc_now()
            self._write_job(path, job)
            return copy.deepcopy(job)
        return None

    def _checkpoint(
        self,
        job: PendingMemoryJob,
        owner_id: str,
        stage: MemoryJobStage,
        extracted_payload: list[dict[str, Any]] | None,
        consolidation_payload: list[dict[str, Any]] | None,
    ) -> PendingMemoryJob:
        jobs_dir, _, _ = self._paths(job.task.identity)
        path = jobs_dir / f"{job.id}.json"
        with self._with_lock(job.task.identity):
            current = self._owned_job(path, owner_id)
            current.stage = stage
            if extracted_payload is not None:
                current.extracted_payload = copy.deepcopy(extracted_payload)
            if consolidation_payload is not None:
                current.consolidation_payload = copy.deepcopy(consolidation_payload)
            current.lease_expires_at = _timestamp_after(self.lease_seconds)
            current.updated_at = utc_now()
            self._write_job(path, current)
            return copy.deepcopy(current)

    def _complete(
        self, job: PendingMemoryJob, owner_id: str, result: dict[str, Any],
    ) -> None:
        jobs_dir, receipts_dir, _ = self._paths(job.task.identity)
        path = jobs_dir / f"{job.id}.json"
        with self._with_lock(job.task.identity):
            current = self._owned_job(path, owner_id)
            receipts_dir.mkdir(parents=True, exist_ok=True)
            receipt = {
                "schema_version": 1,
                "job_id": current.id,
                "project_id": current.task.identity.project_id,
                "completed_at": utc_now(),
                "result": result,
            }
            self._write_json(receipts_dir / f"{current.id}.json", receipt)
            path.unlink(missing_ok=True)
            receipts = sorted(
                receipts_dir.glob("job_*.json"), key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for stale in receipts[self.receipt_limit:]:
                stale.unlink(missing_ok=True)

    def _fail(
        self,
        job: PendingMemoryJob,
        owner_id: str,
        error: str,
        retryable: bool,
        reset_stage: MemoryJobStage | None,
    ) -> PendingMemoryJob:
        jobs_dir, _, _ = self._paths(job.task.identity)
        path = jobs_dir / f"{job.id}.json"
        with self._with_lock(job.task.identity):
            current = self._owned_job(path, owner_id)
            current.last_error = redact_sensitive_text(error)[:1000]
            current.updated_at = utc_now()
            if reset_stage is not None:
                current.stage = reset_stage
                current.consolidation_payload = None
                if reset_stage == "extraction":
                    current.extracted_payload = None
            if retryable and current.attempts < self.max_attempts:
                self._reset_pending(current)
                current.next_attempt_at = _timestamp_after(
                    self.retry_delay_seconds * max(1, current.attempts),
                )
            else:
                current.status = "failed"
                current.owner_id = None
                current.owner_pid = None
                current.lease_expires_at = None
                current.next_attempt_at = None
            self._write_job(path, current)
            return copy.deepcopy(current)

    def _release(self, job: PendingMemoryJob, owner_id: str) -> None:
        jobs_dir, _, _ = self._paths(job.task.identity)
        path = jobs_dir / f"{job.id}.json"
        with self._with_lock(job.task.identity):
            if not path.exists():
                return
            current = self._read_job(path)
            if current.status != "processing" or current.owner_id != owner_id:
                return
            self._reset_pending(current)
            current.attempts = max(0, current.attempts - 1)
            current.updated_at = utc_now()
            self._write_job(path, current)

    def _stats(self, identity: MemoryIdentity) -> MemoryJobCounts:
        jobs_dir, receipts_dir, _ = self._paths(identity)
        pending = processing = failed = 0
        last_error: str | None = None
        last_error_at = ""
        with self._with_lock(identity):
            if not jobs_dir.exists():
                return MemoryJobCounts()
            self._recover_legacy_quarantines(jobs_dir, receipts_dir)
            now = datetime.now(timezone.utc)
            for path in self._job_paths(jobs_dir):
                try:
                    job = self._read_job(path)
                except MemoryJobQueueError:
                    # _read_job quarantines the bad file.  Status remains
                    # available and subsequent scans will not rediscover it.
                    continue
                if job.task.identity != identity:
                    continue
                if job.status == "processing" and self._recover_stale(job, now):
                    self._write_job(path, job)
                if job.status == "pending":
                    pending += 1
                elif job.status == "processing":
                    processing += 1
                elif job.status == "failed":
                    failed += 1
                if job.last_error and job.updated_at >= last_error_at:
                    last_error = job.last_error
                    last_error_at = job.updated_at
        return MemoryJobCounts(
            pending=pending, processing=processing, failed=failed, last_error=last_error,
        )

    def _ready_count(self, identity: MemoryIdentity) -> int:
        _, receipts_dir, _ = self._paths(identity)
        if not receipts_dir.exists():
            return 0
        count = 0
        with self._with_lock(identity):
            for path in receipts_dir.glob("job_*.json"):
                if self._read_valid_receipt(
                    path, job_id=path.stem, project_id=identity.project_id,
                ) is not None:
                    count += 1
        return count

    @staticmethod
    def _reset_pending(job: PendingMemoryJob) -> None:
        job.status = "pending"
        job.owner_id = None
        job.owner_pid = None
        job.lease_expires_at = None

    def _recover_stale(self, job: PendingMemoryJob, now: datetime) -> bool:
        if not self._lease_is_stale(job, now):
            return False
        if job.attempts >= self.max_attempts:
            job.status = "failed"
            job.owner_id = None
            job.owner_pid = None
            job.lease_expires_at = None
            job.next_attempt_at = None
            job.last_error = "memory job lease expired after maximum attempts"
        else:
            self._reset_pending(job)
        job.updated_at = utc_now()
        return True

    @staticmethod
    def _lease_is_stale(job: PendingMemoryJob, now: datetime) -> bool:
        expires = _parse_timestamp(job.lease_expires_at)
        return (
            not _pid_alive(job.owner_pid)
            or expires is None
            or expires <= now
        )

    @staticmethod
    def _job_order_key(
        item: tuple[Path, PendingMemoryJob],
    ) -> tuple[datetime, datetime, str]:
        _, job = item
        latest = datetime.max.replace(tzinfo=timezone.utc)
        return (
            _parse_timestamp(job.task.ended_at) or latest,
            _parse_timestamp(job.created_at) or latest,
            job.id,
        )

    @staticmethod
    def _read_valid_receipt(
        path: Path,
        *,
        job_id: str,
        project_id: str | None,
    ) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("job_id") != job_id
            or payload.get("project_id") != project_id
            or _parse_timestamp(payload.get("completed_at")) is None
        ):
            return None
        return payload

    def _owned_job(self, path: Path, owner_id: str) -> PendingMemoryJob:
        if not path.exists():
            raise MemoryJobQueueError(f"memory job no longer exists: {path.name}")
        job = self._read_job(path)
        if job.status != "processing" or job.owner_id != owner_id:
            raise MemoryJobQueueError(f"memory job lease was lost: {job.id}")
        return job

    @staticmethod
    def _job_paths(jobs_dir: Path) -> list[Path]:
        """Return only canonical live jobs, never quarantine artifacts."""
        if not jobs_dir.exists():
            return []
        return sorted(
            path for path in jobs_dir.iterdir()
            if path.is_file() and _JOB_FILE_RE.fullmatch(path.name)
        )

    def _recover_legacy_quarantines(
        self, jobs_dir: Path, receipts_dir: Path,
    ) -> None:
        """Restore jobs rejected only because an older reader was too strict."""
        for path in sorted(jobs_dir.glob("job_*.corrupt-*.json")):
            try:
                job = self._read_job(path)
            except MemoryJobQueueError:
                continue
            if not path.name.startswith(f"{job.id}."):
                self._quarantine_job_file(path)
                continue
            canonical = jobs_dir / f"{job.id}.json"
            receipt = self._read_valid_receipt(
                receipts_dir / f"{job.id}.json",
                job_id=job.id,
                project_id=job.task.identity.project_id,
            )
            if receipt is not None:
                self._quarantine_job_file(path)
                continue
            if canonical.exists():
                try:
                    self._read_job(canonical)
                except MemoryJobQueueError:
                    pass
            if canonical.exists():
                self._quarantine_job_file(path)
                continue
            try:
                os.replace(path, canonical)
            except OSError:
                pass

    def _read_job(self, path: Path) -> PendingMemoryJob:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                raise ValueError("unsupported job schema")
            raw_job = payload.get("job")
            if not isinstance(raw_job, dict):
                raise ValueError("job payload is missing")
            return PendingMemoryJob.from_dict(raw_job)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._quarantine_job_file(path)
            raise MemoryJobQueueError(f"invalid memory job file: {path}") from exc

    @staticmethod
    def _quarantine_job_file(path: Path) -> Path | None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        quarantine_dir = path.parent / "corrupt"
        backup = quarantine_dir / (
            f"{path.stem}.corrupt-{stamp}-{os.getpid()}-"
            f"{threading.get_ident()}{path.suffix}"
        )
        try:
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(path, backup)
        except OSError:
            return None
        return backup

    def _write_job(self, path: Path, job: PendingMemoryJob) -> None:
        self._write_json(path, {"schema_version": 1, "job": job.to_dict()})

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            try:
                path.parent.chmod(0o700)
            except OSError:
                pass
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        serialized = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ) + "\n"
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            try:
                path.chmod(0o600)
            except OSError:
                pass


__all__ = ["FileMemoryJobQueue", "MemoryJobQueueError"]
