"""Analysis history follows the active branch and never invents missing usage."""
from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from agent_core import SessionManager
from agent_core.session.types import CompactionResult, ModelChangeEntry, SessionMessageEntry
from agent_llm import AssistantMessage, Model, ModelCost, TextContent, ToolResultMessage, Usage, UsageCost, UserMessage

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.usage_analysis import project_usage_requests


def request(usage=None, *, model="model-a", stop="stop", timestamp=1_700_000_000_123):
    return AssistantMessage(
        model=model, provider="test-provider", stop_reason=stop,
        content=[TextContent(text="private prompt output")], usage=usage if usage is not None else Usage(),
        timestamp=timestamp, provider_data={"private": "provider-secret"},
    )


def entry(message, *, identity="request-1", timestamp="2026-01-02T03:04:05Z"):
    return SessionMessageEntry(id=identity, parent_id=None, timestamp=timestamp, message=message)


def make_session(manager, cwd="."):
    return AgentSession(AgentSessionConfig(
        cwd=cwd, session_manager=manager, no_builtin_tools=True,
        model=Model(id="model-b", provider="test-provider", context_window=100_000, cost=ModelCost(0, 0, 0, 0)),
    ))


def test_request_includes_cache_in_input_and_preserves_model_cost_and_outcome():
    usage = Usage(input=100, output=25, cache_read=40, cache_write=10,
                  cache_read_reported=True, cost=UsageCost(total=0.0012))
    row = project_usage_requests([entry(request(usage, stop="aborted"))])[0]
    assert row == {
        "id": "request-1", "index": 1, "timestamp": 1_700_000_000_123,
        "provider": "test-provider", "model": "model-a", "stopReason": "aborted",
        "inputTokens": 150, "uncachedInputTokens": 100, "outputTokens": 25,
        "cacheReadTokens": 40, "cacheWriteTokens": 10,
        "cacheHitRate": pytest.approx(40 / 150 * 100), "cost": 0.0012,
    }


@pytest.mark.parametrize("reported", [None, False])
def test_default_or_unreported_zero_usage_remains_unknown(reported):
    row = project_usage_requests([entry(request(Usage(cache_read_reported=reported)))])[0]
    assert all(row[key] is None for key in (
        "inputTokens", "uncachedInputTokens", "outputTokens", "cacheReadTokens",
        "cacheWriteTokens", "cacheHitRate", "cost",
    ))


def test_explicit_zero_usage_is_visible_but_zero_input_has_no_cache_percentage():
    row = project_usage_requests([entry(request(Usage(cache_read_reported=True)))])[0]
    assert row["inputTokens"] == row["outputTokens"] == row["cacheReadTokens"] == row["cacheWriteTokens"] == 0
    assert row["cost"] == 0
    assert row["cacheHitRate"] is None
    with_input = project_usage_requests([entry(request(Usage(input=20, cache_read_reported=True)))])[0]
    assert with_input["cacheHitRate"] == 0


@pytest.mark.parametrize("reported,cached,expected", [(True, 0, 0), (False, 0, None), (None, 0, None), (None, 60, 60)])
def test_cache_availability_matches_shared_metrics(reported, cached, expected):
    row = project_usage_requests([entry(request(Usage(
        input=100 - cached, output=20, cache_read=cached, cache_read_reported=reported,
    )))])[0]
    assert row["inputTokens"] == 100
    assert row["outputTokens"] == 20
    assert row["cacheHitRate"] == expected
    assert row["cacheReadTokens"] == (cached if expected is not None else None)


@pytest.mark.parametrize("invalid", [-1, float("nan"), float("inf"), True, "24", 1.5, 10**400])
def test_invalid_token_fields_are_gaps_not_truncated_or_nonfinite_numbers(invalid):
    usage = SimpleNamespace(input=invalid, output=7, cache_read=0, cache_write=0, cache_read_reported=True,
                            cost=SimpleNamespace(total=float("inf")))
    message = request()
    message.usage = usage
    rows = project_usage_requests([entry(message)])
    assert rows[0]["uncachedInputTokens"] is None
    assert rows[0]["inputTokens"] is None
    assert rows[0]["outputTokens"] == 7
    assert rows[0]["cacheHitRate"] is None
    assert rows[0]["cost"] is None
    json.dumps(rows, allow_nan=False)


