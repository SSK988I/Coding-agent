"""Session persistence smoke tests (in-memory + JSONL file).

Verifies create/append/build_session_context round-trips, flush-on-first-
assistant, resume via open(), and the compaction-aware context rebuild.
"""
from __future__ import annotations

from pathlib import Path

from agent_llm import AssistantMessage, TextContent, ToolResultMessage, UserMessage

from agent_core.session.session_manager import SessionManager
from agent_core.session.types import CompactionResult


def _msg_user(t: str) -> UserMessage:
    return UserMessage(content=t)


def _msg_asst(t: str) -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=t)], provider="deepseek", model="m")


def _msg_tool() -> ToolResultMessage:
    return ToolResultMessage(tool_call_id="c1", tool_name="read", content=[TextContent(text="ok")])


# ─── in-memory ─────────────────────────────────────────────────────────

def test_in_memory_session_append_and_build_context():
    sm = SessionManager.create(in_memory=True)
    sm.append_message(_msg_user("hello"))
    sm.append_message(_msg_asst("hi"))
    ctx = sm.build_session_context()
    assert len(ctx.messages) == 2
    assert ctx.messages[0].role == "user"
    assert ctx.messages[1].role == "assistant"


def test_in_memory_entries_get_ids_and_parent_chain():
    sm = SessionManager.create(in_memory=True)
    e1 = sm.append_message(_msg_user("a"))
    e2 = sm.append_message(_msg_asst("b"))
    assert e1.parent_id is None
    assert e2.parent_id == e1.id
    assert sm.leaf_id == e2.id
    assert len({e.id for e in sm.entries}) == len(sm.entries)  # unique ids


# ─── JSONL file persistence ───────────────────────────────────────────
#
# Tests pass a synthetic short cwd ("/test/proj") plus the real tmp_path as
# agent_dir. Encoding the real Windows tmp_path into a directory name would
# blow past MAX_PATH=260; the agent_dir controls where files actually land.

def test_flush_on_first_assistant(tmp_path: Path):
    sm = SessionManager.create(cwd="/test/proj", agent_dir=tmp_path)
    sm.append_message(_msg_user("buffered"))  # no file yet
    assert sm.path is not None
    assert not sm.path.exists()
    sm.append_message(_msg_asst("first reply"))  # triggers flush
    assert sm.path.exists()
    # File should have header + 2 entries.
    text = sm.path.read_text(encoding="utf-8").splitlines()
    assert len(text) == 3  # header + 2 entries


def test_save_flushes_without_assistant(tmp_path: Path):
    sm = SessionManager.create(cwd="/test/proj", agent_dir=tmp_path)
    sm.append_message(_msg_user("only user"))
    assert not sm.path.exists()
    sm.flush()  # /save before any reply
    assert sm.path.exists()
    lines = sm.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # header + 1 entry


def test_open_resumes_entries_and_context(tmp_path: Path):
    sm = SessionManager.create(cwd="/test/proj", agent_dir=tmp_path)
    sm.append_message(_msg_user("q1"))
    sm.append_message(_msg_asst("a1"))
    sm.append_message(_msg_tool())
    sm.flush()
    assert sm.path is not None

    resumed = SessionManager.open(sm.path)
    assert resumed.header.id == sm.header.id
    assert len(resumed.entries) == 3
    ctx = resumed.build_session_context()
    assert len(ctx.messages) == 3
    assert ctx.messages[0].role == "user"
    assert ctx.messages[2].role == "toolResult"


def test_list_sessions_returns_infos(tmp_path: Path):
    sm1 = SessionManager.create(cwd="/test/proj", agent_dir=tmp_path)
    sm1.append_message(_msg_user("first"))
    sm1.append_message(_msg_asst("reply"))
    sm1.flush()

    sm2 = SessionManager.create(cwd="/test/proj", agent_dir=tmp_path)
    sm2.append_message(_msg_user("second"))
    sm2.append_message(_msg_asst("reply2"))
    sm2.flush()

    infos = SessionManager.list_sessions(cwd="/test/proj", agent_dir=tmp_path)
    assert len(infos) == 2
    assert all(i.message_count == 2 for i in infos)


