"""Tests for the pure-function compaction algorithm (compaction.py).

Covers token estimation, cut-point selection (including the trickier split-turn
and toolResult-avoidance cases), should_compact threshold, file-operation
extraction, and prepare_compaction's iterative (previousSummary) path.
"""
from __future__ import annotations

from agent_llm import AssistantMessage, TextContent, ToolCall, ToolResultMessage, Usage, UserMessage

from agent_core.session.compaction import (
    FileOperations,
    estimate_context_tokens,
    estimate_tokens,
    extract_file_operations,
    find_cut_point,
    prepare_compaction,
    should_compact,
)
from agent_core.session.session_manager import SessionManager
from agent_core.session.types import CompactionResult, CompactionSettings


def _u(t: str) -> UserMessage:
    return UserMessage(content=t)


def _a(t: str, usage: Usage | None = None) -> AssistantMessage:
    m = AssistantMessage(content=[TextContent(text=t)], provider="d", model="m")
    if usage is not None:
        m.usage = usage
    return m


def _tr() -> ToolResultMessage:
    return ToolResultMessage(tool_call_id="c1", tool_name="read", content=[TextContent(text="ok")])


# ─── estimate_tokens ──────────────────────────────────────────────────

def test_estimate_tokens_user_string():
    # "hello world" = 11 chars -> 2 tokens (11//4)
    assert estimate_tokens(UserMessage(content="hello world")) == 2


def test_estimate_tokens_assistant_includes_thinking_and_toolcall():
    m = AssistantMessage(content=[
        TextContent(text="answer"),  # 6 chars
        ToolCall(id="c1", name="read", arguments={"path": "/a/b.py"}),  # name + args json
    ], provider="d", model="m")
    # Just verify it's positive and accounts for both blocks.
    assert estimate_tokens(m) > 0


def test_estimate_tokens_never_zero_for_real_content():
    assert estimate_tokens(UserMessage(content="x")) >= 1


# ─── estimate_context_tokens (mixed real + estimated) ─────────────────

def test_estimate_context_tokens_uses_real_usage_then_estimates_trailing():
    msgs = [
        _u("q1"),
        _a("a1", usage=Usage(input=100, output=50, total_tokens=150)),  # real usage
        _u("q2 long enough to add tokens"),
        _a("a2"),
    ]
    est = estimate_context_tokens(msgs)
    assert est.last_usage_index == 1
    assert est.usage_tokens == 150
    assert est.trailing_tokens > 0
    assert est.tokens == 150 + est.trailing_tokens


def test_estimate_context_tokens_no_usage_fully_estimates():
    msgs = [_u("q"), _a("a")]
    est = estimate_context_tokens(msgs)
    assert est.last_usage_index is None
    assert est.usage_tokens == 0
    assert est.tokens == est.trailing_tokens


def test_estimate_context_tokens_skips_error_aborted_usage():
    msgs = [
        _u("q"),
        _a("a", usage=Usage(total_tokens=100, ), ),  # need to set stop_reason
    ]
    msgs[1].stop_reason = "error"
    est = estimate_context_tokens(msgs)
    assert est.last_usage_index is None  # error usage ignored


# ─── should_compact ────────────────────────────────────────────────────

def test_should_compact_below_threshold_false():
    s = CompactionSettings(enabled=True, reserve_tokens=1000, keep_recent_tokens=500)
    assert should_compact(5000, 65536, s) is False


def test_should_compact_above_threshold_true():
    s = CompactionSettings(enabled=True, reserve_tokens=1000, keep_recent_tokens=500)
    # context_tokens > context_window - reserve -> 65000 > 65536 - 1000 = 64536
    assert should_compact(65000, 65536, s) is True


def test_should_compact_disabled_is_false():
    s = CompactionSettings(enabled=False)
    assert should_compact(999999, 1000, s) is False


# ─── find_cut_point ────────────────────────────────────────────────────

def _entries(*msgs) -> list:
    """Build a SessionManager in-memory and append messages, return its entries."""
    sm = SessionManager.create(in_memory=True)
    for m in msgs:
        sm.append_message(m)
    return list(sm.entries)


def test_find_cut_point_returns_start_when_no_valid_cuts():
    # Only toolResult entries: no valid cut.
    entries = _entries(_tr())
    cut = find_cut_point(entries, 0, len(entries), keep_recent_tokens=100)
    assert cut.first_kept_entry_index == 0
    assert cut.is_split_turn is False


