"""SessionManager: in-memory entries + JSONL persistence.

Owns the session entry list and the leaf pointer. Appends entries (messages,
compaction markers, model/thinking changes) and persists them to a JSONL file.
``build_session_context`` reconstructs the active message list, honoring the
latest compaction (summarizing everything before first_kept_entry_id).

Leaf pointers are maintained in memory while entries are persisted to JSONL.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path

from agent_llm import Message

from agent_core.session.ids import create_session_id, generate_entry_id, is_valid_session_id
from agent_core.session.messages import CompactionSummaryMessage
from agent_core.session.storage import (
    append_entry_line,
    build_session_info,
    compute_leaf_id,
    iso_now,
    list_session_files,
    read_entries,
    read_header,
    session_dir_for_cwd,
    session_file_path,
    write_header_line,
)
from agent_core.session.types import (
    CompactionEntry,
    CompactionResult,
    LeafEntry,
    ModelChangeEntry,
    SessionContext,
    SessionEntry,
    SessionHeader,
    SessionInfo,
    SessionInfoEntry,
    SessionMessageEntry,
    SessionTreeNode,
    ThinkingLevelChangeEntry,
)

__all__ = ["SessionManager"]


class SessionManager:
    """Manages one session's entries + JSONL persistence.

    Usage:
        sm = SessionManager.create(cwd=os.getcwd())     # new session
        # ... agent runs, call sm.append_message(...) per message ...
        sm = SessionManager.open(path)                   # resume

    The leaf pointer selects the current branch tip in the (implicit) entry
    tree; for this cut we keep it in memory and always append to it.
    """

    def __init__(
        self,
        *,
        header: SessionHeader,
        entries: list[SessionEntry] | None = None,
        leaf_id: str | None = None,
        path: Path | None = None,
        agent_dir: Path | None = None,
        sessions_dir: Path | None = None,
        in_memory: bool = False,
    ) -> None:
        self.header = header
        self.entries: list[SessionEntry] = list(entries or [])
        self.leaf_id: str | None = leaf_id
        self.path: Path | None = path
        self.agent_dir: Path | None = agent_dir
        self.sessions_dir: Path | None = sessions_dir
        self.in_memory: bool = in_memory
        # Flush-on-first-assistant buffering:
        # before the file is created we buffer entries in memory.
        self._flushed: bool = path is not None and (path.exists() if path else False)
        self._buffer: list[SessionEntry] = [] if not self._flushed else list(self.entries)

    # ─── factories ─────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        *,
        cwd: str | None = None,
        agent_dir: Path | None = None,
        sessions_dir: Path | None = None,
        in_memory: bool = False,
        session_id: str | None = None,
    ) -> "SessionManager":
        """Create a brand-new session."""
        cwd = cwd or os.getcwd()
        if agent_dir is not None and sessions_dir is not None:
            raise ValueError("agent_dir and sessions_dir are mutually exclusive")
        session_id = session_id or create_session_id()
        if not is_valid_session_id(session_id):
            raise ValueError(f"Invalid session id: {session_id!r}")
        ts = iso_now()
        header = SessionHeader(id=session_id, timestamp=ts, cwd=cwd)
        path = None if in_memory else session_file_path(
            header, cwd, agent_dir, sessions_dir=sessions_dir,
        )
        sm = cls(
            header=header,
            agent_dir=agent_dir,
            sessions_dir=sessions_dir,
            path=path,
            in_memory=in_memory,
        )
        return sm

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        agent_dir: Path | None = None,
        sessions_dir: Path | None = None,
    ) -> "SessionManager":
        """Open an existing JSONL session file."""
        if agent_dir is not None and sessions_dir is not None:
            raise ValueError("agent_dir and sessions_dir are mutually exclusive")
        path = Path(path)
        header = read_header(path)
        if header is None:
            raise FileNotFoundError(f"Not a valid session file: {path}")
        entries = read_entries(path)
        # 用 compute_leaf_id：如果文件末尾有 LeafEntry，恢复到它指向的叶；
        # 否则退化为"最后一条 entry"，与旧行为完全一致。
        leaf_id = compute_leaf_id(entries)
        sm = cls(
            header=header, entries=entries, leaf_id=leaf_id,
            path=path,
            agent_dir=agent_dir,
            sessions_dir=sessions_dir,
            in_memory=False,
        )
        sm._flushed = True
        return sm

    @classmethod
    def continue_recent(
        cls,
        *,
        cwd: str | None = None,
        agent_dir: Path | None = None,
        sessions_dir: Path | None = None,
    ) -> "SessionManager | None":
        """Open the most recently modified session for this cwd, or None.

        continueRecent.
        """
        cwd = cwd or os.getcwd()
        project_sessions_dir = session_dir_for_cwd(
            cwd, agent_dir, sessions_dir=sessions_dir,
        )
        files = list_session_files(project_sessions_dir)
        if not files:
            return None
        return cls.open(
            files[0], agent_dir=agent_dir, sessions_dir=sessions_dir,
        )

    @classmethod
    def list_sessions(
        cls,
        *,
        cwd: str | None = None,
        agent_dir: Path | None = None,
        sessions_dir: Path | None = None,
    ) -> list[SessionInfo]:
        """List sessions for this cwd, newest first."""
        cwd = cwd or os.getcwd()
        project_sessions_dir = session_dir_for_cwd(
            cwd, agent_dir, sessions_dir=sessions_dir,
        )
        infos: list[SessionInfo] = []
        for p in list_session_files(project_sessions_dir):
            info = build_session_info(p)
            if info is not None:
                infos.append(info)
        return infos

    @classmethod
    def fork(
        cls,
        source: "SessionManager",
        *,
        cwd: str | None = None,
        agent_dir: Path | None = None,
        sessions_dir: Path | None = None,
    ) -> "SessionManager":
        """Create a new session containing the source's active branch."""
        if agent_dir is not None and sessions_dir is not None:
            raise ValueError("agent_dir and sessions_dir are mutually exclusive")
        fork_cwd = cwd or source.header.cwd or os.getcwd()
        header = SessionHeader(
            id=create_session_id(),
            timestamp=iso_now(),
            cwd=fork_cwd,
            parent_session=source.header.id,
        )
        entries = copy.deepcopy(source.get_branch())
        leaf_id = entries[-1].id if entries else None
        path = session_file_path(
            header, fork_cwd, agent_dir, sessions_dir=sessions_dir,
        )
        return cls(
            header=header,
            entries=entries,
            leaf_id=leaf_id,
            path=path,
            agent_dir=agent_dir,
            sessions_dir=sessions_dir,
        )

    # ─── id helpers ────────────────────────────────────────────────────

    def _existing_ids(self) -> set[str]:
        return {e.id for e in self.entries}

    def _next_entry_id(self) -> str:
        return generate_entry_id(self._existing_ids())

    def _parent_for_new_entry(self) -> str | None:
        # Append under the current leaf (or the last entry if leaf unset).
        if self.leaf_id is not None:
            return self.leaf_id
        return self.entries[-1].id if self.entries else None

    # ─── append public API ─────────────────────────────────────────────

    def append_message(self, message: Message, *, thinking_level: str | None = None) -> SessionMessageEntry:
        """Append a message entry."""
        entry = SessionMessageEntry(
            type="message",
            id=self._next_entry_id(),
            parent_id=self._parent_for_new_entry(),
            timestamp=iso_now(),
            message=message,
            thinking_level=thinking_level,
        )
        self._commit(entry)
        return entry

    def append_compaction(
        self,
        result: CompactionResult,
        *,
        from_hook: bool = False,
    ) -> CompactionEntry:
        """Append a compaction marker."""
        entry = CompactionEntry(
            type="compaction",
            id=self._next_entry_id(),
            parent_id=self._parent_for_new_entry(),
            timestamp=iso_now(),
            summary=result.summary,
            first_kept_entry_id=result.first_kept_entry_id,
            tokens_before=result.tokens_before,
            details=result.details,
            from_hook=from_hook,
        )
        self._commit(entry)
        return entry

    def append_model_change(self, *, provider: str, model_id: str) -> ModelChangeEntry:
        entry = ModelChangeEntry(
            type="model_change",
            id=self._next_entry_id(),
            parent_id=self._parent_for_new_entry(),
            timestamp=iso_now(),
            provider=provider, model_id=model_id,
        )
        self._commit(entry)
        return entry

    def append_thinking_level_change(self, level: str) -> ThinkingLevelChangeEntry:
        entry = ThinkingLevelChangeEntry(
            type="thinking_level_change",
            id=self._next_entry_id(),
            parent_id=self._parent_for_new_entry(),
            timestamp=iso_now(),
            thinking_level=level,
        )
        self._commit(entry)
        return entry

    def set_name(self, name: str) -> SessionInfoEntry:
        entry = SessionInfoEntry(
            type="session_info",
            id=self._next_entry_id(),
            parent_id=self._parent_for_new_entry(),
            timestamp=iso_now(),
            name=name,
        )
        self._commit(entry)
        return entry

    # ─── commit: in-memory + on-disk ──────────────────────────────────

    def _commit(self, entry: SessionEntry) -> None:
        """Add to memory and persist. Handles flush-on-first-assistant."""
        self.entries.append(entry)
        self.leaf_id = entry.id
        if self.in_memory or self.path is None:
            return
        # Flush-on-first-assistant: only materialize the file once an assistant
        # message lands. Until then buffer everything in memory.
        is_first_assistant = (
            isinstance(entry, SessionMessageEntry)
            and entry.message is not None
            and getattr(entry.message, "role", None) == "assistant"
        )
        if not self._flushed:
            if not is_first_assistant:
                # Buffer; nothing on disk yet.
                return
            # First assistant message: create the file with header + all buffered entries.
            write_header_line(self.path, self.header)
            self._flushed = True
            # Flush the buffer (everything except this just-added entry, which
            # we append below).
            for buffered in self.entries[:-1]:
                append_entry_line(self.path, buffered)
        append_entry_line(self.path, entry)

    def flush(self) -> None:
        """Force-create the file even if no assistant message has landed yet.

        Useful for /save before any assistant reply. No-op if already flushed
        or in-memory.
        """
        if self.in_memory or self.path is None or self._flushed:
            return
        write_header_line(self.path, self.header)
        self._flushed = True
        for buffered in self.entries:
            append_entry_line(self.path, buffered)

    # ─── queries ───────────────────────────────────────────────────────

    def get_name(self) -> str | None:
        """Latest session_info.name on the branch, or None.

        Walks entries in reverse to find the most recent ``session_info`` entry.
        Empty/whitespace names explicitly clear the title → return None.
        """
        for e in reversed(self.entries):
            if isinstance(e, SessionInfoEntry):
                name = (e.name or "").strip()
                return name or None
        return None

    def get_branch(self, from_id: str | None = None) -> list[SessionEntry]:
        """Walk leaf->root and return the path (root-first).

        For this cut (no branching), the path is simply all entries up to and
        including the leaf, in append order. ``from_id`` selects a different
        leaf than the current one.
        """
        if from_id is None and self.leaf_id is not None:
            from_id = self.leaf_id
        if from_id is None:
            return list(self.entries)
        # Build child->parent map and walk up from from_id.
        by_id = {e.id: e for e in self.entries}
        if from_id not in by_id:
            return list(self.entries)
        path: list[SessionEntry] = []
        cur: str | None = from_id
        seen: set[str] = set()
        while cur is not None and cur in by_id and cur not in seen:
            seen.add(cur)
            path.append(by_id[cur])
            cur = by_id[cur].parent_id
        path.reverse()
        return path

    # ─── 分支操作（/tree 用） ────────────────────────────────────────────

    def set_leaf_id(self, target_id: str | None) -> None:
        """切换叶指针并持久化。

        追加一条 LeafEntry 到 JSONL，把叶指针切换到 ``target_id``。下次
        重新打开该会话时，``compute_leaf_id`` 会扫到这条 LeafEntry 并
        恢复到 ``target_id`` 指向的叶。

        ``target_id`` 必须指向一个已存在的 entry（或 None 表示重置到根）。
        切换后内存里的 ``self.leaf_id`` 立即同步，无需重新 open。
        """
        if target_id is not None and not any(e.id == target_id for e in self.entries):
            raise ValueError(f"找不到 entry: {target_id}")
        entry = LeafEntry(
            type="leaf",
            id=self._next_entry_id(),
            parent_id=self._parent_for_new_entry(),
            timestamp=iso_now(),
            target_id=target_id,
        )
        self._commit(entry)
        # _commit 会把 leaf_id 设成这条 LeafEntry 自己的 id，但这不对——
        # LeafEntry 是元数据条目，叶指针应该是它的 target_id。
        self.leaf_id = target_id

    def branch(self, entry_id: str) -> None:
        """仅内存切换叶指针，不持久化。

        用于"瞬时切换"场景（例如预览某个分支但还没决定要不要写盘）。
        切换后想持久化请额外调用 ``set_leaf_id``。
        """
        if not any(e.id == entry_id for e in self.entries):
            raise ValueError(f"找不到 entry: {entry_id}")
        self.leaf_id = entry_id

    def get_tree(self) -> list[SessionTreeNode]:
        """构建嵌套会话树，供 /tree 选择器渲染。

        算法：
          1. 为所有可见 entry 建立 ``id -> node`` 映射
          2. 建立 ``parent_id -> [children]`` 索引；父节点缺失的孤儿视为根
          3. 用显式栈连接节点，每一层的 children 按 timestamp 稳定排序

        LeafEntry 不参与树结构（它是元数据，不该作为可见节点）。
        """
        visible_entries = [
            entry for entry in self.entries
            if not isinstance(entry, LeafEntry)
        ]
        nodes_by_id = {
            entry.id: SessionTreeNode(entry=entry, children=[])
            for entry in visible_entries
        }

        children_by_parent: dict[str, list[SessionEntry]] = {}
        root_entries: list[SessionEntry] = []
        for entry in visible_entries:
            parent_id = entry.parent_id
            if (
                parent_id is None
                or parent_id not in nodes_by_id
                or parent_id == entry.id
            ):
                root_entries.append(entry)
            else:
                children_by_parent.setdefault(parent_id, []).append(entry)

        def sort_key(entry: SessionEntry) -> str:
            return entry.timestamp
        root_entries.sort(key=sort_key)
        for children in children_by_parent.values():
            children.sort(key=sort_key)

        roots: list[SessionTreeNode] = []
        attached_ids: set[str] = set()

        def attach_from_root(root_entry: SessionEntry) -> None:
            """连接一棵可达子树；重复引用和环在首次访问处截断。"""
            if root_entry.id in attached_ids:
                return
            root = nodes_by_id[root_entry.id]
            roots.append(root)
            attached_ids.add(root_entry.id)
            stack = [root]
            while stack:
                parent = stack.pop()
                added_children: list[SessionTreeNode] = []
                for child_entry in children_by_parent.get(parent.entry.id, []):
                    if child_entry.id in attached_ids:
                        continue
                    child = nodes_by_id[child_entry.id]
                    parent.children.append(child)
                    attached_ids.add(child_entry.id)
                    added_children.append(child)
                # 逆序入栈，保持与递归 DFS 相同的 timestamp 顺序。
                stack.extend(reversed(added_children))

        for root_entry in root_entries:
            attach_from_root(root_entry)

        # 没有自然根的残余节点只能来自环或损坏数据。把其中最早的节点提升
        # 为根并迭代连接，保证 /tree 仍能展示所有可恢复条目。
        for entry in sorted(visible_entries, key=sort_key):
            attach_from_root(entry)

        return roots

    def get_latest_compaction_entry(self, entries: list[SessionEntry] | None = None) -> CompactionEntry | None:
        """Latest compaction entry on the current branch."""
        path = entries if entries is not None else self.get_branch()
        for e in reversed(path):
            if isinstance(e, CompactionEntry):
                return e
        return None

    # ─── context reconstruction (the heart of compaction effect) ───────

    def build_session_context(self, entries: list[SessionEntry] | None = None) -> SessionContext:
        """Reconstruct the active message list.

        If there is a compaction entry on the branch:
          - Start with a CompactionSummaryMessage
          - Then entries from first_kept_entry_id up to (not incl.) the compaction
          - Then everything after the compaction
        Otherwise: all message entries on the branch.

        Also derives the active thinking_level + model from settings entries.
        """
        path = entries if entries is not None else self.get_branch()
        compaction = self.get_latest_compaction_entry(path)

        messages: list = []
        if compaction is None:
            # No compaction: all message entries in order.
            for e in path:
                if isinstance(e, SessionMessageEntry) and e.message is not None:
                    messages.append(e.message)
        else:
            # Find the compaction's position in the path.
            try:
                compaction_idx = path.index(compaction)
            except ValueError:
                compaction_idx = len(path) - 1
            # Summary message first.
            messages.append(CompactionSummaryMessage(
                summary=compaction.summary, tokens_before=compaction.tokens_before,
            ))
            # Entries from first_kept_entry_id (inclusive) to compaction (exclusive).
            first_kept_id = compaction.first_kept_entry_id
            found_first_kept = not first_kept_id  # if empty, start from beginning
            for e in path[:compaction_idx]:
                if not found_first_kept and e.id == first_kept_id:
                    found_first_kept = True
                if found_first_kept and isinstance(e, SessionMessageEntry) and e.message is not None:
                    messages.append(e.message)
            # Everything after the compaction entry.
            for e in path[compaction_idx + 1:]:
                if isinstance(e, SessionMessageEntry) and e.message is not None:
                    messages.append(e.message)

        # Derive active settings from settings entries (last-wins).
        thinking_level: str | None = None
        model_info: dict | None = None
        for e in path:
            if isinstance(e, ThinkingLevelChangeEntry):
                thinking_level = e.thinking_level
            elif isinstance(e, ModelChangeEntry):
                model_info = {"provider": e.provider, "model_id": e.model_id}

        return SessionContext(messages=messages, thinking_level=thinking_level, model=model_info)
