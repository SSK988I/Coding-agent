"""Tests for AgentSession.prompt overflow-retry behavior.

Verifies that when the compaction orchestrator reports ``need_retry=True``
(overflow recovery), ``AgentSession.prompt`` re-prompts the original message
once — mirroring the single-attempt overflow recovery.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_llm import Model, ModelCost

from agent_core import SessionManager
from agent_core.compaction_orchestrator import CompactionOutcome

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig


def _run(coro):
    return asyncio.run(coro)


def _model() -> Model:
    return Model(
        id="m", context_window=1000,
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )


def _make_session_with_stub_orchestrator():
    """Build an AgentSession, then swap in a stub orchestrator + stub agent.prompt."""
    sm = SessionManager.create(cwd="/test", in_memory=True)
    config = AgentSessionConfig(model=_model(), cwd="/test", session_manager=sm)
    session = AgentSession(config)

    fake_orch = MagicMock()
    fake_orch.reset_overflow_guard = MagicMock()
    fake_orch.abort = MagicMock()
    session._compaction_orchestrator = fake_orch
    session._agent.prompt = AsyncMock()  # type: ignore[assignment]
    return session, fake_orch


def test_prompt_re_prompts_on_need_retry():
    session, fake_orch = _make_session_with_stub_orchestrator()

    outcomes = iter([
        CompactionOutcome(performed=True, reason="overflow", need_retry=True, summary_preview="s"),
        CompactionOutcome(performed=False),
    ])
    fake_orch.check_compaction = AsyncMock(side_effect=lambda **kw: next(outcomes))

    _run(session.prompt("hello"))

    # Original prompt + one retry = two calls.
    assert session._agent.prompt.await_count == 2
    for call in session._agent.prompt.await_args_list:
        assert call.args[0] == "hello"


def test_prompt_does_not_retry_when_need_retry_false():
    session, fake_orch = _make_session_with_stub_orchestrator()
    fake_orch.check_compaction = AsyncMock(
        return_value=CompactionOutcome(performed=False),
    )

    _run(session.prompt("hello"))

    # No retry: single prompt call.
    assert session._agent.prompt.await_count == 1


def test_prompt_retries_at_most_once():
    """Only one overflow-recovery attempt is allowed per turn."""
    session, fake_orch = _make_session_with_stub_orchestrator()
    # Stub returns need_retry every time; prompt() must still stop after one retry.
    fake_orch.check_compaction = AsyncMock(
        return_value=CompactionOutcome(performed=True, reason="overflow", need_retry=True),
    )

    _run(session.prompt("hello"))

    # Original + exactly one retry, even though the stub keeps asking.
    assert session._agent.prompt.await_count == 2


def test_abort_compaction_delegates_to_orchestrator():
    session, fake_orch = _make_session_with_stub_orchestrator()
    session.abort_compaction()
    fake_orch.abort.assert_called_once()


def test_processing_flag_cleared_after_retry():
    """is_processing must be False after prompt() returns, even with a retry."""
    session, fake_orch = _make_session_with_stub_orchestrator()
    fake_orch.check_compaction = AsyncMock(
        return_value=CompactionOutcome(performed=True, reason="overflow", need_retry=True),
    )
    _run(session.prompt("hello"))
    assert session.is_processing is False


def test_overflow_retry_marks_internal_retry_without_marking_same_text_steer(tmp_path):
    """Overflow auto-retry tags only the retried prompt echo, not user-queued same-text steer."""
    from agent_llm import AssistantMessage, TextContent
    from coding_agent.desktop.runtime import DesktopRuntime

    sm = SessionManager.create(cwd=str(tmp_path), agent_dir=tmp_path)
    session = AgentSession(AgentSessionConfig(model=_model(), cwd=str(tmp_path), session_manager=sm, tools=[]))

    fake_orch = MagicMock()
    fake_orch.reset_overflow_guard = MagicMock()
    fake_orch.abort = MagicMock()
    fake_orch.check_compaction = AsyncMock(
        return_value=CompactionOutcome(performed=True, reason="overflow", need_retry=True, summary_preview="s"),
    )
    session._compaction_orchestrator = fake_orch

    runtime_events: list[dict] = []
    runtime = DesktopRuntime(runtime_events.append)
    runtime._session = session
    session.on_event(runtime._on_session_event)

    calls = 0

    class _Stream:
        def __init__(self, reply: AssistantMessage):
            self._reply = reply

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def result(self):
            return self._reply

    def stream_fn(_model_arg, _context, _options):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _Stream(AssistantMessage(
                content=[TextContent(text="")],
                stop_reason="error",
                error_message="context overflow",
            ))
        if calls == 2:
            # User actively sends the exact same text during the retried run.
            session.agent.follow_up("hello")
            return _Stream(AssistantMessage(content=[TextContent(text="retried answer")], stop_reason="end_turn"))
        return _Stream(AssistantMessage(content=[TextContent(text="follow-up answer")], stop_reason="end_turn"))

    session.agent.stream_fn = stream_fn
    _run(session.prompt("hello"))

    user_message_ends = [
        ev["event"]["payload"]
        for ev in runtime_events
        if ev["event"]["type"] == "message_end"
        and ev["event"]["payload"].get("message", {}).get("role") == "user"
    ]
    assert len(user_message_ends) == 3
    # 1st echo: initial prompt; 2nd echo: overflow internal retry linked via retry_of;
    # 3rd echo: explicit user follow-up with same text (no retry_of).
    original_id = user_message_ends[0]["message"].get("message_id")
    assert isinstance(original_id, str) and original_id
    assert user_message_ends[0].get("internalRetry") is None
    assert user_message_ends[0]["message"]["source"] == "prompt"
    assert user_message_ends[0]["message"].get("retry_of") is None
    assert user_message_ends[1].get("internalRetry") is True
    assert user_message_ends[1]["message"]["source"] == "prompt"
    assert user_message_ends[1]["message"]["retry_of"] == original_id
    assert user_message_ends[2].get("internalRetry") is None
    assert user_message_ends[2]["message"]["source"] == "follow_up"
    assert user_message_ends[2]["message"].get("retry_of") is None

    # Reopening from disk preserves message_id & retry_of so persisted timeline deduplication matches live events.
    restored = SessionManager.open(sm.path).build_session_context().messages
    restored_users = [(m.content, m.source, m.message_id, m.retry_of) for m in restored if m.role == "user"]
    assert len(restored_users) == 3
    assert restored_users[0] == ("hello", "prompt", original_id, None)
    assert restored_users[1][0] == "hello"
    assert restored_users[1][1] == "prompt"
    assert restored_users[1][3] == original_id
    assert restored_users[2] == ("hello", "follow_up", None, None)


def test_interleaved_follow_up_overflow_retry_links_original_prompt_across_reopen(tmp_path):
    """Send A, queue follow-up B, overflow while handling B -> retry links back to A's message_id on disk."""
    from agent_llm import AssistantMessage, TextContent
    from coding_agent.desktop.runtime import DesktopRuntime

    sm = SessionManager.create(cwd=str(tmp_path), agent_dir=tmp_path)
    session = AgentSession(AgentSessionConfig(model=_model(), cwd=str(tmp_path), session_manager=sm, tools=[]))

    fake_orch = MagicMock()
    fake_orch.reset_overflow_guard = MagicMock()
    fake_orch.abort = MagicMock()
    fake_orch.check_compaction = AsyncMock(
        return_value=CompactionOutcome(performed=True, reason="overflow", need_retry=True, summary_preview="s"),
    )
    session._compaction_orchestrator = fake_orch

    runtime_events: list[dict] = []
    runtime = DesktopRuntime(runtime_events.append)
    runtime._session = session
    session.on_event(runtime._on_session_event)

    calls = 0

    class _Stream:
        def __init__(self, reply: AssistantMessage):
            self._reply = reply

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def result(self):
            return self._reply

    def stream_fn(_model_arg, _context, _options):
        nonlocal calls
        calls += 1
        if calls == 1:
            session.agent.follow_up("B")
            return _Stream(AssistantMessage(content=[TextContent(text="reply A")], stop_reason="end_turn"))
        if calls == 2:
            return _Stream(AssistantMessage(
                content=[TextContent(text="")],
                stop_reason="error",
                error_message="context overflow",
            ))
        return _Stream(AssistantMessage(content=[TextContent(text="retried after compaction")], stop_reason="end_turn"))

    session.agent.stream_fn = stream_fn
    _run(session.prompt("A"))

    user_ends = [
        ev["event"]["payload"]
        for ev in runtime_events
        if ev["event"]["type"] == "message_end"
        and ev["event"]["payload"].get("message", {}).get("role") == "user"
    ]
    assert len(user_ends) == 3
    a_id = user_ends[0]["message"]["message_id"]
    assert a_id
    assert user_ends[0]["message"]["content"] == "A"
    assert user_ends[0].get("internalRetry") is None
    assert user_ends[1]["message"]["content"] == "B"
    assert user_ends[1]["message"]["source"] == "follow_up"
    assert user_ends[1].get("internalRetry") is None
    assert user_ends[2]["message"]["content"] == "A"
    assert user_ends[2]["message"]["retry_of"] == a_id
    assert user_ends[2].get("internalRetry") is True

    restored = SessionManager.open(sm.path).build_session_context().messages
    restored_users = [m for m in restored if m.role == "user"]
    assert [(m.content, m.source, m.retry_of) for m in restored_users] == [
        ("A", "prompt", None),
        ("B", "follow_up", None),
        ("A", "prompt", a_id),
    ]
    assert restored_users[0].message_id == a_id


