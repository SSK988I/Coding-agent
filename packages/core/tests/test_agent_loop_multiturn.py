"""End-to-end multi-turn tests for the agent loop.

Unlike ``test_tool_execution.py`` (which drives the internal
``_execute_tool_calls*`` directly), these tests drive the full loop through
``run_agent_loop`` / ``Agent.prompt`` with a scriptable fake ``stream_fn``.

Coverage:
  - single-turn no-tool stop (turn count, agent_end exactly once)
  - multi-turn tool chain until a plain-text turn stops the loop
  - ``terminate`` flag stops the inner loop (no follow-up → stop)
  - error/abort stop_reason still emits agent_end
  - steering queue injects a message before the next turn
  - follow-up queue re-enters the inner loop after it naturally stops
  - QueueMode "all" vs "one-at-a-time" drain semantics
  - Agent.steer / Agent.follow_up end-to-end through ``Agent.prompt``
  - agent_end fires exactly once on every termination path
"""
from __future__ import annotations

import asyncio
from typing import Any

from agent_llm import (
    AssistantMessage,
    Context,
    Model,
    ModelCost,
    TextContent,
    ToolCall,
    UserMessage,
)

from agent_core import Agent
from agent_core.agent_loop import run_agent_loop
from agent_core.types import (
    AgentContext,
    AgentLoopConfig,
    AgentToolResult,
)


# ─── harness ───────────────────────────────────────────────────────────


class _FakeStream:
    """Minimal AssistantMessageEventStream stand-in: yields no deltas, returns
    a canned final message from ``.result()`` (mirrors the pattern in
    test_compaction_orchestrator._FakeStream)."""

    def __init__(self, final: AssistantMessage) -> None:
        self._final = final

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def result(self) -> AssistantMessage:
        return self._final


class _ScriptedStream:
    """A ``stream_fn`` that pops the next scripted AssistantMessage per call.

    Lets a test script a turn sequence like [tool_call, tool_call, plain-text]
    and assert how many times the loop invoked the LLM. Raises if the loop
    asks for more turns than scripted (catches runaway loops in tests).
    """

    def __init__(self, script: list[AssistantMessage]) -> None:
        self._script = list(script)
        self._i = 0
        self.calls = 0
        self.contexts: list[Context] = []  # captured for inspection

    def __call__(self, model: Any, context: Context, options: Any = None) -> _FakeStream:
        if self._i >= len(self._script):
            raise RuntimeError(
                f"script exhausted at index {self._i} (loop asked for turn {self._i + 1})"
            )
        msg = self._script[self._i]
        self._i += 1
        self.calls += 1
        self.contexts.append(context)
        return _FakeStream(msg)


def _run(coro):
    return asyncio.run(coro)


def _assistant(content=None, *, stop_reason="stop") -> AssistantMessage:
    return AssistantMessage(
        content=content or [],
        provider="deepseek", model="m", stop_reason=stop_reason,
    )


def _text(t: str) -> AssistantMessage:
    return _assistant([TextContent(text=t)])


def _tool_call(name: str = "echo", args: dict | None = None, *, terminate: bool = False) -> AssistantMessage:
    """An assistant turn carrying a single tool_call whose result terminate=..."""
    tc = ToolCall(id=f"{name}-1", name=name, arguments=args or {})
    return _assistant([tc], stop_reason="tool_use")


class _EchoTool:
    """Minimal tool that records calls and returns its terminate flag."""

    def __init__(self, *, name: str = "echo", terminate: bool = False, record: list | None = None) -> None:
        self.name = name
        self.label = name
        self.description = "echo"
        self.parameters = {"type": "object", "properties": {}}
        self._terminate = terminate
        self._record = record

    async def execute(self, tool_call_id, params, signal=None):
        if self._record is not None:
            self._record.append({"id": tool_call_id, "params": params})
        return AgentToolResult(
            content=[TextContent(text=f"echo:{params}")],
            terminate=self._terminate,
        )


def _sink():
    """Sync event sink that appends to a list. Returns ``(events, sink)``."""
    events: list[dict] = []

    def sink(event):
        events.append(event)

    return events, sink


def _model() -> Model:
    return Model(
        id="m", context_window=10000,
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )


def _config(**kw) -> AgentLoopConfig:
    """AgentLoopConfig with a passthrough convert_to_llm + optional overrides."""
    async def _convert(messages):
        return list(messages)

    return AgentLoopConfig(
        model=None,  # not used by the fake stream_fn
        convert_to_llm=_convert,
        **kw,
    )


def _count(events: list[dict], etype: str) -> int:
    return sum(1 for e in events if e.get("type") == etype)


# =====================================================================
# 1-4: baseline behavior on the current single-layer loop (must pass)
# =====================================================================