@pytest.mark.parametrize("invalid", [None, -1, float("nan"), float("inf"), True, "yesterday"])
def test_bad_message_timestamp_uses_recorded_entry_time_without_reading_the_clock(invalid):
    row = project_usage_requests([entry(request(timestamp=invalid))])[0]
    assert row["timestamp"] == 1_767_323_045_000
    assert project_usage_requests([entry(request(timestamp=invalid), timestamp="corrupt")])[0]["timestamp"] is None


def test_only_assistant_entries_are_exposed_and_raw_contents_are_omitted():
    rows = project_usage_requests([
        entry(UserMessage(content="private user prompt")),
        ModelChangeEntry(id="model-change", parent_id=None, timestamp="", provider="test", model_id="other"),
        entry(ToolResultMessage(content=[TextContent(text="private tool output")])),
        entry(request()),
    ])
    assert len(rows) == 1 and rows[0]["index"] == 1
    serialized = json.dumps(rows)
    assert "private" not in serialized and "provider-secret" not in serialized
    assert "content" not in rows[0] and "provider_data" not in rows[0]


def test_analysis_keeps_pre_compaction_requests_and_uses_only_the_selected_branch(tmp_path):
    manager = SessionManager.create(cwd=str(tmp_path), in_memory=True)
    manager.append_message(UserMessage(content="first"))
    first = manager.append_message(request(Usage(input=100, output=20, cache_read_reported=True)))
    kept = manager.append_message(UserMessage(content="second"))
    second = manager.append_message(request(Usage(input=200, output=30, cache_read_reported=True), model="model-b"))
    manager.append_model_change(provider="test-provider", model_id="model-b")
    manager.append_compaction(CompactionResult(summary="## Goal\nEarlier requests", first_kept_entry_id=kept.id, tokens_before=500))
    third = manager.append_message(request(Usage(input=50, output=10, cache_read_reported=True), stop="error"))
    original_leaf = manager.leaf_id
    session = make_session(manager, str(tmp_path))
    try:
        analysis = session.get_usage_analysis()
        assert [row["id"] for row in analysis["requests"]] == [first.id, second.id, third.id]
        assert [row["index"] for row in analysis["requests"]] == [1, 2, 3]
        assert [row["model"] for row in analysis["requests"]] == ["model-a", "model-b", "model-a"]
        assert analysis["leafId"] == original_leaf
        assert sum(row["inputTokens"] for row in analysis["requests"]) == analysis["stats"].metrics.input_tokens == 350
        assert asdict(analysis["stats"]) == asdict(session.get_stats())

        manager.set_leaf_id(kept.id)
        alternative = manager.append_message(request(Usage(input=1, output=2, cache_read_reported=True), model="alternative"))
        session.agent.load_messages(manager.build_session_context().messages)
        branch = session.get_usage_analysis()
        assert [row["id"] for row in branch["requests"]] == [first.id, alternative.id]
        assert branch["stats"].metrics.input_tokens == 101
        manager.set_leaf_id(original_leaf)
        session.agent.load_messages(manager.build_session_context().messages)
        assert [row["id"] for row in session.get_usage_analysis()["requests"]] == [first.id, second.id, third.id]
    finally:
        session.dispose()


def test_analysis_of_restored_history_does_not_write_or_change_the_branch(tmp_path):
    manager = SessionManager.create(cwd=str(tmp_path), sessions_dir=tmp_path / "sessions")
    manager.append_message(UserMessage(content="saved"))
    saved = manager.append_message(request(Usage(input=12, output=3, cache_read_reported=True)))
    assert manager.path is not None
    original_bytes = manager.path.read_bytes()
    restored = SessionManager.open(manager.path)
    session = make_session(restored, str(tmp_path))
    try:
        before_entries = list(restored.entries)
        assert session.get_usage_analysis()["requests"][0]["id"] == saved.id
        assert restored.leaf_id == saved.id and restored.entries == before_entries
        assert manager.path.read_bytes() == original_bytes
    finally:
        session.dispose()
