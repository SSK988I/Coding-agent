"""JSONL file I/O for sessions.

Reads and writes the append-only JSONL session format:

    <agent_dir>/sessions/--<encoded-cwd>--/<ISO-ts-with-dashes>_<sessionId>.jsonl

Line 1 is a SessionHeader; each subsequent line is one SessionEntry dict.

Persistence semantics:
  - "flush on first assistant": before the first assistant message, entries
    are buffered in memory; the file is created (exclusive) only when the
    first assistant message is appended. After that, every entry is appended.

This module is intentionally synchronous because writes are small, ordered,
and occur on the session event path.
"""
from __future__ import annotations

import json
import os
import tempfile
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_core.session.serde import dict_to_message, message_to_dict
from agent_core.session.types import (
    CompactionDetails,
    CompactionEntry,
    CollaborationModeChangeEntry,
    LeafEntry,
    ModelChangeEntry,
    PlanQuestionAnswerEntry,
    PlanQuestionEntry,
    PlanRevisionEntry,
    PlanRunEntry,
    SessionEntry,
    SessionHeader,
    SessionInfo,
    SessionInfoEntry,
    SessionMessageEntry,
    ThinkingLevelChangeEntry,
)

__all__ = [
    "default_agent_dir",
    "session_dir_for_cwd",
    "session_dirs_for_cwd",
    "session_file_path",
    "filename_for_session",
    "write_header_line",
    "rewrite_header_line",
    "append_entry_line",
    "read_header",
    "read_entries",
    "list_session_files",
    "entry_to_line_dict",
    "line_dict_to_entry",
    "build_session_info",
    "compute_leaf_id",
    "iso_now",
]


# ─── paths ─────────────────────────────────────────────────────────────

def default_agent_dir() -> Path:
    """Return the default agent directory: ``~/.coding-agent``."""
    return Path.home() / ".coding-agent"


def session_dir_for_cwd(
    cwd: str,
    agent_dir: Path | None = None,
    *,
    sessions_dir: Path | None = None,
) -> Path:
    """The per-cwd sessions subdirectory: <agent_dir>/sessions/--<encoded-cwd>--."""
    from agent_core.session.ids import encode_cwd
    if agent_dir is not None and sessions_dir is not None:
        raise ValueError("agent_dir and sessions_dir are mutually exclusive")
    if sessions_dir is None:
        base = agent_dir if agent_dir is not None else default_agent_dir()
        sessions_dir = base / "sessions"
    return sessions_dir / encode_cwd(cwd)


def session_dirs_for_cwd(
    cwd: str,
    agent_dir: Path | None = None,
    *,
    sessions_dir: Path | None = None,
) -> list[Path]:
    """Return current and legacy per-cwd directories, current first."""
    from agent_core.session.ids import encode_cwd_legacy

    current = session_dir_for_cwd(
        cwd, agent_dir, sessions_dir=sessions_dir,
    )
    base = current.parent
    legacy = base / encode_cwd_legacy(cwd)
    return [current] if legacy == current else [current, legacy]


def filename_for_session(header: SessionHeader) -> str:
    """Build the JSONL filename: <ISO-ts-with-dashes>_<sessionId>.jsonl."""
    safe_ts = header.timestamp.replace(":", "-").replace(".", "-")
    return f"{safe_ts}_{header.id}.jsonl"


def session_file_path(
    header: SessionHeader,
    cwd: str,
    agent_dir: Path | None = None,
    *,
    sessions_dir: Path | None = None,
) -> Path:
    return session_dir_for_cwd(
        cwd, agent_dir, sessions_dir=sessions_dir,
    ) / filename_for_session(header)


# ─── time ──────────────────────────────────────────────────────────────

