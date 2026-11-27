"""Durable intent and outcome of one built-in edit, scoped to its source call."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterator

from agent_llm import TextContent, ToolResultMessage

from agent_core.session.edit_calls import branch_edits
from agent_core.session.ids import generate_entry_id
from agent_core.session.session_manager import SessionManager
from agent_core.session.types import SessionEntry, SessionMessageEntry
from agent_core.tools._mutation import file_mutation_lock, resolved_file_lock, resolved_mutation_path
from agent_core.tools.edit import EditTool, PreparedEdit, _normalize_edits_params
from agent_core.types import AgentToolResult, ToolExecutionError


_SCHEMA = """
    CREATE TABLE IF NOT EXISTS edits (
        session TEXT NOT NULL, entry TEXT NOT NULL, call TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('prepared', 'completed', 'conflicted')),
        request TEXT NOT NULL, request_hash TEXT NOT NULL, target TEXT NOT NULL,
        original_version TEXT NOT NULL, before_hash TEXT NOT NULL, after_hash TEXT NOT NULL,
        summary TEXT NOT NULL, patch TEXT NOT NULL,
        PRIMARY KEY (session, entry, call)
    )
"""
_KEY = "session = ? AND entry = ? AND call = ?"
_STORAGE_ERRORS = (sqlite3.Error, OSError, ValueError, TypeError)
EditGuard = Callable[[], AgentToolResult | None]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _request(cwd: str, path: str, edits: list[dict]) -> str:
    return json.dumps([os.path.normcase(os.path.abspath(cwd)), path, edits],
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _error(code: str, text: str, *, uncertain: bool = False, retryable: bool = False) -> AgentToolResult:
    details: dict[str, object] = {"code": code}
    if retryable:
        details["retryable"] = True
    return AgentToolResult(content=[TextContent(text=text)], details=details, status="uncertain" if uncertain else "failed")


def _unavailable() -> AgentToolResult:
    return _error("EDIT_RECOVERY_UNAVAILABLE", "无法核对编辑记录或文件。请检查读取权限及会话存储后重试。", uncertain=True)


def _pending(path: str, *, verified: bool = False) -> AgentToolResult:
    message = f"{path} 仍是原版本，可重试原编辑。" if verified else f"{path} 的编辑结果待核对。"
    return _error("EDIT_RECOVERY_PENDING", message, uncertain=True, retryable=verified)


@dataclass(frozen=True)
class _Record:
    state: str
    request: str
    target: str
    version: tuple[int, ...]
    before: str
    after: str
    summary: str
    patch: str

    def result(self) -> AgentToolResult:
        if self.state == "completed":
            return AgentToolResult(content=[TextContent(text=self.summary)], details={"patch": self.patch})
        if self.state == "conflicted":
            return _error("EDIT_CONFLICT", self.summary)
        return _pending(self.target)


class EditRecovery:
    """Shared by Agent dispatch, explicit retry, and read-only history projection."""

    def __init__(self, manager: SessionManager):
        self.session_id = manager.header.id
        self.path = manager.path.with_suffix(".edits.sqlite3") if manager.path and not manager.in_memory else None
        self._memory: dict[tuple[str, str, str], _Record] = {}

    @contextmanager
    def _database(self, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        assert self.path is not None
        mode = "ro" if readonly else "rw"
        with closing(sqlite3.connect(self.path.absolute().as_uri() + "?mode=" + mode, uri=True)) as db, db:
            version, = db.execute("PRAGMA user_version").fetchone()
            if version != 1:
                raise ValueError("Unsupported edit journal version")
            yield db

    async def _ensure_database(self) -> None:
        if self.path is None or self.path.exists():
            return
        # Publish a fully initialized database. A second tool may already be
        # preparing a different file in this session, with a different path lock.
        async with file_mutation_lock(str(self.path)):
            if self.path.exists():
                return
            fd, temporary = tempfile.mkstemp(dir=self.path.parent, suffix=".edits.tmp")
            os.close(fd)
            try:
                with closing(sqlite3.connect(temporary)) as db, db:
                    db.execute("BEGIN")
                    db.execute(_SCHEMA)
                    db.execute("PRAGMA user_version=1")
                os.replace(temporary, self.path)
            finally:
                Path(temporary).unlink(missing_ok=True)

    def _load(self, key: tuple[str, str, str], *, readonly: bool = False) -> _Record | None:
        if self.path is None:
            return self._memory.get(key)
        try:
            self.path.stat()
        except FileNotFoundError:
            return None
        with self._database(readonly=readonly) as db:
            row = db.execute(
                "SELECT state, request, request_hash, target, original_version, before_hash, after_hash, summary, patch "
                f"FROM edits WHERE {_KEY}", key,
            ).fetchone()
        if row is None:
            return None
        state, request, digest, target, version_json, before, after, summary, patch = row
        version = json.loads(version_json)
        if (state not in {"prepared", "completed", "conflicted"} or not isinstance(request, str) or _sha(request) != digest
                or not isinstance(version, list) or len(version) != 4 or not all(type(v) is int for v in version)
                or not all(isinstance(v, str) for v in (target, before, after, summary, patch))
                or len(before) != 64 or len(after) != 64 or before == after):
            raise ValueError("Invalid edit record")
        return _Record(state, request, target, tuple(version), before, after, summary, patch)

    def _insert(self, key: tuple[str, str, str], record: _Record) -> bool:
        if self.path is None:
            return self._memory.setdefault(key, record) is record
        with self._database() as db:
            cursor = db.execute(
                "INSERT INTO edits VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (session,entry,call) DO NOTHING",
                (*key, record.state, record.request, _sha(record.request), record.target, json.dumps(record.version),
                 record.before, record.after, record.summary, record.patch),
            )
            return cursor.rowcount == 1

    def _finish(self, key: tuple[str, str, str], record: _Record, state: str, reason: str | None = None) -> AgentToolResult:
        summary = record.summary if state == "completed" else (
            reason or f"{record.target} 的路径或版本发生变化。已保留当前文件，请重新读取后发起新编辑。")
        terminal = replace(record, state=state, summary=summary)
        if self.path is None:
            self._memory[key] = terminal
        else:
            try:
                with self._database() as db:
                    changed = db.execute(f"UPDATE edits SET state=?,summary=? WHERE {_KEY} AND state='prepared'",
                                         (state, summary, *key))
                    if changed.rowcount != 1:
                        raise ValueError("Prepared edit record changed")
            except _STORAGE_ERRORS:
                return _unavailable()
        return terminal.result()

    def peek(self, assistant_entry_id: str, tool_call_id: str, *, tool: EditTool | None = None,
             params: dict | None = None) -> AgentToolResult | None:
        """Read recorded results without accessing project files or creating a journal."""
        try:
            record = self._load((self.session_id, assistant_entry_id, tool_call_id), readonly=True)
            if record and tool is not None and params is not None:
                path, edits = _normalize_edits_params(params)
                return self._known(record, _request(tool.cwd, path, edits)) or record.result()
        except (*_STORAGE_ERRORS, KeyError):
            return _unavailable()
        return record.result() if record else None

    def original_cwd(self, assistant_entry_id: str, tool_call_id: str) -> str | None:
        try:
            record = self._load((self.session_id, assistant_entry_id, tool_call_id), readonly=True)
            if record is not None:
                cwd, _, _ = json.loads(record.request)
                if isinstance(cwd, str) and os.path.isabs(cwd):
                    return cwd
        except _STORAGE_ERRORS:
            pass
        return None

    def materialize_fork_results(self, entries: list[SessionEntry]) -> None:
        """Copy confirmed outcomes to a new branch history, never pending write authority."""
        insertions: dict[str, list[ToolResultMessage]] = {}
        replacements: dict[int, ToolResultMessage] = {}
        for item in branch_edits(entries):
            if not item.unresolved:
                continue
            cwd = self.original_cwd(item.entry_id, item.call.id)
            if cwd is None:
                continue
            result = self.peek(item.entry_id, item.call.id, tool=EditTool(cwd), params=item.call.arguments)
            if result is None or result.status == "uncertain":
                continue
            message = ToolResultMessage(tool_call_id=item.call.id, tool_name="edit", content=result.content,
                                        details=result.details, status=result.status, is_error=result.status != "completed",
                                        timestamp=item.assistant.timestamp)
            if item.saved is not None:
                replacements[id(item.saved)] = message
            else:
                insertions.setdefault(item.entry_id, []).append(message)
        if not insertions and not replacements:
            return
        ids = {entry.id for entry in entries}
        branch = []
        parents = {}
        for entry in entries:
            if isinstance(entry, SessionMessageEntry) and id(entry.message) in replacements:
                entry.message = replacements[id(entry.message)]
            if entry.parent_id in parents:
                entry.parent_id = parents[entry.parent_id]
            branch.append(entry)
            for result in insertions.get(entry.id, []):
                new_id = generate_entry_id(ids)
                ids.add(new_id)
                branch.append(SessionMessageEntry(id=new_id, parent_id=branch[-1].id, timestamp=entry.timestamp,
                                                  message=result))
                parents[entry.id] = new_id
        entries[:] = branch

    async def execute(self, tool: EditTool, assistant_entry_id: str, tool_call_id: str, params: dict,
                      *, guard: EditGuard | None = None) -> AgentToolResult:
        result = await self._run(tool, assistant_entry_id, tool_call_id, params, new=True, retry=False, guard=guard)
        assert result is not None
        return result

    async def recover(self, tool: EditTool, assistant_entry_id: str, tool_call_id: str, params: dict,
                      *, retry: bool = False, guard: EditGuard | None = None) -> AgentToolResult | None:
        """Reconcile only an existing intent. A new file write requires explicit retry."""
        try:
            return await self._run(tool, assistant_entry_id, tool_call_id, params, new=False, retry=retry, guard=guard)
        except (OSError, KeyError, ValueError, TypeError):
            # A damaged historical request or an inaccessible lock directory
            # must not prevent opening the rest of the session.
            return _unavailable()

    @staticmethod
    def _known(record: _Record | None, request: str) -> AgentToolResult | None:
        if record is not None:
            if record.request != request:
                return _error("EDIT_OPERATION_MISMATCH", "同一次编辑的参数发生变化。已保留原操作及文件。")
            if record.state != "prepared":
                return record.result()
        return None

    async def _run(self, tool: EditTool, entry: str, call: str, params: dict, *, new: bool,
                   retry: bool, guard: EditGuard | None) -> AgentToolResult | None:
        path, edits = _normalize_edits_params(params)
        key = (self.session_id, entry, call)
        request = _request(tool.cwd, path, edits)
        try:
            record = self._load(key)
        except _STORAGE_ERRORS:
            return _unavailable()
        cached = self._known(record, request)
        if cached or (record is None and not new):
            return cached

        source_path = os.path.join(tool.cwd, path)
        lock = resolved_file_lock(record.target) if record else file_mutation_lock(source_path)
        async with lock as locked_path:
            if guard is not None and (blocked := guard()) is not None:
                return blocked
            try:
                record = self._load(key)
            except _STORAGE_ERRORS:
                return _unavailable()
            cached = self._known(record, request)
            if cached:
                return cached
            if record is None:
                if not new:
                    return None
                prepared = tool.prepare_edit(path, edits)
                if prepared.resolved_path != locked_path:
                    return _error("EDIT_CONFLICT", f"{path} 在准备编辑期间改指其他文件。已保留当前内容。")
                record = _Record("prepared", request, prepared.resolved_path, prepared.version,
                                 prepared.before_hash, prepared.after_hash, prepared.result.content[0].text,
                                 prepared.result.details["patch"])
                try:
                    await self._ensure_database()
                    if guard is not None and (blocked := guard()) is not None:
                        return blocked
                    inserted = self._insert(key, record)
                    if not inserted:
                        winner = self._load(key)
                        return self._known(winner, request) or _unavailable()
                except _STORAGE_ERRORS:
                    return _error("EDIT_PREPARE_FAILED", "无法保存编辑意图，文件尚未修改。请检查会话存储。")
                if guard is not None and (blocked := guard()) is not None:
                    return blocked
                return self._commit(tool, prepared, key, record)
            if locked_path != record.target or resolved_mutation_path(source_path) != record.target:
                return self._finish(key, record, "conflicted")
            try:
                resolved, version, current = tool.inspect_file(path)
                if resolved != locked_path or resolved != record.target:
                    return self._finish(key, record, "conflicted")
                if current == record.after:
                    return self._finish(key, record, "completed")
                if current != record.before or version != record.version:
                    return self._finish(key, record, "conflicted")
                if not record.version[1]:
                    # Identical bytes alone cannot identify the original file.
                    return _unavailable()
                if not retry:
                    return _pending(path, verified=True)
                prepared = tool.prepare_edit(path, edits)
            except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
                return self._finish(key, record, "conflicted")
            except ToolExecutionError as exc:
                if isinstance(exc.details, dict) and exc.details.get("code") == "EDIT_CONFLICT":
                    return self._finish(key, record, "conflicted")
                return _unavailable()
            except (OSError, ValueError, RuntimeError):
                return _unavailable()
            if (prepared.after_hash != record.after or prepared.before_hash != record.before
                    or prepared.version != record.version or prepared.resolved_path != resolved):
                return self._finish(key, record, "conflicted")
            return self._commit(tool, prepared, key, record)

    def _commit(self, tool: EditTool, prepared: PreparedEdit, key: tuple[str, str, str],
                record: _Record) -> AgentToolResult:
        try:
            tool.commit_edit(prepared)
        except ToolExecutionError as exc:
            if isinstance(exc.details, dict) and exc.details.get("code") == "EDIT_CONFLICT":
                return self._finish(key, record, "conflicted", str(exc))
            raise
        except OSError as exc:
            return _error("EDIT_RECOVERY_PENDING", f"{record.target} 的编辑结果待核对：{exc}", uncertain=True)
        return self._finish(key, record, "completed")