def test_single_turn_no_tool_stops_cleanly():
    """Plain-text turn: stream_fn called once, 1 turn pair, agent_end once."""
    sf = _ScriptedStream([_text("hello")])
    events, sink = _sink()
    _run(run_agent_loop(
        [UserMessage(content="hi")],
        AgentContext(system_prompt="s", messages=[], tools=None),
        _config(),
        sink, None, sf,
    ))
    assert sf.calls == 1
    assert _count(events, "turn_start") == 1
    assert _count(events, "turn_end") == 1
    assert _count(events, "agent_end") == 1


def test_multi_turn_tool_chain_until_plain_text():
    """tool → tool → plain-text: 3 turns, 3 stream calls, 1 agent_end."""
    record: list = []
    tool = _EchoTool(record=record)
    sf = _ScriptedStream([
        _tool_call(),
        _tool_call(),
        _text("done"),
    ])
    events, sink = _sink()
    _run(run_agent_loop(
        [UserMessage(content="go")],
        AgentContext(system_prompt="s", messages=[], tools=[tool]),
        _config(),
        sink, None, sf,
    ))
    assert sf.calls == 3
    assert _count(events, "turn_start") == 3
    assert _count(events, "turn_end") == 3
    assert _count(events, "agent_end") == 1
    assert len(record) == 2  # two tool executions


def test_terminate_stops_inner_loop():
    """A tool with terminate=True sets has_more=False; no follow-up → stop."""
    tool = _EchoTool(terminate=True)
    sf = _ScriptedStream([_tool_call()])
    events, sink = _sink()
    _run(run_agent_loop(
        [UserMessage(content="go")],
        AgentContext(system_prompt="s", messages=[], tools=[tool]),
        _config(),
        sink, None, sf,
    ))
    assert sf.calls == 1  # only one turn; terminate prevented another
    assert _count(events, "turn_end") == 1
    assert _count(events, "agent_end") == 1


def test_error_stop_reason_emits_agent_end():
    """An error turn must still emit turn_end + agent_end (well-formed exit)."""
    sf = _ScriptedStream([_assistant([TextContent(text="")], stop_reason="error")])
    events, sink = _sink()
    _run(run_agent_loop(
        [UserMessage(content="go")],
        AgentContext(system_prompt="s", messages=[], tools=None),
        _config(),
        sink, None, sf,
    ))
    assert _count(events, "turn_end") == 1
    assert _count(events, "agent_end") == 1


# =====================================================================
# 5-7: queue-driven multi-turn (requires AgentLayer queues)
# =====================================================================

def test_steering_queue_injects_before_next_turn():
    """A steering message enqueued during a turn is injected before turn 2."""
    tool = _EchoTool()
    sf = _ScriptedStream([_tool_call(), _text("ok")])
    events, sink = _sink()

    steering = [UserMessage(content="steer!")]
    polled = {"count": 0}

    def get_steering():
        polled["count"] += 1
        # Return the steering message on the *post-turn* poll only.
        if polled["count"] == 2:
            return steering
        return []

    _run(run_agent_loop(
        [UserMessage(content="go")],
        AgentContext(system_prompt="s", messages=[], tools=[tool]),
        _config(get_steering_messages=get_steering),
        sink, None, sf,
    ))
    # The steering message was injected → message_start/message_end for it.
    injected = [e for e in events if e.get("type") == "message_end"
                and getattr(e["message"], "role", None) == "user"
                and _user_text(e["message"]) == "steer!"]
    assert len(injected) == 1
    assert sf.calls == 2  # turn 1 (tool) + turn 2 (after steering, plain text)


def test_follow_up_queue_reenters_inner_loop():
    """Follow-up drains once the inner loop naturally stops, re-entering it."""
    sf = _ScriptedStream([_text("first"), _text("after-followup")])
    events, sink = _sink()

    follow_ups = [UserMessage(content="again")]
    polled = {"count": 0}

    def get_follow_up():
        polled["count"] += 1
        if polled["count"] == 1:
            return follow_ups
        return []

    _run(run_agent_loop(
        [UserMessage(content="go")],
        AgentContext(system_prompt="s", messages=[], tools=None),
        _config(get_follow_up_messages=get_follow_up),
        sink, None, sf,
    ))
    assert sf.calls == 2  # plain-text stop, follow-up re-enters, plain-text stop
    assert _count(events, "turn_start") == 2
    assert _count(events, "agent_end") == 1


