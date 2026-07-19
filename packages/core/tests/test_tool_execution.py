"""Tests for the tool-execution layer of the agent loop.

Covers:
  - sequential vs parallel dispatch (and per-tool ``execution_mode`` degradation)
  - the ``terminate`` batch-stop flag (all-must-agree; error results never terminate)
  - ``before_tool_call`` / ``after_tool_call`` hooks (block, rewrite result, inject terminate)
  - the ``prepare_arguments`` per-tool shim
  - ``on_update`` partial-result streaming (``tool_execution_update`` events)
  - single-tool failure does not break sibling tools
  - backward compat: legacy tools without the new optional members

These drive the internal ``_execute_tool_calls*`` functions directly (no
stream_fn / LLM needed) and collect events via a synchronous list sink.
"""
from __future__ import annotations

import asyncio
import time

from agent_llm import AssistantMessage, TextContent, ToolCall

from agent_core.agent_loop import (
    _execute_tool_calls,
)
from agent_core.types import (
    AfterToolCallContext,
    AfterToolCallResult,
    AgentContext,
    AgentLoopConfig,
    AgentToolResult,
    BeforeToolCallContext,
    BeforeToolCallResult,
)


def _run(coro):
    return asyncio.run(coro)


def _msg(content=None):
    """An assistant message that owns the tool calls in ``content``."""
    return AssistantMessage(
        content=content or [],
        provider="deepseek", model="m", stop_reason="tool_use",
    )


def _tc(name: str, args: dict | None = None, id_: str | None = None) -> ToolCall:
    return ToolCall(id=id_ or f"{name}-1", name=name, arguments=args or {})


def _ctx(tools) -> AgentContext:
    return AgentContext(system_prompt="sys", messages=[], tools=tools)


def _sink():
    """A sync event sink that appends to a list. Returns ``(events, sink)``."""
    events: list[dict] = []

    def sink(event):
        events.append(event)

    return events, sink


# ─── fake tool ─────────────────────────────────────────────────────────

class FakeTool:
    """A configurable tool for testing the hook chain.

    Each knob controls one optional tool hook so we can exercise them
    independently without touching the real tools.
    """

    def __init__(
        self,
        *,
        name: str = "fake",
        label: str = "fake",
        description: str = "d",
        parameters: dict | None = None,
        delay: float = 0.0,
        terminate: bool = False,
        raise_on_execute: bool = False,
        updates: list[str] | None = None,
        execution_mode: str | None = None,
        prepare_arguments=None,
        record=None,
    ) -> None:
        self.name = name
        self.label = label
        self.description = description
        self.parameters = parameters or {"type": "object", "properties": {}}
        self._delay = delay
        self._terminate = terminate
        self._raise = raise_on_execute
        self._updates = updates or []
        self.execution_mode = execution_mode
        self.prepare_arguments = prepare_arguments
        self._record = record  # shared dict to log calls into

    async def execute(self, tool_call_id, params, signal=None, on_update=None):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._record is not None:
            self._record.setdefault("calls", []).append({"id": tool_call_id, "params": params})
        for partial_text in self._updates:
            if on_update is not None:
                on_update(AgentToolResult(content=[TextContent(text=partial_text)]))
        if self._raise:
            raise RuntimeError("boom")
        return AgentToolResult(
            content=[TextContent(text=f"ok:{params}")],
            terminate=self._terminate,
        )


def _config(**kw) -> AgentLoopConfig:
    """Build an AgentLoopConfig with only the tool-execution fields populated."""
    async def _convert(messages):
        return messages
    return AgentLoopConfig(
        model=None,  # type: ignore[arg-type]  # not used by _execute_tool_calls
        convert_to_llm=_convert,
        **kw,
    )


# =====================================================================
# 1. Sequential vs parallel dispatch
# =====================================================================

def test_sequential_default_runs_tools_in_order():
    tool = FakeTool(name="t")
    events, sink = _sink()
    tcs = [_tc("t", {"n": 1}, "a"), _tc("t", {"n": 2}, "b")]

    messages, terminate = _run(_execute_tool_calls(
        tcs, _ctx([tool]), _msg(tcs), _config(), sink, None,
    ))

    starts = [e for e in events if e["type"] == "tool_execution_start"]
    ends = [e for e in events if e["type"] == "tool_execution_end"]
    # start emitted in source order; end emitted in completion order (== source
    # order here since everything is sequential).
    assert [s["tool_call_id"] for s in starts] == ["a", "b"]
    assert [e["tool_call_id"] for e in ends] == ["a", "b"]
    assert len(messages) == 2
    assert terminate is False