def test_interleaved_follow_up_overflow_retry_with_real_compaction_orchestrator(tmp_path, monkeypatch):
    """Real CompactionOrchestrator + SessionManager reopen keeps CompactionSummary, A.message_id, B, and retry_of."""
    import agent_core.compaction_orchestrator as orchestrator_mod
    from agent_core.session.messages import CompactionSummaryMessage
    from agent_core.session.types import CompactionResult, CompactionSettings
    from agent_llm import AssistantMessage, TextContent, UserMessage
    from coding_agent.desktop.runtime import DesktopRuntime

    sm = SessionManager.create(cwd=str(tmp_path), agent_dir=tmp_path)
    # Seed older history so prepare_compaction summarizes older turns while keeping recent A/B.
    sm.append_message(UserMessage(content="older history " * 100, source="prompt", message_id="older-1"))
    sm.append_message(AssistantMessage(content=[TextContent(text="older reply " * 100)], stop_reason="stop"))
    sm.flush()

    session = AgentSession(AgentSessionConfig(model=_model(), cwd=str(tmp_path), session_manager=sm, tools=[]))
    session._compaction_orchestrator._settings = lambda: CompactionSettings(
        enabled=True, reserve_tokens=200, keep_recent_tokens=50,
    )

    async def fake_run_compact(preparation, **_kwargs):
        return CompactionResult(
            summary="Compacted prior history.",
            first_kept_entry_id=preparation.first_kept_entry_id,
            tokens_before=preparation.tokens_before,
        )

    monkeypatch.setattr(orchestrator_mod, "run_compact", fake_run_compact)

    runtime_events: list[dict] = []
    runtime = DesktopRuntime(runtime_events.append)
    runtime._session = session
    session.on_event(runtime._on_session_event)

    calls = 0

    class _Stream:
        def __init__(self, reply: AssistantMessage):
            self._reply = reply

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def result(self):
            return self._reply

    def stream_fn(_model_arg, _context, _options):
        nonlocal calls
        calls += 1
        if calls == 1:
            session.agent.follow_up("B")
            return _Stream(AssistantMessage(content=[TextContent(text="reply A")], stop_reason="stop"))
        if calls == 2:
            return _Stream(AssistantMessage(
                content=[TextContent(text="")],
                stop_reason="error",
                error_message="context overflow: prompt is too long",
            ))
        return _Stream(AssistantMessage(content=[TextContent(text="retried after compaction")], stop_reason="stop"))

    session.agent.stream_fn = stream_fn
    _run(session.prompt("A"))

    compaction_events = [ev["event"]["type"] for ev in runtime_events if ev["event"]["type"].startswith("compaction_")]
    assert "compaction_start" in compaction_events and "compaction_end" in compaction_events

    user_ends = [
        ev["event"]["payload"]
        for ev in runtime_events
        if ev["event"]["type"] == "message_end"
        and ev["event"]["payload"].get("message", {}).get("role") == "user"
    ]
    assert len(user_ends) == 3
    a_id = user_ends[0]["message"]["message_id"]
    assert a_id
    assert user_ends[0]["message"]["content"] == "A"
    assert user_ends[0].get("internalRetry") is None
    assert user_ends[1]["message"]["content"] == "B"
    assert user_ends[1]["message"]["source"] == "follow_up"
    assert user_ends[1].get("internalRetry") is None
    assert user_ends[2]["message"]["content"] == "A"
    assert user_ends[2]["message"]["retry_of"] == a_id
    assert user_ends[2].get("internalRetry") is True

    restored = SessionManager.open(sm.path).build_session_context().messages
    assert isinstance(restored[0], CompactionSummaryMessage)
    assert restored[0].summary == "Compacted prior history."
    restored_users = [m for m in restored if getattr(m, "role", None) == "user" and m.content in {"A", "B"}]
    assert [(m.content, m.source, m.retry_of) for m in restored_users] == [
        ("A", "prompt", None),
        ("B", "follow_up", None),
        ("A", "prompt", a_id),
    ]
    assert restored_users[0].message_id == a_id