def test_queue_mode_all_vs_one_at_a_time():
    """"all" drains everything at once; "one-at-a-time" drains one per poll."""
    # one-at-a-time: 3 messages → 3 follow-up-driven turns.
    sf_oaat = _ScriptedStream([_text("a"), _text("b"), _text("c"), _text("d")])
    events_oaat, sink_oaat = _sink()
    queue_oaat = list(UserMessage(content=m) for m in ["m1", "m2", "m3"])

    def get_follow_up_oaat():
        if queue_oaat:
            return [queue_oaat.pop(0)]  # one at a time
        return []

    _run(run_agent_loop(
        [UserMessage(content="go")],
        AgentContext(system_prompt="s", messages=[], tools=None),
        _config(get_follow_up_messages=get_follow_up_oaat),
        sink_oaat, None, sf_oaat,
    ))
    # initial turn + 3 follow-up turns = 4 stream calls.
    assert sf_oaat.calls == 4

    # all: 3 messages → 1 follow-up-driven turn (all injected together).
    sf_all = _ScriptedStream([_text("a"), _text("b")])
    events_all, sink_all = _sink()
    queue_all = [UserMessage(content=m) for m in ["m1", "m2", "m3"]]
    drained_all = {"done": False}

    def get_follow_up_all():
        if not drained_all["done"]:
            drained_all["done"] = True
            return queue_all[:]  # all at once
        return []

    _run(run_agent_loop(
        [UserMessage(content="go")],
        AgentContext(system_prompt="s", messages=[], tools=None),
        _config(get_follow_up_messages=get_follow_up_all),
        sink_all, None, sf_all,
    ))
    # initial turn + 1 follow-up turn (all 3 injected) = 2 stream calls.
    assert sf_all.calls == 2


# =====================================================================
# 8: Agent-level steer / follow_up end-to-end
# =====================================================================

def test_agent_steer_and_follow_up_end_to_end():
    """steer/follow_up on the Agent reach the loop via drain callbacks."""
    agent = Agent(
        model=_model(),
        system_prompt="s",
        tools=[_EchoTool()],
        stream_fn=_ScriptedStream([_tool_call(), _text("ok")]),
    )
    agent.steer(UserMessage(content="steer!"))
    events: list[dict] = []
    agent.subscribe(lambda e, sig: events.append(e))
    _run(agent.prompt("go"))
    assert any(
        e.get("type") == "message_end"
        and getattr(e["message"], "role", None) == "user"
        and _user_text(e["message"]) == "steer!"
        for e in events
    )


def test_agent_has_queued_messages_reflects_state():
    agent = Agent(model=_model(), system_prompt="s", tools=[], stream_fn=None)
    assert agent.has_queued_messages() is False
    agent.steer(UserMessage(content="x"))
    assert agent.has_queued_messages() is True
    agent.clear_steering_queue()
    assert agent.has_queued_messages() is False
    agent.follow_up(UserMessage(content="y"))
    assert agent.has_queued_messages() is True
    agent.clear_all_queues()
    assert agent.has_queued_messages() is False


# =====================================================================
# 9: agent_end fires exactly once on every termination path
# =====================================================================

def test_agent_end_exactly_once_normal():
    sf = _ScriptedStream([_text("done")])
    events, sink = _sink()
    _run(run_agent_loop([UserMessage(content="go")],
                        AgentContext(system_prompt="s", messages=[], tools=None),
                        _config(), sink, None, sf))
    assert _count(events, "agent_end") == 1


def test_agent_end_exactly_once_on_error():
    sf = _ScriptedStream([_assistant([TextContent(text="")], stop_reason="error")])
    events, sink = _sink()
    _run(run_agent_loop([UserMessage(content="go")],
                        AgentContext(system_prompt="s", messages=[], tools=None),
                        _config(), sink, None, sf))
    assert _count(events, "agent_end") == 1


def test_agent_end_exactly_once_on_terminate():
    tool = _EchoTool(terminate=True)
    sf = _ScriptedStream([_tool_call()])
    events, sink = _sink()
    _run(run_agent_loop([UserMessage(content="go")],
                        AgentContext(system_prompt="s", messages=[], tools=[tool]),
                        _config(), sink, None, sf))
    assert _count(events, "agent_end") == 1


def test_agent_end_exactly_once_on_follow_up_drain():
    sf = _ScriptedStream([_text("a"), _text("b")])
    events, sink = _sink()
    fu = [UserMessage(content="again")]
    polled = {"n": 0}

    def get_fu():
        polled["n"] += 1
        return fu if polled["n"] == 1 else []

    _run(run_agent_loop([UserMessage(content="go")],
                        AgentContext(system_prompt="s", messages=[], tools=None),
                        _config(get_follow_up_messages=get_fu), sink, None, sf))
    assert _count(events, "agent_end") == 1


# ─── helpers ───────────────────────────────────────────────────────────


def _user_text(message) -> str | None:
    """Extract the concatenated text of a user message, else None."""
    if getattr(message, "role", None) != "user":
        return None
    parts = []
    for block in getattr(message, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts) if parts else None