def test_parallel_runs_concurrently_not_sum_of_delays():
    # Three tools each sleeping 0.15s. Sequential would be ~0.45s; parallel
    # should be ~0.15s. Use a generous upper bound to stay CI-stable.
    tools = [FakeTool(name=f"t{i}", delay=0.15) for i in range(3)]
    tcs = [_tc(f"t{i}", {}, id_=f"c{i}") for i in range(3)]
    events, sink = _sink()

    start = time.perf_counter()
    _run(_execute_tool_calls(
        tcs, _ctx(tools), _msg(tcs), _config(tool_execution="parallel"), sink, None,
    ))
    elapsed = time.perf_counter() - start

    assert elapsed < 0.40, f"parallel took {elapsed:.3f}s, expected ~0.15s"
    # All three starts fired.
    starts = [e for e in events if e["type"] == "tool_execution_start"]
    assert len(starts) == 3


def test_per_tool_execution_mode_degrades_batch_to_sequential():
    # One tool declares execution_mode="sequential" → whole batch must run
    # sequentially even though config says parallel. Verify by ordering: with
    # two 0.15s tools run sequentially, total >= 0.30s (parallel would be ~0.15).
    seq_tool = FakeTool(name="seq", delay=0.15, execution_mode="sequential")
    par_tool = FakeTool(name="par", delay=0.15)
    tcs = [_tc("seq", {}, "c1"), _tc("par", {}, "c2")]
    events, sink = _sink()

    start = time.perf_counter()
    _run(_execute_tool_calls(
        tcs, _ctx([seq_tool, par_tool]), _msg(tcs),
        _config(tool_execution="parallel"), sink, None,
    ))
    elapsed = time.perf_counter() - start

    assert elapsed >= 0.28, f"expected sequential (~0.30s), got {elapsed:.3f}s"


# =====================================================================
# 2. terminate flag (all-must-agree; errors never terminate)
# =====================================================================

def test_terminate_all_agree_stops_batch():
    tool = FakeTool(name="t", terminate=True)
    tcs = [_tc("t", {}, "a"), _tc("t", {}, "b")]
    events, sink = _sink()

    _, terminate = _run(_execute_tool_calls(
        tcs, _ctx([tool]), _msg(tcs), _config(), sink, None,
    ))
    assert terminate is True


def test_terminate_one_disagree_does_not_stop():
    a = FakeTool(name="ta", terminate=True)
    b = FakeTool(name="tb", terminate=False)
    tcs = [_tc("ta", {}, "x"), _tc("tb", {}, "y")]
    events, sink = _sink()

    _, terminate = _run(_execute_tool_calls(
        tcs, _ctx([a, b]), _msg(tcs), _config(), sink, None,
    ))
    assert terminate is False


def test_terminate_error_result_never_terminates():
    ok = FakeTool(name="ok", terminate=True)
    bad = FakeTool(name="bad", terminate=True, raise_on_execute=True)
    tcs = [_tc("ok", {}, "1"), _tc("bad", {}, "2")]
    events, sink = _sink()

    messages, terminate = _run(_execute_tool_calls(
        tcs, _ctx([ok, bad]), _msg(tcs), _config(), sink, None,
    ))
    # bad raised → error result with terminate=False → batch cannot terminate.
    assert terminate is False
    assert any(m.is_error for m in messages)


# =====================================================================
# 3. before_tool_call hook
# =====================================================================

def test_before_tool_call_blocks_emits_error_result():
    tool = FakeTool(name="t", record={})
    tcs = [_tc("t", {"x": 1}, "a")]
    events, sink = _sink()
    seen = {}

    def before(ctx: BeforeToolCallContext, signal):
        seen["args"] = ctx.args
        return BeforeToolCallResult(block=True, reason="nope")

    messages, terminate = _run(_execute_tool_calls(
        tcs, _ctx([tool]), _msg(tcs),
        _config(before_tool_call=before), sink, None,
    ))

    # Hook saw validated args.
    assert seen["args"] == {"x": 1}
    # Tool.execute was never called.
    assert tool._record is None or "calls" not in tool._record
    # An error result was emitted and recorded.
    end = next(e for e in events if e["type"] == "tool_execution_end")
    assert end["is_error"] is True
    assert "nope" in end["result"].content[0].text
    assert messages[0].is_error