def test_plan_execution_tags_explicit_source_and_preserves_it_on_overflow_retry(tmp_path):
    """execute_plan tags UserMessage with source='plan_execution' and keeps it across overflow retry."""
    from agent_llm import AssistantMessage, TextContent, ToolCall
    from coding_agent.desktop.runtime import DesktopRuntime

    sm = SessionManager.create(cwd=str(tmp_path), agent_dir=tmp_path)
    session = AgentSession(AgentSessionConfig(model=_model(), cwd=str(tmp_path), session_manager=sm, tools=[]))
    session.enter_plan_mode()
    plan_md = (
        "<proposed_plan>\n# Test Plan\n\n## Summary\nDo work.\n\n"
        "## Implementation Changes\nEdit file.\n\n## Public Interfaces\nNone.\n\n"
        "## Test Plan\nRun pytest.\n\n## Assumptions\nLocal.\n</proposed_plan>"
    )
    sm.append_message(AssistantMessage(
        content=[ToolCall(id="submit-1", name="submit_plan", arguments={"title": "Test Plan", "markdown": plan_md})],
        stop_reason="tool_use",
    ))
    _run(session._submit_plan("submit-1", "Test Plan", plan_md))
    revision = session.plan_state.latest_revision
    assert revision is not None

    fake_orch = MagicMock()
    fake_orch.reset_overflow_guard = MagicMock()
    fake_orch.abort = MagicMock()
    fake_orch.check_compaction = AsyncMock(
        return_value=CompactionOutcome(performed=True, reason="overflow", need_retry=True, summary_preview="s"),
    )
    session._compaction_orchestrator = fake_orch

    runtime_events: list[dict] = []
    runtime = DesktopRuntime(runtime_events.append)
    runtime._session = session
    session.on_event(runtime._on_session_event)

    calls = 0

    class _Stream:
        def __init__(self, reply: AssistantMessage):
            self._reply = reply

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def result(self):
            return self._reply

    def stream_fn(_model_arg, _context, _options):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _Stream(AssistantMessage(
                content=[TextContent(text="")],
                stop_reason="error",
                error_message="context overflow",
            ))
        return _Stream(AssistantMessage(content=[TextContent(text="plan done")], stop_reason="stop"))

    session.agent.stream_fn = stream_fn
    _run(session.execute_plan(revision.plan_id, revision.revision, revision.digest))

    user_ends = [
        ev["event"]["payload"]
        for ev in runtime_events
        if ev["event"]["type"] == "message_end"
        and ev["event"]["payload"].get("message", {}).get("role") == "user"
    ]
    assert len(user_ends) == 2
    assert user_ends[0].get("internalRetry") is None
    assert user_ends[0]["message"]["source"] == "plan_execution"
    assert user_ends[1].get("internalRetry") is True
    assert user_ends[1]["message"]["source"] == "plan_execution"
    assert user_ends[1]["message"]["retry_of"] == user_ends[0]["message"]["message_id"]

    restored = SessionManager.open(sm.path).build_session_context().messages
    plan_users = [m for m in restored if m.role == "user"]
    assert len(plan_users) == 2
    assert plan_users[0].source == "plan_execution"
    assert plan_users[0].message_id
    assert plan_users[0].retry_of is None
    assert plan_users[1].source == "plan_execution"
    assert plan_users[1].retry_of == plan_users[0].message_id


