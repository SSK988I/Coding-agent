from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys

from coding_agent.memory.jobs import FileMemoryJobQueue, _pid_alive
from coding_agent.memory.types import CompletedTask, MemoryEvidence, MemoryIdentity


def _run(value):
    return asyncio.run(value)


def _task(
    *,
    session_id: str = "session-1",
    ended_at: str = "2026-08-24T10:01:00Z",
) -> CompletedTask:
    return CompletedTask(
        identity=MemoryIdentity("local-user", "project-a"),
        session_id=session_id,
        evidence=[MemoryEvidence(
            id="entry-1",
            text="以后回答使用中文",
            source_kind="user",
            timestamp="2026-08-24T10:00:00Z",
        )],
        final_response="好的",
        ended_at=ended_at,
    )


def test_pid_alive_probe_does_not_terminate_live_process() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _pid_alive(process.pid)
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)

    assert not _pid_alive(process.pid)


def test_enqueued_job_is_claimed_by_a_new_queue_instance(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    task = _task()
    queued = _run(FileMemoryJobQueue(root).enqueue(task))

    # A fresh queue represents another CLI process or a restarted service.
    claimed = _run(FileMemoryJobQueue(root).claim(task.identity, owner_id="worker-2"))

    assert claimed is not None
    assert claimed.id == queued.id
    assert claimed.task == task
    assert claimed.status == "processing"
    assert claimed.owner_id == "worker-2"
    assert claimed.attempts == 1


def test_enqueue_is_idempotent_for_the_same_completed_task(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    queue = FileMemoryJobQueue(root)
    task = _task()

    first = _run(queue.enqueue(task))
    second = _run(queue.enqueue(task))

    assert second.id == first.id
    assert _run(queue.stats(task.identity)).pending == 1
    assert len(list(root.rglob("job_*.json"))) == 1


def test_legacy_session_timestamp_is_normalized_when_job_is_read(
    tmp_path: Path,
) -> None:
    root = tmp_path / "memory"
    queue = FileMemoryJobQueue(root)
    task = _task()
    queued = _run(queue.enqueue(task))
    path = next(root.rglob(f"{queued.id}.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["job"]["task"]["evidence"][0]["timestamp"] = (
        "2026-08-24T10-00-00-123Z"
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    claimed = _run(queue.claim(task.identity, owner_id="worker-1"))

    assert claimed is not None
    assert claimed.task.evidence[0].timestamp == "2026-08-24T10:00:00.123Z"


def test_invalid_job_is_quarantined_once_and_stats_remains_available(
    tmp_path: Path,
) -> None:
    root = tmp_path / "memory"
    queue = FileMemoryJobQueue(root)
    task = _task()
    queued = _run(queue.enqueue(task))
    path = next(root.rglob(f"{queued.id}.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["job"]["task"]["evidence"][0]["timestamp"] = "not-a-timestamp"
    path.write_text(json.dumps(payload), encoding="utf-8")

    first = _run(queue.stats(task.identity))
    quarantined = list((path.parent / "corrupt").glob("*.json"))
    second = _run(queue.stats(task.identity))

    assert first.pending == second.pending == 0
    assert not path.exists()
    assert len(quarantined) == 1
    assert list((path.parent / "corrupt").glob("*.json")) == quarantined
    assert ".corrupt-" in quarantined[0].name
    assert quarantined[0].name.count(".corrupt-") == 1


def test_claim_skips_invalid_job_and_claims_a_valid_job(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    queue = FileMemoryJobQueue(root)
    invalid_task = _task(
        session_id="invalid-session", ended_at="2026-08-24T10:01:00Z",
    )
    valid_task = _task(
        session_id="valid-session", ended_at="2026-08-24T11:01:00Z",
    )
    invalid = _run(queue.enqueue(invalid_task))
    _run(queue.enqueue(valid_task))
    invalid_path = next(root.rglob(f"{invalid.id}.json"))
    payload = json.loads(invalid_path.read_text(encoding="utf-8"))
    payload["job"]["task"]["evidence"][0]["timestamp"] = "invalid"
    invalid_path.write_text(json.dumps(payload), encoding="utf-8")

    counts = _run(queue.stats(valid_task.identity))
    claimed = _run(queue.claim(valid_task.identity, owner_id="worker-1"))

    assert counts.pending == 1
    assert claimed is not None
    assert claimed.task.session_id == "valid-session"


def test_parseable_legacy_quarantine_is_recovered_once(
    tmp_path: Path,
) -> None:
    root = tmp_path / "memory"
    queue = FileMemoryJobQueue(root)
    task = _task()
    queued = _run(queue.enqueue(task))
    path = next(root.rglob(f"{queued.id}.json"))
    legacy_quarantine = path.with_name(f"{path.stem}.corrupt-old.json")
    path.rename(legacy_quarantine)

    assert _run(queue.stats(task.identity)).pending == 1
    assert path.exists()
    assert not legacy_quarantine.exists()
    claimed = _run(queue.claim(task.identity, owner_id="worker-1"))
    assert claimed is not None
    assert claimed.id == queued.id


def test_completion_receipt_prevents_reenqueue(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    queue = FileMemoryJobQueue(root)
    task = _task()
    original = _run(queue.enqueue(task))
    claimed = _run(queue.claim(task.identity, owner_id="worker-1"))
    assert claimed is not None
    _run(queue.complete(claimed, owner_id="worker-1", result={"accepted": 1}))

    duplicate = _run(FileMemoryJobQueue(root).enqueue(task))

    assert duplicate.id == original.id
    assert duplicate.status == "completed"
    assert _run(queue.stats(task.identity)).pending == 0
    assert _run(queue.ready_count(task.identity)) == 1
    assert _run(queue.claim(task.identity, owner_id="worker-2")) is None


def test_checkpoint_survives_release_and_reclaim(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    task = _task()
    first_queue = FileMemoryJobQueue(root)
    _run(first_queue.enqueue(task))
    claimed = _run(first_queue.claim(task.identity, owner_id="worker-1"))
    assert claimed is not None

    extracted = [{
        "kind": "preference",
        "scope": "global",
        "content": "回答使用中文",
        "value": "zh-CN",
        "confidence": 1.0,
        "source_kind": "explicit_user",
        "evidence_entry_ids": ["entry-1"],
        "source_timestamp": "2026-08-24T10:00:00Z",
        "relation_key": "response.language",
    }]
    checkpointed = _run(first_queue.checkpoint(
        claimed,
        owner_id="worker-1",
        stage="consolidation",
        extracted_payload=extracted,
    ))
    _run(first_queue.release(checkpointed, owner_id="worker-1"))

    resumed = _run(FileMemoryJobQueue(root).claim(
        task.identity, owner_id="worker-2",
    ))

    assert resumed is not None
    assert resumed.stage == "consolidation"
    assert resumed.extracted_payload == extracted
    # A graceful release is not counted as a failed processing attempt.
    assert resumed.attempts == 1


def test_retryable_failure_requeues_then_becomes_terminal(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    task = _task()
    queue = FileMemoryJobQueue(
        root, max_attempts=2, retry_delay_seconds=0,
    )
    _run(queue.enqueue(task))

    first = _run(queue.claim(task.identity, owner_id="worker-1"))
    assert first is not None
    retry = _run(queue.fail(
        first, owner_id="worker-1", error="extract failed", retryable=True,
    ))
    assert retry.status == "pending"
    assert retry.last_error == "extract failed"

    second = _run(FileMemoryJobQueue(
        root, max_attempts=2, retry_delay_seconds=0,
    ).claim(task.identity, owner_id="worker-2"))
    assert second is not None
    assert second.attempts == 2
    failed = _run(queue.fail(
        second, owner_id="worker-2", error="still broken", retryable=True,
    ))

    assert failed.status == "failed"
    counts = _run(queue.stats(task.identity))
    assert counts.pending == 0
    assert counts.failed == 1
    assert counts.last_error == "still broken"


def test_expired_lease_is_recovered_by_another_worker(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    task = _task()
    first_queue = FileMemoryJobQueue(root, lease_seconds=0)
    _run(first_queue.enqueue(task))
    first = _run(first_queue.claim(task.identity, owner_id="worker-1"))
    assert first is not None

    recovered = _run(FileMemoryJobQueue(root, lease_seconds=60).claim(
        task.identity, owner_id="worker-2",
    ))

    assert recovered is not None
    assert recovered.id == first.id
    assert recovered.owner_id == "worker-2"
    assert recovered.attempts == 2


def test_live_lease_is_not_stolen_by_another_owner_in_same_process(
    tmp_path: Path,
) -> None:
    root = tmp_path / "memory"
    task = _task()
    queue = FileMemoryJobQueue(root, lease_seconds=60)
    _run(queue.enqueue(task))
    claimed = _run(queue.claim(task.identity, owner_id="worker-1"))
    assert claimed is not None

    assert _run(queue.claim(task.identity, owner_id="worker-2")) is None


def test_expired_lease_respects_max_attempts(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    task = _task()
    queue = FileMemoryJobQueue(root, lease_seconds=0, max_attempts=1)
    _run(queue.enqueue(task))
    claimed = _run(queue.claim(task.identity, owner_id="worker-1"))
    assert claimed is not None

    assert _run(FileMemoryJobQueue(
        root, lease_seconds=60, max_attempts=1,
    ).claim(task.identity, owner_id="worker-2")) is None
    counts = _run(queue.stats(task.identity))
    assert counts.pending == 0
    assert counts.processing == 0
    assert counts.failed == 1
    assert counts.last_error == "memory job lease expired after maximum attempts"


def test_jobs_are_claimed_in_task_order_and_only_one_is_processing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "memory"
    queue = FileMemoryJobQueue(root, lease_seconds=60)
    later = _task(
        session_id="session-later", ended_at="2026-08-24T12:00:00Z",
    )
    earlier = _task(
        session_id="session-earlier", ended_at="2026-08-24T11:00:00Z",
    )
    _run(queue.enqueue(later))
    _run(queue.enqueue(earlier))

    first = _run(queue.claim(earlier.identity, owner_id="worker-1"))
    assert first is not None
    assert first.task.session_id == "session-earlier"
    assert _run(queue.claim(earlier.identity, owner_id="worker-2")) is None

    _run(queue.complete(first, owner_id="worker-1", result={}))
    second = _run(queue.claim(later.identity, owner_id="worker-2"))
    assert second is not None
    assert second.task.session_id == "session-later"


def test_enqueued_evidence_and_response_are_redacted_on_disk(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    task = _task()
    task.evidence[0].text = (
        "token sk-abcdefghijklmnop and password=hunter12345 "
        "Authorization: Bearer abcdefghijklmnop\n"
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEFAKEPRIVATEKEYMATERIAL1234567890\n"
        "-----END PRIVATE KEY-----"
    )
    task.final_response = "secret=topsecretvalue"

    queued = _run(FileMemoryJobQueue(root).enqueue(task))
    persisted = "\n".join(
        path.read_text(encoding="utf-8") for path in root.rglob("job_*.json")
    )

    assert "[REDACTED]" in queued.task.evidence[0].text
    assert "[REDACTED]" in queued.task.final_response
    for secret in (
        "sk-abcdefghijklmnop",
        "hunter12345",
        "abcdefghijklmnop",
        "topsecretvalue",
        "MIIEFAKEPRIVATEKEYMATERIAL1234567890",
    ):
        assert secret not in persisted


def test_failure_error_is_redacted_before_persistence(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    task = _task()
    queue = FileMemoryJobQueue(root)
    _run(queue.enqueue(task))
    claimed = _run(queue.claim(task.identity, owner_id="worker-1"))
    assert claimed is not None

    failed = _run(queue.fail(
        claimed,
        owner_id="worker-1",
        error="provider rejected Authorization: Bearer abcdefghijklmnop",
        retryable=False,
    ))
    persisted = "\n".join(
        path.read_text(encoding="utf-8") for path in root.rglob("job_*.json")
    )

    assert failed.last_error == "provider rejected [REDACTED]"
    assert _run(queue.stats(task.identity)).last_error == failed.last_error
    assert "abcdefghijklmnop" not in persisted