def test_continue_recent_returns_latest(tmp_path: Path):
    sm1 = SessionManager.create(cwd="/test/proj", agent_dir=tmp_path)
    sm1.append_message(_msg_user("older"))
    sm1.append_message(_msg_asst("r"))
    sm1.flush()

    recent = SessionManager.continue_recent(cwd="/test/proj", agent_dir=tmp_path)
    assert recent is not None
    assert recent.header.id == sm1.header.id


def test_create_accepts_custom_session_id_and_sessions_directory(tmp_path: Path):
    sessions_dir = tmp_path / "custom-sessions"
    sm = SessionManager.create(
        cwd="/test/proj",
        sessions_dir=sessions_dir,
        session_id="stable-session-id",
    )

    assert sm.header.id == "stable-session-id"
    assert sm.path is not None
    assert sessions_dir in sm.path.parents


def test_create_rejects_invalid_custom_session_id():
    try:
        SessionManager.create(in_memory=True, session_id="invalid/session/id")
    except ValueError as exc:
        assert "Invalid session id" in str(exc)
    else:
        raise AssertionError("expected invalid custom session id to fail")


def test_fork_copies_active_branch_and_records_parent(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    source = SessionManager.create(cwd="/test/proj", sessions_dir=sessions_dir)
    source.append_message(_msg_user("question"))
    source.append_message(_msg_asst("answer"))

    forked = SessionManager.fork(
        source, cwd="/test/proj", sessions_dir=sessions_dir,
    )

    assert forked.header.id != source.header.id
    assert forked.header.parent_session == source.header.id
    assert forked.entries == source.get_branch()
    assert forked.entries is not source.entries
    assert forked.path != source.path


# ─── compaction-aware context rebuild ──────────────────────────────────

def test_build_context_honors_compaction():
    sm = SessionManager.create(in_memory=True)
    sm.append_message(_msg_user("old question"))
    sm.append_message(_msg_asst("old answer"))
    e_u2 = sm.append_message(_msg_user("new question"))
    sm.append_message(_msg_asst("new answer"))

    # Compact: keep from e_u2 onward (summarize the first user/assistant pair).
    result = CompactionResult(
        summary="## Goal\nold stuff",
        first_kept_entry_id=e_u2.id,
        tokens_before=1000,
    )
    sm.append_compaction(result)

    ctx = sm.build_session_context()
    # Expected: CompactionSummary + (u2, a2) = 3 messages; u1/a1 summarized away.
    assert len(ctx.messages) == 3
    assert ctx.messages[0].role == "compactionSummary"
    assert ctx.messages[1].role == "user"
    assert getattr(ctx.messages[1], "content", "") == "new question"
    assert ctx.messages[2].role == "assistant"


def test_build_context_without_compaction_keeps_all():
    sm = SessionManager.create(in_memory=True)
    sm.append_message(_msg_user("a"))
    sm.append_message(_msg_asst("b"))
    sm.append_message(_msg_user("c"))
    ctx = sm.build_session_context()
    assert len(ctx.messages) == 3
    assert all(getattr(m, "role", None) != "compactionSummary" for m in ctx.messages)


# ─── 分支（/tree）相关 ───────────────────────────────────────────────

def test_compute_leaf_id_falls_back_to_last_entry():
    """没有 LeafEntry 的旧文件，compute_leaf_id 应退化为最后一条 entry。"""
    from agent_core.session.storage import compute_leaf_id

    sm = SessionManager.create(in_memory=True)
    sm.append_message(_msg_user("u1"))
    sm.append_message(_msg_asst("a1"))
    # 没有 LeafEntry
    leaf = compute_leaf_id(sm.entries)
    assert leaf == sm.entries[-1].id


def test_compute_leaf_id_honors_leaf_entry():
    """有 LeafEntry 时，compute_leaf_id 应返回 target_id。"""
    from agent_core.session.storage import compute_leaf_id

    sm = SessionManager.create(in_memory=True)
    e_u1 = sm.append_message(_msg_user("u1"))
    sm.append_message(_msg_asst("a1"))
    sm.append_message(_msg_user("u2"))
    # 写一条 LeafEntry 把叶指针切回 u1
    sm.set_leaf_id(e_u1.id)
    leaf = compute_leaf_id(sm.entries)
    assert leaf == e_u1.id


def test_set_leaf_id_persists_and_resumes(tmp_path: Path):
    """切换叶指针后重启会话，应恢复到切换后的叶，不是最后一条 entry。"""
    sm = SessionManager.create(cwd="/test/proj", agent_dir=tmp_path)
    e_u1 = sm.append_message(_msg_user("u1"))
    sm.append_message(_msg_asst("a1"))
    sm.append_message(_msg_user("u2"))
    sm.append_message(_msg_asst("a2"))
    # 切到 u1（不 append），关闭重开
    sm.set_leaf_id(e_u1.id)

    resumed = SessionManager.open(sm.path)
    assert resumed.leaf_id == e_u1.id
    # 末尾应是 LeafEntry
    assert resumed.entries[-1].type == "leaf"
    assert resumed.entries[-1].target_id == e_u1.id
    # branch 应该是 [u1]
    branch = resumed.get_branch()
    assert len(branch) == 1
    assert branch[0].id == e_u1.id


def test_set_leaf_id_rejects_unknown_entry():
    """set_leaf_id 传入不存在的 entry_id 应抛错。"""
    sm = SessionManager.create(in_memory=True)
    sm.append_message(_msg_user("u1"))
    try:
        sm.set_leaf_id("nonexistent-id")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown entry id")


def test_get_tree_builds_branch_structure():
    """切换分支后 append 新内容，get_tree 应反映出两个分支。"""
    sm = SessionManager.create(in_memory=True)
    e_u1 = sm.append_message(_msg_user("u1"))
    e_a1 = sm.append_message(_msg_asst("a1"))
    sm.append_message(_msg_user("u2"))
    sm.append_message(_msg_asst("a2"))

    # 切回 u1，在新分支上 append
    sm.set_leaf_id(e_u1.id)
    e_new = sm.append_message(_msg_asst("新分支回复"))

    tree = sm.get_tree()
    # 1 个根（u1）
    assert len(tree) == 1
    root = tree[0]
    assert root.entry.id == e_u1.id
    # u1 应有 2 个 child（原 a1 + 新 a）
    assert len(root.children) == 2
    child_ids = {c.entry.id for c in root.children}
    assert e_a1.id in child_ids
    assert e_new.id in child_ids


def test_get_branch_walks_switched_leaf():
    """切换 leaf_id 后，get_branch() 应返回新分支的路径。"""
    sm = SessionManager.create(in_memory=True)
    e_u1 = sm.append_message(_msg_user("u1"))
    e_a1 = sm.append_message(_msg_asst("a1"))
    e_u2 = sm.append_message(_msg_user("u2"))
    sm.append_message(_msg_asst("a2"))

    # 切到 a1
    sm.set_leaf_id(e_a1.id)
    branch = sm.get_branch()
    assert [e.id for e in branch] == [e_u1.id, e_a1.id]

    # 内存切换（branch 方法，不持久化）
    sm.branch(e_u2.id)
    branch = sm.get_branch()
    assert [e.id for e in branch] == [e_u1.id, e_a1.id, e_u2.id]


def test_get_tree_skips_leaf_entries():
    """LeafEntry 是元数据，不应该出现在 get_tree 的节点里。"""
    sm = SessionManager.create(in_memory=True)
    sm.append_message(_msg_user("u1"))
    sm.append_message(_msg_asst("a1"))
    sm.set_leaf_id(sm.entries[0].id)

    tree = sm.get_tree()
    # 收集所有节点的 entry.type
    def collect(node, acc):
        acc.append(node.entry.type)
        for c in node.children:
            collect(c, acc)

    types: list[str] = []
    for root in tree:
        collect(root, types)
    assert "leaf" not in types