def test_agent_steer_and_follow_up_backfill_source_on_user_message(tmp_path):
    from agent_llm import ImageContent, TextContent, UserMessage

    sm = SessionManager.create(cwd=str(tmp_path), agent_dir=tmp_path)
    session = AgentSession(AgentSessionConfig(model=_model(), cwd=str(tmp_path), session_manager=sm, tools=[]))

    session.agent.steer(UserMessage(content="/model 看看"))
    session.agent.follow_up(UserMessage(content="/src/app.py"))
    media_steer = UserMessage(content=[TextContent(text="看图"), ImageContent(data="iVBORw0KGgo=", mime_type="image/png")])
    session.agent.steer(media_steer)

    steered_first = session.agent._steering_queue.drain()
    steered_second = session.agent._steering_queue.drain()
    followed = session.agent._follow_up_queue.drain()
    assert len(steered_first) == 1 and steered_first[0].source == "steer"
    assert len(steered_second) == 1 and steered_second[0].source == "steer"
    assert steered_second[0].content == media_steer.content
    assert media_steer.source is None
    assert len(followed) == 1 and followed[0].source == "follow_up"


def test_direct_api_image_user_message_links_overflow_retry_without_mutating_input(tmp_path):
    """Direct image prompts get retry links while retaining the caller's original message."""
    import base64
    import io
    from PIL import Image
    from agent_llm import AssistantMessage, ImageContent, TextContent, UserMessage
    from coding_agent.desktop.runtime import DesktopRuntime, _RunAttachmentPersistence

    png_buf = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(png_buf, format="PNG")
    png_b64 = base64.b64encode(png_buf.getvalue()).decode("ascii")

    vision_model = Model(
        id="vision-m",
        context_window=1000,
        input=["text", "image"],
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )
    sm = SessionManager.create(cwd=str(tmp_path), agent_dir=tmp_path)
    session = AgentSession(AgentSessionConfig(model=vision_model, cwd=str(tmp_path), session_manager=sm, tools=[]))

    fake_orch = MagicMock()
    fake_orch.reset_overflow_guard = MagicMock()
    fake_orch.abort = MagicMock()
    fake_orch.check_compaction = AsyncMock(
        return_value=CompactionOutcome(performed=True, reason="overflow", need_retry=True, summary_preview="s"),
    )
    session._compaction_orchestrator = fake_orch

    runtime_events: list[dict] = []
    runtime = DesktopRuntime(runtime_events.append)
    runtime._session = session
    session.on_event(runtime._on_session_event)

    calls = 0

    class _Stream:
        def __init__(self, reply: AssistantMessage):
            self._reply = reply

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def result(self):
            return self._reply

    def stream_fn(_model_arg, _context, _options):
        nonlocal calls
        calls += 1
        if calls == 1:
            session.agent.follow_up("B")
            return _Stream(AssistantMessage(content=[TextContent(text="reply A")], stop_reason="end_turn"))
        if calls == 2:
            return _Stream(AssistantMessage(
                content=[TextContent(text="")],
                stop_reason="error",
                error_message="context overflow",
            ))
        return _Stream(AssistantMessage(content=[TextContent(text="retried image A")], stop_reason="end_turn"))

    session.agent.stream_fn = stream_fn
    image_block = ImageContent(data=png_b64, mime_type="image/png")
    image_message = UserMessage(content=[TextContent(text="A"), image_block])

    attachments = _RunAttachmentPersistence(session)
    try:
        _run(session.prompt(image_message))
        attachments.validate()
    finally:
        attachments.close()

    assert image_message.source is None
    assert image_message.message_id is None
    assert image_message.retry_of is None

    user_ends = [
        ev["event"]["payload"]
        for ev in runtime_events
        if ev["event"]["type"] == "message_end"
        and ev["event"]["payload"].get("message", {}).get("role") == "user"
    ]
    assert len(user_ends) == 3
    assert user_ends[0]["message"]["source"] == "prompt"
    original_id = user_ends[0]["message"]["message_id"]
    assert isinstance(original_id, str) and original_id
    assert user_ends[0]["message"].get("retry_of") is None
    assert user_ends[0].get("internalRetry") is None
    assert user_ends[1]["message"]["content"] == "B"
    assert user_ends[1]["message"]["source"] == "follow_up"
    assert user_ends[1].get("internalRetry") is None
    assert user_ends[2]["message"]["source"] == "prompt"
    retried_id = user_ends[2]["message"].get("message_id")
    assert isinstance(retried_id, str) and retried_id and retried_id != original_id
    assert user_ends[2]["message"]["retry_of"] == original_id
    assert user_ends[2].get("internalRetry") is True

    state_users = [m for m in session.state.messages if getattr(m, "role", None) == "user"]
    assert len(state_users) == 3
    assert state_users[0].content == image_message.content
    assert state_users[0].message_id == original_id
    assert state_users[2] is not image_message
    assert state_users[2].message_id == retried_id
    assert state_users[2].retry_of == original_id

    restored = SessionManager.open(sm.path).build_session_context().messages
    restored_users = [m for m in restored if m.role == "user"]
    assert len(restored_users) == 3
    assert restored_users[0].source == "prompt"
    assert restored_users[0].message_id == original_id
    assert restored_users[0].retry_of is None
    assert any(isinstance(b, ImageContent) and b.data == image_block.data for b in restored_users[0].content)
    assert restored_users[1].content == "B"
    assert restored_users[1].source == "follow_up"
    assert restored_users[1].retry_of is None
    assert restored_users[2].source == "prompt"
    assert restored_users[2].message_id == retried_id
    assert restored_users[2].retry_of == original_id
    assert any(isinstance(b, ImageContent) and b.data == image_block.data for b in restored_users[2].content)

    fake_orch.check_compaction.return_value = None
    _run(session.prompt(image_message))
    reused_prompt = session.state.messages[-2]
    assert isinstance(reused_prompt, UserMessage)
    assert reused_prompt.content == image_message.content
    assert reused_prompt.message_id not in {None, original_id, retried_id}
    assert reused_prompt.retry_of is None