def iso_now() -> str:
    """Return the current UTC time as ISO 8601 with millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z",
    )


# ─── serialization: SessionEntry <-> line dict ────────────────────────

def entry_to_line_dict(entry: SessionEntry) -> dict:
    """Convert a SessionEntry dataclass to a JSON-safe dict for one JSONL line.

    Field names use camelCase to match the on-disk format (type/id/parentId/
    timestamp/message/...), so files are interchangeable.
    """
    if isinstance(entry, SessionMessageEntry):
        return {
            "type": "message",
            "id": entry.id,
            "parentId": entry.parent_id,
            "timestamp": entry.timestamp,
            "message": message_to_dict(entry.message) if entry.message is not None else None,
            "thinkingLevel": entry.thinking_level,
        }
    if isinstance(entry, CompactionEntry):
        d = {
            "type": "compaction",
            "id": entry.id,
            "parentId": entry.parent_id,
            "timestamp": entry.timestamp,
            "summary": entry.summary,
            "firstKeptEntryId": entry.first_kept_entry_id,
            "tokensBefore": entry.tokens_before,
            "details": None,
            "fromHook": entry.from_hook,
        }
        if entry.details is not None:
            d["details"] = {
                "readFiles": list(entry.details.read_files),
                "modifiedFiles": list(entry.details.modified_files),
            }
        return d
    if isinstance(entry, ModelChangeEntry):
        return {
            "type": "model_change",
            "id": entry.id, "parentId": entry.parent_id, "timestamp": entry.timestamp,
            "provider": entry.provider, "modelId": entry.model_id,
        }
    if isinstance(entry, ThinkingLevelChangeEntry):
        return {
            "type": "thinking_level_change",
            "id": entry.id, "parentId": entry.parent_id, "timestamp": entry.timestamp,
            "thinkingLevel": entry.thinking_level,
        }
    if isinstance(entry, SessionInfoEntry):
        return {
            "type": "session_info",
            "id": entry.id, "parentId": entry.parent_id, "timestamp": entry.timestamp,
            "name": entry.name,
        }
    if isinstance(entry, LeafEntry):
        return {
            "type": "leaf",
            "id": entry.id, "parentId": entry.parent_id, "timestamp": entry.timestamp,
            "targetId": entry.target_id,
        }
    if isinstance(entry, CollaborationModeChangeEntry):
        return {
            "type": "collaboration_mode_change",
            "id": entry.id, "parentId": entry.parent_id, "timestamp": entry.timestamp,
            "mode": entry.mode, "planId": entry.plan_id,
            "reason": entry.reason, "relatedSessionId": entry.related_session_id,
        }
    if isinstance(entry, PlanQuestionEntry):
        return {
            "type": "plan_question",
            "id": entry.id, "parentId": entry.parent_id, "timestamp": entry.timestamp,
            "planId": entry.plan_id, "questionId": entry.question_id,
            "header": entry.header, "question": entry.question,
            "options": list(entry.options), "allowCustom": entry.allow_custom,
        }
    if isinstance(entry, PlanQuestionAnswerEntry):
        return {
            "type": "plan_question_answer",
            "id": entry.id, "parentId": entry.parent_id, "timestamp": entry.timestamp,
            "planId": entry.plan_id, "questionId": entry.question_id,
            "answer": entry.answer,
        }
    if isinstance(entry, PlanRevisionEntry):
        return {
            "type": "plan_revision",
            "id": entry.id, "parentId": entry.parent_id, "timestamp": entry.timestamp,
            "planId": entry.plan_id, "revision": entry.revision,
            "title": entry.title, "markdown": entry.markdown,
            "digest": entry.digest, "sourceMessageId": entry.source_message_id,
            "schemaVersion": entry.schema_version,
            "submittedByToolCallId": entry.submitted_by_tool_call_id,
            "originSessionId": entry.origin_session_id,
        }
    if isinstance(entry, PlanRunEntry):
        return {
            "type": "plan_run",
            "id": entry.id, "parentId": entry.parent_id, "timestamp": entry.timestamp,
            "planId": entry.plan_id, "revision": entry.revision,
            "digest": entry.digest, "status": entry.status,
            "runId": entry.run_id, "error": entry.error,
            "assistantMessageId": entry.assistant_message_id,
            "stopReason": entry.stop_reason,
        }
    raise TypeError(f"Cannot serialize entry of type {type(entry)!r}")


def line_dict_to_entry(d: dict) -> SessionEntry:
    """Convert a JSONL line dict back to a SessionEntry dataclass."""
    etype = d.get("type")
    entry_id = d.get("id", "")
    timestamp = d.get("timestamp", "")
    parent_id = d.get("parentId")
    if not isinstance(entry_id, str) or not isinstance(timestamp, str):
        raise ValueError("Session entry id and timestamp must be strings")
    if parent_id is not None and not isinstance(parent_id, str):
        raise ValueError("Session entry parentId must be a string or null")
    if etype in {"collaboration_mode_change", "plan_question", "plan_question_answer", "plan_revision", "plan_run"}:
        # Accept absent legacy fields, never coerce tampered values (e.g.
        # true or "1" into revision 1) into an apparently valid signature.
        for field in ("planId", "questionId", "header", "question", "answer", "title", "markdown",
                      "digest", "sourceMessageId", "submittedByToolCallId"):
            if field in d and not isinstance(d[field], str):
                if field != "planId" or etype != "collaboration_mode_change" or d[field] is not None:
                    raise ValueError(f"{etype}.{field} must be a string")
        for field in ("revision", "schemaVersion"):
            if field in d and type(d[field]) is not int:
                raise ValueError(f"{etype}.{field} must be an integer")
        for field in ("runId", "error", "assistantMessageId", "stopReason", "originSessionId", "reason", "relatedSessionId"):
            if d.get(field) is not None and not isinstance(d[field], str):
                raise ValueError(f"{etype}.{field} must be a string or null")
        if "allowCustom" in d and not isinstance(d["allowCustom"], bool):
            raise ValueError("plan_question.allowCustom must be a boolean")
    if etype == "message":
        msg = d.get("message")
        return SessionMessageEntry(
            message=dict_to_message(msg) if msg is not None else None,
            thinking_level=d.get("thinkingLevel"),
            id=entry_id, parent_id=parent_id, timestamp=timestamp,
        )
    if etype == "compaction":
        details_raw = d.get("details")
        details = None
        if isinstance(details_raw, dict):
            details = CompactionDetails(
                read_files=list(details_raw.get("readFiles") or []),
                modified_files=list(details_raw.get("modifiedFiles") or []),
            )
        return CompactionEntry(
            summary=d.get("summary", ""),
            first_kept_entry_id=d.get("firstKeptEntryId", ""),
            tokens_before=int(d.get("tokensBefore", 0) or 0),
            details=details,
            from_hook=bool(d.get("fromHook", False)),
            id=entry_id, parent_id=parent_id, timestamp=timestamp,
        )
    if etype == "model_change":
        return ModelChangeEntry(
            provider=d.get("provider", ""), model_id=d.get("modelId", ""),
            id=entry_id, parent_id=parent_id, timestamp=timestamp,
        )
    if etype == "thinking_level_change":
        return ThinkingLevelChangeEntry(
            thinking_level=d.get("thinkingLevel", ""),
            id=entry_id, parent_id=parent_id, timestamp=timestamp,
        )
    if etype == "session_info":
        return SessionInfoEntry(
            name=d.get("name", ""), id=entry_id,
            parent_id=parent_id, timestamp=timestamp,
        )
    if etype == "leaf":
        return LeafEntry(
            target_id=d.get("targetId"), id=entry_id,
            parent_id=parent_id, timestamp=timestamp,
        )
    if etype == "collaboration_mode_change":
        mode = d.get("mode", "default")
        if mode not in {"default", "plan"}:
            raise ValueError(f"Invalid collaboration mode: {mode!r}")
        return CollaborationModeChangeEntry(
            mode=mode, plan_id=d.get("planId"), id=entry_id,
            reason=d.get("reason"), related_session_id=d.get("relatedSessionId"),
            parent_id=parent_id, timestamp=timestamp,
        )
    if etype == "plan_question":
        options = d.get("options") or []
        if not isinstance(options, list):
            raise ValueError("plan_question.options must be a list")
        if any(not isinstance(item, dict) or not all(
            isinstance(item.get(key), str) for key in ("label", "description")
        ) for item in options):
            raise ValueError("plan_question.options must contain string labels and descriptions")
        return PlanQuestionEntry(
            plan_id=str(d.get("planId", "")),
            question_id=str(d.get("questionId", "")),
            header=str(d.get("header", "")), question=str(d.get("question", "")),
            options=[dict(item) for item in options if isinstance(item, dict)],
            allow_custom=bool(d.get("allowCustom", True)),
            id=entry_id, parent_id=parent_id, timestamp=timestamp,
        )
    if etype == "plan_question_answer":
        return PlanQuestionAnswerEntry(
            plan_id=str(d.get("planId", "")),
            question_id=str(d.get("questionId", "")),
            answer=str(d.get("answer", "")), id=entry_id,
            parent_id=parent_id, timestamp=timestamp,
        )
    if etype == "plan_revision":
        return PlanRevisionEntry(
            plan_id=str(d.get("planId", "")), revision=int(d.get("revision", 0) or 0),
            title=str(d.get("title", "")), markdown=str(d.get("markdown", "")),
            digest=str(d.get("digest", "")),
            source_message_id=str(d.get("sourceMessageId", "")), id=entry_id,
            schema_version=int(d.get("schemaVersion", 0) or 0),
            submitted_by_tool_call_id=str(d.get("submittedByToolCallId", "")),
            origin_session_id=d.get("originSessionId"),
            parent_id=parent_id, timestamp=timestamp,
        )
    if etype == "plan_run":
        status = d.get("status", "started")
        if status not in {"started", "completed", "failed", "aborted"}:
            raise ValueError(f"Invalid plan run status: {status!r}")
        return PlanRunEntry(
            plan_id=str(d.get("planId", "")), revision=int(d.get("revision", 0) or 0),
            digest=str(d.get("digest", "")), status=status,
            run_id=d.get("runId"), error=d.get("error"), id=entry_id,
            assistant_message_id=d.get("assistantMessageId"),
            stop_reason=d.get("stopReason"),
            parent_id=parent_id, timestamp=timestamp,
        )
    raise ValueError(f"Unknown entry type in session file: {etype!r}")


def header_to_dict(header: SessionHeader) -> dict:
    return {
        "type": "session",
        "version": header.version,
        "id": header.id,
        "timestamp": header.timestamp,
        "cwd": header.cwd,
        "parentSession": header.parent_session,
    }


def dict_to_header(d: dict) -> SessionHeader:
    return SessionHeader(
        id=d.get("id", ""),
        timestamp=d.get("timestamp", ""),
        cwd=d.get("cwd", ""),
        version=int(d.get("version", 1) or 1),
        parent_session=d.get("parentSession"),
    )


# ─── file write ───────────────────────────────────────────────────────

def write_header_line(path: Path, header: SessionHeader) -> None:
    """Create the file with the header line (exclusive create, flush-on-first)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # 'x' mode = exclusive create (fails if exists), matching the 'wx' flag.
    with open(path, "x", encoding="utf-8") as f:
        f.write(json.dumps(header_to_dict(header), ensure_ascii=False))
        f.write("\n")


