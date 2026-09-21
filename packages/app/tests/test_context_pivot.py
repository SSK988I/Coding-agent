"""User-directed compaction through the shared runtime and both adapters."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agent_core import SessionManager
from agent_llm import AssistantMessage, Model, ModelCost, TextContent, ToolCall, UserMessage

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.plan_mode import PlanModeError
from coding_agent.desktop.protocol import RpcError
from coding_agent.desktop.runtime import DesktopRuntime
from coding_agent.modes.interactive.interactive_mode import InteractiveMode


class SummaryStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def result(self):
        return AssistantMessage(content=[TextContent(text="Preserve the selected design; next implement.")])


def model():
    return Model(id="test", cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0))


def test_pivot_preserves_confirmable_plan_and_session(tmp_path):
    async def scenario():
        manager = SessionManager.create(cwd=str(tmp_path), agent_dir=tmp_path)
        session = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=model(), session_manager=manager,
        ))
        session.enter_plan_mode()
        manager.append_message(UserMessage(content="Inspect and propose"))
        call = ToolCall(id="submit-1", name="submit_plan", arguments={"title": "Design", "markdown": "Do this."})
        manager.append_message(AssistantMessage(content=[call], stop_reason="tool_use"))
        await next(t for t in session.tools if t.name == "submit_plan").execute(call.id, call.arguments)
        before = session.plan_state.to_payload()
        original_id = manager.header.id
        session.agent.stream_fn = lambda *_: SummaryStream()

        outcome = await session.compact("pivot", direction="implement the design")

        assert outcome["performed"]
        assert session.plan_state.to_payload() == before
        assert session.session_manager.header.id == original_id
        assert len(session.state.messages) == 1
        assert "implement the design" in session.state.messages[0].summary
        assert not session.is_processing
        reopened = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=model(), session_manager=SessionManager.open(manager.path),
        ))
        assert reopened.plan_state.to_payload() == before
        await reopened.aclose()
        await session.aclose()

    asyncio.run(scenario())


def test_pivot_reserves_session_against_prompt_and_new_session(tmp_path):
    async def scenario():
        session = AgentSession(AgentSessionConfig(cwd=str(tmp_path), model=model()))
        entered, release = asyncio.Event(), asyncio.Event()

        async def pivot(_direction):
            entered.set()
            await release.wait()
            return SimpleNamespace(performed=False, reason="pivot", summary_preview=None, error="cancelled")

        session._compaction_orchestrator.context_pivot = pivot
        task = asyncio.create_task(session.compact(direction="review"))
        await entered.wait()
        with pytest.raises(PlanModeError):
            session.new_session()
        with pytest.raises(PlanModeError):
            await session.compact(direction="another")
        release.set()
        await task
        assert not session.is_processing
        await session.aclose()

    asyncio.run(scenario())


def test_desktop_passes_direction_and_uses_existing_abort_rpc():
    async def scenario():
        runtime = DesktopRuntime(lambda _: None)
        session = SimpleNamespace(
            compact=AsyncMock(return_value={"performed": False}), is_compacting=False,
            abort_compaction=Mock(),
        )
        runtime._session = session
        await runtime._session_compact({"direction": "implement"})
        session.compact.assert_awaited_once_with("pivot", direction="implement")
        session.is_compacting = True
        with pytest.raises(RpcError, match="压缩"):
            await runtime._run_start({"text": "run now"})
        with pytest.raises(RpcError, match="压缩"):
            await runtime._model_select({})
        assert (await runtime._run_abort({}))["aborted"]
        session.abort_compaction.assert_called_once()

    asyncio.run(scenario())


def test_tui_compact_restores_editor_and_plan_controls():
    compact = AsyncMock(return_value={"performed": True})
    unsubscribe = Mock()
    mode = SimpleNamespace(
        _is_responding=False, editor=SimpleNamespace(disable_submit=False),
        _session=SimpleNamespace(compact=compact, on_event=Mock(return_value=unsubscribe)),
        _on_agent_event=Mock(), _add_system_message=Mock(), _clear_status_indicator=Mock(),
        _refresh_footer=Mock(), _render_plan_state_controls=Mock(),
    )
    asyncio.run(InteractiveMode._cmd_compact(mode, "implement"))
    compact.assert_awaited_once_with("pivot", direction="implement")
    assert not mode.editor.disable_submit and not mode._is_responding
    unsubscribe.assert_called_once()
    mode._render_plan_state_controls.assert_called_once()
