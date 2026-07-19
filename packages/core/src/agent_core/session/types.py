"""Session data model.

Defines the discriminated union of SessionEntry types that get appended to a
JSONL file, plus the compaction result/settings structs used by the pure
compaction functions.

注：LeafEntry（叶指针持久化）已在本期落地，用于 /tree 切换分支后能跨重启恢复。
仍未做：fork/branch entry 类型、CustomEntry/CustomMessageEntry（扩展系统）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from agent_llm import Message

CURRENT_SESSION_VERSION = 3

#: Discriminator values for entry types.
EntryType = Literal[
    "message",
    "compaction",
    "model_change",
    "thinking_level_change",
    "session_info",
    "leaf",
]


# ─── File header ────────────────────────────

@dataclass
class SessionHeader:
    """First line of a JSONL session file."""
    id: str
    timestamp: str  # ISO 8601, also used to build the filename
    cwd: str
    type: Literal["session"] = "session"
    version: int = CURRENT_SESSION_VERSION
    parent_session: str | None = None


# ─── Entry base + variants ────────────────

# All entry types use kw_only=True so the inherited ``type`` field's default
# doesn't force every subclass field to have a default too (avoids the
# "non-default argument follows default argument" dataclass error).
@dataclass(kw_only=True)
class SessionEntry:
    """Base: every line after the header shares these fields.

    ``id`` is a short 8-hex id unique within the session. ``parent_id`` forms
    the session tree (None = root). ``timestamp`` is ISO 8601.
    """
    type: str
    id: str
    parent_id: str | None
    timestamp: str


@dataclass(kw_only=True)
class SessionMessageEntry(SessionEntry):
    """A user/assistant/toolResult message."""
    type: Literal["message"] = "message"
    message: Message | None = None
    # Thinking level snapshot at the time of this message (optional).
    thinking_level: str | None = None


@dataclass(kw_only=True)
class CompactionDetails:
    """File operations tracked across a compaction."""
    read_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class CompactionEntry(SessionEntry):
    """Marks a compaction point.

    History before ``first_kept_entry_id`` is summarized in ``summary`` and
    excluded from the active context (see build_session_context).
    """
    type: Literal["compaction"] = "compaction"
    summary: str = ""
    first_kept_entry_id: str = ""
    tokens_before: int = 0
    details: CompactionDetails | None = None
    from_hook: bool = False


@dataclass(kw_only=True)
class ModelChangeEntry(SessionEntry):
    """Records a model switch."""
    type: Literal["model_change"] = "model_change"
    provider: str = ""
    model_id: str = ""


@dataclass(kw_only=True)
class ThinkingLevelChangeEntry(SessionEntry):
    """Records a thinking-level change."""
    type: Literal["thinking_level_change"] = "thinking_level_change"
    thinking_level: str = ""


@dataclass(kw_only=True)
class SessionInfoEntry(SessionEntry):
    """Display name etc. for /sessions listing."""
    type: Literal["session_info"] = "session_info"
    name: str = ""


@dataclass(kw_only=True)
class LeafEntry(SessionEntry):
    """叶指针持久化条目。

    ``target_id`` 指向某个 entry.id，表示"当前活跃叶是该 entry"。
    追加一条 LeafEntry 即把叶指针切换并持久化到磁盘，下次打开会话
    能恢复到这个叶。``target_id=None`` 表示重置到根（用于重新编辑第一条
    用户消息之类的场景）。

    读取规则（见 storage.compute_leaf_id）：扫所有 entries 时维护一个
    游标，遇到普通 entry → 游标 = entry.id；遇到 leaf entry → 游标 =
    entry.target_id。最终游标即持久化的叶指针。没有 LeafEntry 的旧文件
    自然退化为"最后一条 entry"，完全向后兼容。
    """
    type: Literal["leaf"] = "leaf"
    target_id: "str | None" = None


# ─── 会话树节点（供 UI 渲染） ──────────────────────────────────────────

@dataclass
class SessionTreeNode:
    """会话树的一个节点。

    ``entry`` 是该节点对应的 SessionEntry；``children`` 是它的直接子节点
    （按 timestamp 排序）。由 SessionManager.get_tree() 构建，供 /tree
    选择器组件扁平化渲染用。
    """
    entry: SessionEntry
    children: "list[SessionTreeNode]" = field(default_factory=list)


# ─── Compaction pure-function structs ──────────

@dataclass
class CompactionResult:
    """Output of the ``compact()`` LLM call."""
    summary: str
    first_kept_entry_id: str
    tokens_before: int
    estimated_tokens_after: int | None = None
    details: CompactionDetails | None = None


@dataclass
class CompactionSettings:
    """Compaction tunables.

    Defaults enable compaction, reserve 16384 tokens, and keep 20000 recent tokens.
    """
    enabled: bool = True
    reserve_tokens: int = 16384
    keep_recent_tokens: int = 20000


@dataclass
class ContextUsageEstimate:
    """Mixed real-usage + estimated token count."""
    tokens: int
    usage_tokens: int  # 0 if no real usage available
    trailing_tokens: int  # estimated tokens after the last usage
    last_usage_index: int | None  # None = no assistant usage in window


@dataclass
class CutPointResult:
    """Result of find_cut_point."""
    first_kept_entry_index: int
    turn_start_index: int  # -1 if not a split turn
    is_split_turn: bool


@dataclass
class CompactionPreparation:
    """Pure-function preparation for compaction."""
    first_kept_entry_id: str
    messages_to_summarize: list
    turn_prefix_messages: list
    is_split_turn: bool
    tokens_before: int
    previous_summary: str | None = None
    file_ops: Any = None  # FileOperations (compaction.utils)
    settings: CompactionSettings | None = None


# ─── Built context + listing ─────────────

@dataclass
class SessionContext:
    """Messages reconstructed from a session for the agent + active settings."""
    messages: list  # list[Message] (may start with a CompactionSummaryMessage)
    thinking_level: str | None = None
    model: dict | None = None  # {"provider", "model_id"} or None


@dataclass
class SessionInfo:
    """Lightweight summary for /sessions listing."""
    path: str
    id: str
    cwd: str
    created: float
    modified: float
    message_count: int
    first_message: str
    all_messages_text: str
    name: str | None = None