def rewrite_header_line(path: Path, header: SessionHeader) -> None:
    """Atomically replace only the JSONL header while preserving entry bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "rb") as source:
        source.readline()
        remainder = source.read()
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(json.dumps(header_to_dict(header), ensure_ascii=False).encode("utf-8"))
            target.write(b"\n")
            target.write(remainder)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def append_entry_line(path: Path, entry: SessionEntry) -> None:
    """Append one entry as a JSON line (append-only)."""
    encoded = (json.dumps(entry_to_line_dict(entry), ensure_ascii=False) + "\n").encode("utf-8")
    durable = isinstance(entry, (
        CollaborationModeChangeEntry, PlanQuestionEntry, PlanQuestionAnswerEntry,
        PlanRevisionEntry, PlanRunEntry,
    ))
    with open(path, "ab", buffering=0) as stream:
        offset = stream.tell()
        try:
            written = stream.write(encoded)
            if written != len(encoded):
                raise OSError("Incomplete session entry write")
            if durable:
                os.fsync(stream.fileno())
        except BaseException:
            # Undo only this attempted JSONL append. Never rewrite earlier
            # session history; a process crash remains detectable on replay.
            stream.truncate(offset)
            raise


# ─── file read ────────────────────────────────────────────────────────

def read_header(path: Path) -> SessionHeader | None:
    """Read the header (first line). Returns None if file missing/corrupt."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            line = f.readline()
        if not line:
            return None
        return dict_to_header(json.loads(line))
    except (OSError, ValueError):
        return None


