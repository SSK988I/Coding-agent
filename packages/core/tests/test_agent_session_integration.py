"""Integration: Agent <-> SessionManager wiring.

Verifies that when a SessionManager is attached, the Agent persists messages
to it as they flow through _process_events (message_end / turn_end), and that
load_messages + build_session_context round-trips a resume scenario.

We drive the agent by calling the internal _process_events directly with
synthetic events (no API key needed), mirroring what agent_loop emits.
"""
from __future__ import annotations

from pathlib import Path

from agent_llm import AssistantMessage, Model, ModelCost, TextContent, ToolCall, ToolResultMessage, UserMessage

from agent_core import Agent, SessionManager


def _model() -> Model:
    """A minimal Model with required ModelCost fields filled."""
    return Model(id="m", context_window=64000, cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0))


def _make_agent_with_session(tmp_path: Path) -> tuple[Agent, SessionManager]:
    sm = SessionManager.create(cwd="/test", agent_dir=tmp_path)
    agent = Agent(
        model=_model(),
        system_prompt="sys",
        tools=[],
        stream_fn=lambda m, c, o: None,  # not invoked in this test
        session_manager=sm,
    )
    return agent, sm


def _run(ctx):
    """Run a coroutine to completion (sync test harness)."""
    import asyncio
    return asyncio.get_event_loop().run_until_complete(ctx) if False else asyncio.run(ctx)


# ─── persistence via _process_events ──────────────────────────────────

def test_user_message_persists_to_session(tmp_path: Path):
    agent, sm = _make_agent_with_session(tmp_path)
    user_msg = UserMessage(content="hello")
    _run(agent._process_events({"type": "message_start", "message": user_msg}))
    _run(agent._process_events({"type": "message_end", "message": user_msg}))

    assert len(sm.entries) == 1
    assert sm.entries[0].message.content == "hello"
    # No assistant yet → file not flushed (flush-on-first-assistant).
    assert not sm.path.exists()


def test_assistant_message_triggers_flush(tmp_path: Path):
    agent, sm = _make_agent_with_session(tmp_path)
    user_msg = UserMessage(content="hi")
    asst_msg = AssistantMessage(
        content=[TextContent(text="hello back")],
        provider="deepseek", model="m",
    )
    _run(agent._process_events({"type": "message_end", "message": user_msg}))
    _run(agent._process_events({"type": "message_end", "message": asst_msg}))

    # File should now exist with header + 2 entries.
    assert sm.path.exists()
    lines = sm.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3  # header + user + assistant


def test_tool_result_persists_on_turn_end(tmp_path: Path):
    agent, sm = _make_agent_with_session(tmp_path)
    tc = ToolCall(id="c1", name="read", arguments={"path": "/x"})
    asst_msg = AssistantMessage(
        content=[tc], provider="deepseek", model="m", stop_reason="tool_use",
    )
    tool_result = ToolResultMessage(
        tool_call_id="c1", tool_name="read", content=[TextContent(text="file body")],
    )
    _run(agent._process_events({"type": "message_end", "message": asst_msg}))
    _run(agent._process_events({
        "type": "turn_end", "message": asst_msg, "tool_results": [tool_result],
    }))

    # assistant + toolResult both in session.
    roles = [e.message.role for e in sm.entries if e.message is not None]
    assert roles == ["assistant", "toolResult"]


# ─── resume scenario ──────────────────────────────────────────────────

def test_resume_rebuilds_transcript_from_session(tmp_path: Path):
    # Session 1: write some messages.
    agent1, sm1 = _make_agent_with_session(tmp_path)
    u = UserMessage(content="old question")
    a = AssistantMessage(content=[TextContent(text="old answer")], provider="d", model="m")
    _run(agent1._process_events({"type": "message_end", "message": u}))
    _run(agent1._process_events({"type": "message_end", "message": a}))
    sm1.flush()
    assert sm1.path is not None

    # Session 2: open the file, build context, load into a fresh agent.
    resumed_sm = SessionManager.open(sm1.path)
    agent2 = Agent(
        model=_model(),
        system_prompt="sys", tools=[],
        stream_fn=lambda m, c, o: None,
        session_manager=resumed_sm,
    )
    ctx = resumed_sm.build_session_context()
    agent2.load_messages(ctx.messages)

    assert len(agent2.state.messages) == 2
    assert agent2.state.messages[0].role == "user"
    assert agent2.state.messages[1].role == "assistant"
    assert agent2.state.messages[0].content == "old question"


# ─── persistence errors don't break the loop ──────────────────────────

def test_session_persistence_error_is_swallowed(tmp_path: Path):
    agent, sm = _make_agent_with_session(tmp_path)
    # Sabotage the session manager so append_message raises.
    sm.append_message = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
    agent.attach_session(sm)

    user_msg = UserMessage(content="hello")
    # Should NOT raise despite the broken session.
    _run(agent._process_events({"type": "message_end", "message": user_msg}))
    # The transcript still got the message.
    assert len(agent.state.messages) == 1