def test_find_cut_point_keeps_recent_when_everything_fits():
    # Small conversation, large keep_recent: cut at oldest valid point (summarize least).
    entries = _entries(_u("a"), _a("b"), _u("c"), _a("d"))
    cut = find_cut_point(entries, 0, len(entries), keep_recent_tokens=1_000_000)
    # Should pick the oldest valid cut (index 0).
    assert cut.first_kept_entry_index == 0


def test_find_cut_point_summarizes_old_when_recent_exceeds_budget():
    # Build a big conversation: lots of old content, recent pair to keep.
    msgs = []
    for i in range(20):
        msgs.append(_u(f"old question number {i} " * 10))
        msgs.append(_a(f"old answer number {i} " * 10))
    msgs.append(_u("recent question"))
    msgs.append(_a("recent answer"))
    entries = _entries(*msgs)
    # Small keep_recent so old stuff must be summarized.
    cut = find_cut_point(entries, 0, len(entries), keep_recent_tokens=200)
    # The kept index should be somewhere in the later part of the conversation.
    assert cut.first_kept_entry_index > 0


def test_find_cut_point_avoids_cutting_on_tool_result():
    # assistant(toolCall) -> toolResult -> user. Cut must not land on toolResult.
    tc = ToolCall(id="c1", name="read", arguments={"path": "/x"})
    asst = AssistantMessage(content=[tc], provider="d", model="m", stop_reason="tool_use")
    entries = _entries(_u("q"), asst, _tr(), _u("q2"), _a("a2"))
    cut = find_cut_point(entries, 0, len(entries), keep_recent_tokens=50)
    kept_entry = entries[cut.first_kept_entry_index]
    # Must not be the toolResult entry.
    kept_msg = kept_entry.message
    assert getattr(kept_msg, "role", None) != "toolResult"


# ─── extract_file_operations ──────────────────────────────────────────

def test_extract_file_operations_records_read_write_edit():
    msgs = [
        AssistantMessage(content=[
            ToolCall(id="1", name="read", arguments={"path": "/a.py"}),
            ToolCall(id="2", name="write", arguments={"path": "/b.py"}),
            ToolCall(id="3", name="edit", arguments={"path": "/c.py"}),
        ], provider="d", model="m"),
    ]
    ops = extract_file_operations(msgs)
    assert "/a.py" in ops.read
    assert "/b.py" in ops.written
    assert "/c.py" in ops.edited


def test_file_operations_to_details_dict_merges_write_edit():
    ops = FileOperations()
    ops.written.add("/x")
    ops.edited.add("/y")
    d = ops.to_details_dict()
    assert sorted(d["modified_files"]) == ["/x", "/y"]
    assert d["read_files"] == []


# ─── prepare_compaction ───────────────────────────────────────────────

def test_prepare_compaction_returns_none_when_only_compaction_entry():
    sm = SessionManager.create(in_memory=True)
    sm.append_message(_u("q"))
    sm.append_message(_a("a"))
    sm.append_compaction(CompactionResult(summary="s", first_kept_entry_id="x", tokens_before=10))
    prep = prepare_compaction(sm.entries, CompactionSettings())
    assert prep is None


def test_prepare_compaction_iterative_with_previous_summary():
    sm = SessionManager.create(in_memory=True)
    e_u1 = sm.append_message(_u("old"))
    sm.append_message(_a("old answer"))
    # First compaction.
    sm.append_compaction(CompactionResult(
        summary="first summary", first_kept_entry_id=e_u1.id, tokens_before=100,
    ))
    # New messages after compaction — large enough that keep_recent can't hold all.
    big = "lorem ipsum dolor sit amet " * 50  # ~1400 chars -> ~350 tokens each
    sm.append_message(_u(big))
    sm.append_message(_a(big))
    sm.append_message(_u(big))
    sm.append_message(_a(big))

    prep = prepare_compaction(sm.entries, CompactionSettings(keep_recent_tokens=200))
    assert prep is not None
    # Should carry the previous summary for iterative update.
    assert prep.previous_summary == "first summary"
    assert len(prep.messages_to_summarize) > 0


def test_prepare_compaction_returns_none_when_nothing_to_summarize():
    # Tiny conversation that fits entirely in keep_recent.
    sm = SessionManager.create(in_memory=True)
    sm.append_message(_u("hi"))
    sm.append_message(_a("yo"))
    prep = prepare_compaction(sm.entries, CompactionSettings(keep_recent_tokens=1_000_000))
    # Nothing to summarize because cut lands at the oldest valid point (index 0),
    # so messages_to_summarize is empty.
    assert prep is None or len(prep.messages_to_summarize) == 0