def read_entries(
    path: Path,
    *,
    issues: list[dict[str, Any]] | None = None,
) -> list[SessionEntry]:
    """Read all valid entries and warn when damaged JSONL lines are skipped."""
    entries: list[SessionEntry] = []
    corrupt_lines: list[int] = []
    seen_ids: set[str] = set()

    def record_issue(line_number: int, code: str, message: str) -> None:
        corrupt_lines.append(line_number)
        if issues is not None:
            issues.append({
                "line": line_number,
                "code": code,
                "message": message,
            })

    try:
        # Decode line-by-line so one invalid UTF-8 tail cannot hide every
        # otherwise valid entry that precedes or follows it.
        with open(path, "rb") as f:
            first = True
            for line_number, raw_line in enumerate(f, start=1):
                if first:  # skip header
                    first = False
                    continue
                try:
                    line = raw_line.decode("utf-8").strip()
                except UnicodeDecodeError as exc:
                    record_issue(line_number, "INVALID_UTF8", str(exc))
                    continue
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                    if not isinstance(payload, dict):
                        raise ValueError("session entry must be a JSON object")
                    entry = line_dict_to_entry(payload)
                    if not entry.id:
                        raise ValueError("session entry id must not be empty")
                    if entry.id in seen_ids:
                        raise ValueError(f"duplicate session entry id: {entry.id}")
                    if entry.parent_id == entry.id:
                        raise ValueError("session entry cannot parent itself")
                    if entry.parent_id is not None and entry.parent_id not in seen_ids:
                        record_issue(
                            line_number,
                            "DANGLING_PARENT",
                            f"unknown parentId: {entry.parent_id}",
                        )
                    if (
                        isinstance(entry, LeafEntry)
                        and entry.target_id is not None
                        and entry.target_id not in seen_ids
                    ):
                        record_issue(
                            line_number,
                            "INVALID_LEAF_TARGET",
                            f"unknown leaf targetId: {entry.target_id}",
                        )
                    setattr(entry, "_source_line", line_number)
                    entries.append(entry)
                    seen_ids.add(entry.id)
                except (AttributeError, KeyError, TypeError, ValueError) as exc:
                    # Skip corrupt/trailing entries but keep the rest and make
                    # the damage available to the Plan recovery reducer.
                    record_issue(line_number, "CORRUPT_SESSION_ENTRY", str(exc))
                    continue
    except OSError:
        pass
    if corrupt_lines:
        preview = ", ".join(str(number) for number in corrupt_lines[:5])
        extra = "..." if len(corrupt_lines) > 5 else ""
        warnings.warn(
            f"Skipped {len(corrupt_lines)} corrupt session line(s) in {path}: {preview}{extra}",
            RuntimeWarning,
            stacklevel=2,
        )
    return entries