def test_before_tool_call_does_not_mutate_args():
    record = {}
    tool = FakeTool(name="t", record=record)
    tcs = [_tc("t", {"orig": 1}, "a")]
    events, sink = _sink()

    def before(ctx: BeforeToolCallContext, signal):
        # Try (and fail) to mutate — execute must still receive the original.
        return None

    _run(_execute_tool_calls(
        tcs, _ctx([tool]), _msg(tcs),
        _config(before_tool_call=before), sink, None,
    ))
    assert record["calls"][0]["params"] == {"orig": 1}


def test_before_tool_call_async_hook_supported():
    tool = FakeTool(name="t", record={})
    tcs = [_tc("t", {}, "a")]
    events, sink = _sink()

    async def before(ctx: BeforeToolCallContext, signal):
        return None  # allow

    _run(_execute_tool_calls(
        tcs, _ctx([tool]), _msg(tcs),
        _config(before_tool_call=before), sink, None,
    ))
    assert tool._record["calls"]  # execute ran


# =====================================================================
# 4. after_tool_call hook
# =====================================================================

def test_after_tool_call_rewrites_result_and_terminate():
    tool = FakeTool(name="t", terminate=False)
    tcs = [_tc("t", {}, "a")]
    events, sink = _sink()

    def after(ctx: AfterToolCallContext, signal):
        return AfterToolCallResult(
            content=[TextContent(text="rewritten")],
            is_error=True,
            terminate=True,
        )

    messages, terminate = _run(_execute_tool_calls(
        tcs, _ctx([tool]), _msg(tcs),
        _config(after_tool_call=after), sink, None,
    ))

    # end event carries the rewritten content + is_error + terminate.
    end = next(e for e in events if e["type"] == "tool_execution_end")
    assert end["result"].content[0].text == "rewritten"
    assert end["is_error"] is True
    assert messages[0].is_error
    # terminate injected by the hook drives batch termination.
    assert terminate is True


def test_after_tool_call_partial_override_keeps_other_fields():
    tool = FakeTool(name="t", terminate=False)
    tcs = [_tc("t", {}, "a")]
    events, sink = _sink()

    def after(ctx: AfterToolCallContext, signal):
        # Only flip terminate; content/is_error must fall through unchanged.
        return AfterToolCallResult(terminate=True)

    messages, terminate = _run(_execute_tool_calls(
        tcs, _ctx([tool]), _msg(tcs),
        _config(after_tool_call=after), sink, None,
    ))
    end = next(e for e in events if e["type"] == "tool_execution_end")
    assert end["is_error"] is False  # original
    assert "ok:" in end["result"].content[0].text  # original content kept
    assert terminate is True


def test_after_tool_call_exception_becomes_error_result():
    ok = FakeTool(name="ok")
    bad_hook_tool = FakeTool(name="bad")
    tcs = [_tc("ok", {}, "1"), _tc("bad", {}, "2")]
    events, sink = _sink()

    def after(ctx: AfterToolCallContext, signal):
        if ctx.tool_call.name == "bad":
            raise RuntimeError("hook crashed")
        return None

    messages, terminate = _run(_execute_tool_calls(
        tcs, _ctx([ok, bad_hook_tool]), _msg(tcs),
        _config(after_tool_call=after), sink, None,
    ))
    by_name = {m.tool_name: m for m in messages}
    assert by_name["bad"].is_error
    assert "hook crashed" in by_name["bad"].content[0].text
    assert by_name["ok"].is_error is False  # sibling unaffected
    # error result has terminate=False → batch cannot terminate.
    assert terminate is False


# =====================================================================
# 5. prepare_arguments shim
# =====================================================================

def test_prepare_arguments_runs_before_validation_and_rewrites_args():
    record = {}

    def rename_path(args):
        # Legacy models send ``path``; tool wants ``file_path``.
        new = dict(args)
        if "path" in new:
            new["file_path"] = new.pop("path")
        return new

    tool = FakeTool(
        name="t",
        parameters={
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
            "additionalProperties": False,
        },
        prepare_arguments=rename_path,
        record=record,
    )
    tcs = [_tc("t", {"path": "/x"}, "a")]
    events, sink = _sink()

    _run(_execute_tool_calls(
        tcs, _ctx([tool]), _msg(tcs), _config(), sink, None,
    ))
    # execute received the rewritten args.
    assert record["calls"][0]["params"] == {"file_path": "/x"}


def test_prepare_arguments_absent_passes_through():
    record = {}
    tool = FakeTool(name="t", record=record)  # no prepare_arguments
    tcs = [_tc("t", {"k": 1}, "a")]
    events, sink = _sink()

    _run(_execute_tool_calls(
        tcs, _ctx([tool]), _msg(tcs), _config(), sink, None,
    ))
    assert record["calls"][0]["params"] == {"k": 1}


# =====================================================================
# 6. on_update partial-result streaming
# =====================================================================

def test_on_update_emits_tool_execution_update_events():
    tool = FakeTool(name="t", updates=["step1", "step2"])
    tcs = [_tc("t", {}, "a")]
    events, sink = _sink()

    _run(_execute_tool_calls(
        tcs, _ctx([tool]), _msg(tcs), _config(), sink, None,
    ))
    updates = [e for e in events if e["type"] == "tool_execution_update"]
    assert [u["partial_result"].content[0].text for u in updates] == ["step1", "step2"]
    # update events carry the tool-call id + args.
    assert updates[0]["tool_call_id"] == "a"


def test_on_update_works_in_parallel_mode():
    tool = FakeTool(name="t", updates=["p1", "p2"])
    tcs = [_tc("t", {}, "a")]
    events, sink = _sink()

    _run(_execute_tool_calls(
        tcs, _ctx([tool]), _msg(tcs),
        _config(tool_execution="parallel"), sink, None,
    ))
    updates = [e for e in events if e["type"] == "tool_execution_update"]
    assert len(updates) == 2


# =====================================================================
# 7. Single-tool failure isolation
# =====================================================================

def test_one_tool_failure_does_not_break_others_in_parallel():
    ok1 = FakeTool(name="ok1", record={})
    bad = FakeTool(name="bad", raise_on_execute=True)
    ok3 = FakeTool(name="ok3", record={})
    tcs = [_tc("ok1", {}, "1"), _tc("bad", {}, "2"), _tc("ok3", {}, "3")]
    events, sink = _sink()

    messages, terminate = _run(_execute_tool_calls(
        tcs, _ctx([ok1, bad, ok3]), _msg(tcs),
        _config(tool_execution="parallel"), sink, None,
    ))
    by_name = {m.tool_name: m for m in messages}
    assert by_name["ok1"].is_error is False
    assert by_name["bad"].is_error is True
    assert by_name["ok3"].is_error is False
    assert "boom" in by_name["bad"].content[0].text
    # error result present → batch cannot terminate even if others did.
    assert terminate is False


def test_tool_not_found_yields_error_result():
    tcs = [_tc("ghost", {}, "1")]
    events, sink = _sink()

    messages, terminate = _run(_execute_tool_calls(
        tcs, _ctx([]), _msg(tcs), _config(), sink, None,
    ))
    assert messages[0].is_error
    assert "not found" in messages[0].content[0].text
    assert terminate is False  # empty-by-effect error never terminates


# =====================================================================
# 8. Backward compat: a real legacy tool (ReadTool-style) works unchanged
# =====================================================================

def test_legacy_tool_without_optional_members_runs():
    """A tool that defines only name/label/description/parameters/execute
    (no prepare_arguments / execution_mode / on_update param) must still work
    and see no tool_execution_update events."""

    class LegacyTool:
        name = "legacy"
        label = "legacy"
        description = "d"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, tool_call_id, params, signal=None):
            # Old 3-arg signature; loop passes on_update as 4th — ignored.
            return AgentToolResult(content=[TextContent(text="legacy-ok")])

    tool = LegacyTool()
    tcs = [_tc("legacy", {}, "1")]
    events, sink = _sink()

    messages, terminate = _run(_execute_tool_calls(
        tcs, _ctx([tool]), _msg(tcs), _config(), sink, None,
    ))
    assert messages[0].is_error is False
    assert messages[0].content[0].text == "legacy-ok"
    assert terminate is False
    assert not [e for e in events if e["type"] == "tool_execution_update"]