def compute_leaf_id(entries: list[SessionEntry]) -> str | None:
    """根据 entries 列表算出持久化的叶指针。

    扫一遍所有 entries，维护一个游标：
      - 遇到 LeafEntry → 游标 = entry.target_id（可能为 None）
      - 遇到其他 entry → 游标 = entry.id

    最后游标的值即"当前活跃叶"。没有 LeafEntry 的旧文件自然退化为
    "最后一条 entry 的 id"，行为完全向后兼容。

    返回 None 表示空会话，或最近一条 LeafEntry 把叶指针显式重置到根。
    """
    leaf: str | None = None
    for e in entries:
        if isinstance(e, LeafEntry):
            leaf = e.target_id
        else:
            leaf = e.id
    return leaf


def list_session_files(sessions_dir: Path) -> list[Path]:
    """List *.jsonl files in a sessions dir, sorted by mtime descending."""
    if not sessions_dir.is_dir():
        return []
    files = [p for p in sessions_dir.iterdir() if p.suffix == ".jsonl"]
    try:
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        pass
    return files


def build_session_info(path: Path) -> SessionInfo | None:
    """Build a SessionInfo summary by scanning a JSONL file (for /sessions)."""
    header = read_header(path)
    if header is None:
        return None
    entries = read_entries(path)
    messages = [
        e.message for e in entries
        if isinstance(e, SessionMessageEntry) and e.message is not None
    ]
    # first message text
    first_text = ""
    for m in messages:
        role = getattr(m, "role", None)
        if role == "user":
            c = getattr(m, "content", "")
            if isinstance(c, str):
                first_text = c
            elif isinstance(c, list) and c:
                first_text = getattr(c[0], "text", "") or ""
            if first_text:
                break
    # all text (for search)
    parts: list[str] = []
    for m in messages:
        role = getattr(m, "role", None)
        if role == "user":
            c = getattr(m, "content", "")
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, list):
                parts.extend(getattr(b, "text", "") or "" for b in c if hasattr(b, "text"))
    # name from session_info entry
    name = None
    for e in reversed(entries):
        if isinstance(e, SessionInfoEntry):
            name = e.name
            break
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    # created from header timestamp (parse ISO-ish); fall back to mtime.
    created = mtime
    try:
        created = datetime.fromisoformat(header.timestamp.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        pass
    return SessionInfo(
        path=str(path), id=header.id, cwd=header.cwd,
        created=created, modified=mtime,
        message_count=len(messages),
        first_message=first_text,
        all_messages_text="\n".join(parts),
        name=name,
    )
