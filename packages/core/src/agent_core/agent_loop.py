"""Agent loop core.

Dual-loop: an outer loop (follow-up) wraps an inner loop (tool calls +
steering). A shared ``pending`` channel carries messages drained from the
steering queue (polled before the outer loop and after each inner turn)
and the follow-up queue (polled once after the inner loop naturally stops).

  pending = drain steering
  while True:                                  # outer: follow-up
      while has_more or pending:               # inner: tool + steering
          if not first_turn: emit turn_start
          inject pending messages (message_start/end + append to context)
          message = stream_assistant_response(...)
          append message to context + new_messages
          if stop_reason error/aborted:
              emit turn_end, emit agent_end, return       # hard exit both
          tool_calls = extract toolCalls
          if tool_calls:
              execute_tool_calls (sequential or parallel; hook chain)
              append ToolResultMessages to context
              has_more = not batch_terminate
          else:
              has_more = False
          emit turn_end
          pending = drain steering             # post-turn steering poll
      follow_ups = drain follow-up             # inner exited
      if follow_ups:
          pending = follow_ups
          continue                             # re-enter inner
      break
  emit agent_end

Tool execution semantics:
  - batch-level ``tool_execution`` + per-tool ``execution_mode`` override (a
    single sequential tool degrades the whole batch to sequential);
  - ``before_tool_call`` (can block) / ``after_tool_call`` (can rewrite result +
    inject ``terminate``) hooks;
  - ``prepare_arguments`` per-tool shim (runs before schema validation);
  - ``on_update`` partial-result streaming (``tool_execution_update`` events);
  - ``terminate`` flag drives inner-loop stop (all finalized results agree).

"""
from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from typing import Any

from agent_llm import (
    AssistantMessage,
    Context,
    TextContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    validate_tool_arguments,
)

from agent_core.types import (
    AfterToolCallContext,
    AgentContext,
    AgentEvent,
    AgentEventSink,
    AgentLoopConfig,
    AgentTool,
    AgentToolResult,
    BeforeToolCallContext,
    StreamFn,
)


# ─── public entry points ───────────────────────────────────────────────

async def run_agent_loop(
    prompts: list,
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: Any = None,
    stream_fn: StreamFn | None = None,
) -> list:
    """Run the agent loop from a fresh prompt.

    runAgentLoop. Builds new_messages from
    prompts, emits agent_start + turn_start + prompt messages, then runs the
    loop. Returns the new messages produced by this run.
    """
    if stream_fn is None:
        from agent_llm.compat import stream_simple as default_stream_fn
        stream_fn = default_stream_fn  # type: ignore[assignment]

    new_messages = list(prompts)
    current_context = AgentContext(
        system_prompt=context.system_prompt,
        messages=list(context.messages) + list(prompts),
        tools=context.tools,
    )

    await _emit(emit, {"type": "agent_start"})
    await _emit(emit, {"type": "turn_start"})

    # Emit prompt messages.
    for p in prompts:
        await _emit(emit, {"type": "message_start", "message": p})
        await _emit(emit, {"type": "message_end", "message": p})

    await _run_loop(current_context, config, emit, signal, stream_fn, new_messages, first_turn=True)
    return new_messages


async def run_agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: Any = None,
    stream_fn: StreamFn | None = None,
) -> list:
    """Continue the loop from the current transcript.

    runAgentLoopContinue. Guards: messages
    non-empty, last message is user or toolResult.
    """
    if stream_fn is None:
        from agent_llm.compat import stream_simple as default_stream_fn
        stream_fn = default_stream_fn  # type: ignore[assignment]

    if not context.messages:
        raise RuntimeError("No messages to continue from")
    last = context.messages[-1]
    if hasattr(last, "role") and last.role == "assistant":
        raise RuntimeError("Cannot continue from message role: assistant")

    new_messages: list = []
    await _emit(emit, {"type": "agent_start"})
    await _emit(emit, {"type": "turn_start"})

    await _run_loop(context, config, emit, signal, stream_fn, new_messages, first_turn=True)
    return new_messages


# ─── the loop ──────────────────────────────────

async def _drain_queue(getter: Any) -> list:
    """Drain a queue getter (sync or async). ``None`` → ``[]``.

    Both ``get_steering_messages`` and ``get_follow_up_messages`` on
    ``AgentLoopConfig`` may return a list synchronously or awaitably; this
    helper normalizes the result to a fresh list.
    """
    if getter is None:
        return []
    result = getter()
    if hasattr(result, "__await__"):
        result = await result  # type: ignore[assignment]
    return list(result) if result else []


async def _run_loop(
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: Any,
    stream_fn: StreamFn,
    new_messages: list,
    first_turn: bool,
) -> None:
    # Pre-loop steering poll. ``pending`` is the shared
    # channel steering (drained here + after each inner turn) and follow-up
    # (drained after the inner loop stops) both flow through.
    pending = await _drain_queue(config.get_steering_messages)

    while True:  # outer loop: follow-up
        has_more = True
        while has_more or pending:  # inner loop: tool calls + steering
            if not first_turn:
                await _emit(emit, {"type": "turn_start"})
            first_turn = False

            # Inject any pending (steering/follow-up) messages before the next
            # assistant turn. Each goes through the
            # full message_start/message_end event pair and joins the live
            # transcript so the LLM sees it on this turn.
            if pending:
                for msg in pending:
                    await _emit(emit, {"type": "message_start", "message": msg})
                    await _emit(emit, {"type": "message_end", "message": msg})
                    context.messages.append(msg)
                    new_messages.append(msg)
                pending = []

            # (a)(b) Stream the assistant response.
            message = await _stream_assistant_response(
                context, config, emit, signal, stream_fn
            )
            # Append the assistant message to both context and new_messages so the next turn's
            # context includes it (and so convert_to_llm can replay
            # tool_calls before tool results).
            context.messages.append(message)
            new_messages.append(message)

            # (c) Hard exit on stream error/abort.
            # A return (not break) exits BOTH loops; emit agent_end so the
            # event sequence stays well-formed.
            if message.stop_reason in ("error", "aborted"):
                await _emit(emit, {"type": "turn_end", "message": message, "tool_results": []})
                await _emit(emit, {"type": "agent_end", "messages": new_messages})
                return

            # Extract tool calls from the assistant message content.
            tool_calls = [b for b in message.content if isinstance(b, ToolCall)]

            # (d)(e) Execute tools and append results.
            tool_results: list[ToolResultMessage] = []
            if tool_calls:
                tool_results, batch_terminate = await _execute_tool_calls(
                    tool_calls, context, message, config, emit, signal
                )
                for result in tool_results:
                    context.messages.append(result)
                    new_messages.append(result)
                has_more = not batch_terminate
            else:
                has_more = False

            await _emit(emit, {"type": "turn_end", "message": message, "tool_results": tool_results})

            # Post-turn steering poll.
            pending = await _drain_queue(config.get_steering_messages)
        # inner exited: no more tool calls AND no pending steering.

        # Follow-up poll. If anything is queued, route it
        # through ``pending`` and re-enter the inner loop; otherwise stop.
        follow_ups = await _drain_queue(config.get_follow_up_messages)
        if follow_ups:
            pending = follow_ups
            continue
        break

    await _emit(emit, {"type": "agent_end", "messages": new_messages})


# ─── stream + consume ──────────────────────────

async def _stream_assistant_response(
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: Any,
    stream_fn: StreamFn,
) -> AssistantMessage:
    """Convert messages, stream, consume events, return final AssistantMessage.

    The streamed message is the final AssistantMessage returned by the stream.
    """
    # convert_to_llm: AgentMessage[] -> Message[] (the ONLY LLM boundary).
    llm_messages = await config.convert_to_llm(list(context.messages))

    # Build LLM Context (tools converted to agent_llm Tool for the request).
    llm_tools: list[Tool] | None = None
    if context.tools:
        llm_tools = [
            Tool(name=t.name, description=t.description, parameters=t.parameters)
            for t in context.tools
        ]

    llm_context = Context(
        system_prompt=context.system_prompt or None,
        messages=llm_messages,
        tools=llm_tools,
    )

    # Resolve API key.
    options: dict = {}
    if config.get_api_key is not None:
        key = config.get_api_key(config.model.provider)
        if hasattr(key, "__await__"):
            key = await key  # type: ignore[assignment]
        if key:
            options["api_key"] = key
    if config.reasoning:
        options["reasoning"] = config.reasoning

    # Stream.
    event_stream = stream_fn(config.model, llm_context, options or None)

    # Emit message_start on first event, message_update per delta, message_end at terminal.
    #
    # CRITICAL: a `await asyncio.sleep(0)` is needed inside the loop to yield
    # back to the event loop. The HTTP layer (openai SDK) reads many SSE chunks
    # per network read (TCP/SSL buffers ~KB), and the event stream's push →
    # put_nowait → inlined listener → request_render chain is fully synchronous.
    # Without yielding, the TUI's `call_later(MIN_RENDER_INTERVAL)` render timer
    # never gets a chance to fire — the whole batch drains first, collapsing N
    # token renders into 1 ("一坨一坨" instead of smooth streaming).
    #
    # Python asyncio therefore needs an explicit yield between dense chunks.
    #
    # Note: sleep(0) only runs currently-ready callbacks; the throttled
    # `call_later(0.016)` timer needs real elapsed time. That's fine — within a
    # tight token batch (gap≈0) sleep(0) lets other ready work (stdin, queue
    # pumps) run; across batches (gap 50-130ms) the loop naturally yields on
    # the next HTTP read and the timer expires normally.
    started = False
    # Wall-clock throttle for yielding to the event loop during dense bursts.
    #
    # PROBLEM: `await asyncio.sleep(0)` only runs currently-ready callbacks.
    # When the OpenAI SDK delivers many SSE chunks per network read (DeepSeek,
    # GLM-5.2 text phase: dozens of deltas in <1ms), EventStream._drive pushes
    # them all into the asyncio.Queue back-to-back. The consumer's
    # `await queue.get()` finds the next event already ready, so sleep(0)
    # returns immediately without giving the TUI's call_later(16ms) render
    # timer a chance to fire. Result: an entire burst of N deltas is consumed
    # + rendered in one tick, collapsed into a single visible frame — the
    # user-visible "一坨一坨" / "wait then dump all at once" effect.
    #
    # FIX: track elapsed wall-clock time since the last *real* yield. Once
    # >= _BURST_YIELD_INTERVAL_S has passed, sleep long enough for ready
    # timers to fire (a tiny real sleep, not sleep(0)). This caps burst
    # consumption to one render frame per interval (~60 fps) — matching the
    # behavior — while staying cheap for sparse providers (Zhipu air,
    # ~100-200ms between chunks) where each delta already escapes the window.
    _BURST_YIELD_INTERVAL_S = 0.016  # ~60 fps, matches TUI MIN_RENDER_INTERVAL_MS
    last_real_yield = time.monotonic()
    # ── DIAGNOSTIC ─────────────────────────────────────────────────────
    import os as _os
    _DBG_LOG = _os.environ.get("CODING_AGENT_STREAM_DEBUG")
    _dbg_n = 0
    def _dbg(tag, **kw):
        if not _DBG_LOG:
            return
        nonlocal _dbg_n
        _dbg_n += 1
        with open(_DBG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.perf_counter():.6f} LOOP#{_dbg_n} {tag} {kw}\n")
    # ────────────────────────────────────────────────────────────────────
    async for event in event_stream:
        etype = event.get("type")
        _dbg("recv", t=etype)
        if etype == "start" and not started:
            started = True
            await _emit(emit, {"type": "message_start", "message": event["partial"]})
        elif etype in (
            "text_start", "text_delta", "text_end",
            "thinking_start", "thinking_delta", "thinking_end",
            "toolcall_start", "toolcall_delta", "toolcall_end",
        ):
            if not started:
                started = True
                await _emit(emit, {"type": "message_start", "message": event["partial"]})
            await _emit(emit, {
                "type": "message_update",
                "message": event["partial"],
                "event": event,
            })
        # Yield to the event loop. sleep(0) handles the common case (sparse
        # deltas, stdin/queue pumps). Within a dense burst, force a real yield
        # at most once per _BURST_YIELD_INTERVAL_S so the throttled render
        # timer can fire between deltas instead of collapsing the burst.
        await asyncio.sleep(0)
        now = time.monotonic()
        if now - last_real_yield >= _BURST_YIELD_INTERVAL_S:
            # sleep(0) didn't actually give timers a chance (queue still has
            # ready events). A minimal real sleep lets call_later timers fire.
            await asyncio.sleep(0.001)
            last_real_yield = time.monotonic()
        # done/error handled by .result() below.

    final = await event_stream.result()
    if not started:
        # Stream produced no content events (e.g. immediate error); still emit start.
        await _emit(emit, {"type": "message_start", "message": final})
    await _emit(emit, {"type": "message_end", "message": final})
    return final


# ─── tool execution ─────────────────────────────

@dataclass
class _Finalized:
    """A tool call that has reached a terminal outcome (executed, blocked,
    not-found, or aborted). Carries the ``terminate`` flag that
    ``ToolResultMessage`` cannot hold."""
    tool_call: ToolCall
    result: AgentToolResult
    is_error: bool


def _error_result(message: str) -> AgentToolResult:
    """Build a non-terminating error result for a failed tool call."""
    return AgentToolResult(content=[TextContent(text=message)])


async def _execute_tool_calls(
    tool_calls: list[ToolCall],
    context: AgentContext,
    assistant_message: AssistantMessage,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: Any,
) -> "tuple[list[ToolResultMessage], bool]":
    """Dispatch a batch of tool calls.

    Picks sequential vs parallel. A single tool declaring
    ``execution_mode == "sequential"`` degrades the whole batch to sequential
    (the conservative rule). Returns ``(messages, terminate)``.
    """
    mode = config.tool_execution or "sequential"
    tool_map = {t.name: t for t in (context.tools or [])}
    has_sequential_tool = any(
        getattr(tool_map.get(tc.name), "execution_mode", None) == "sequential"
        for tc in tool_calls
    )
    if mode == "parallel" and not has_sequential_tool:
        return await _execute_tool_calls_parallel(
            tool_calls, context, assistant_message, config, emit, signal
        )
    return await _execute_tool_calls_sequential(
        tool_calls, context, assistant_message, config, emit, signal
    )


async def _execute_tool_calls_sequential(
    tool_calls: list[ToolCall],
    context: AgentContext,
    assistant_message: AssistantMessage,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: Any,
) -> "tuple[list[ToolResultMessage], bool]":
    """Run tool calls one at a time.

    Order of events per tool:
      tool_execution_start -> [tool_execution_update...] -> tool_execution_end
      -> message_start/message_end (for the tool-result message)

    A blocked/not-found/aborted call short-circuits to an error result without
    running ``execute``.
    """
    finalized_list: list[_Finalized] = []
    for tc in tool_calls:
        await _emit_start(emit, tc)
        preparation = await _prepare_tool_call(tc, context, assistant_message, config, signal)

        if preparation["kind"] == "immediate":
            finalized = preparation["finalized"]
        else:
            executed = await _execute_prepared_tool_call(
                tc, preparation["tool"], preparation["args"], signal, emit
            )
            finalized = await _finalize_executed_tool_call(
                executed, context, assistant_message, tc, preparation["args"], config, signal
            )

        await _emit_end(emit, finalized)
        finalized_list.append(finalized)

    messages = [_create_tool_result_message(f) for f in finalized_list]
    return messages, _should_terminate_tool_batch(finalized_list)


async def _execute_tool_calls_parallel(
    tool_calls: list[ToolCall],
    context: AgentContext,
    assistant_message: AssistantMessage,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: Any,
) -> "tuple[list[ToolResultMessage], bool]":
    """Run tool calls concurrently.

    Three phases:
      1. prepare each call sequentially (emit ``tool_execution_start`` in
         source order; immediate outcomes finalize right away);
      2. run ``execute`` concurrently via ``asyncio.gather`` — each coroutine
         swallows its own errors so one failure cannot reject the gather;
      3. build tool-result messages in source order (NOT completion order).

    ``tool_execution_end`` is emitted in completion order (each coroutine
    emits its own end once it finishes).
    """
    pending: list[Any] = []  # mix of _Finalized (immediate) and coroutines

    for tc in tool_calls:
        await _emit_start(emit, tc)
        preparation = await _prepare_tool_call(tc, context, assistant_message, config, signal)

        if preparation["kind"] == "immediate":
            finalized = preparation["finalized"]
            await _emit_end(emit, finalized)  # immediate: emit end right away
            pending.append(finalized)
            if _is_aborted(signal):
                break
            continue

        pending.append(
            _run_one_parallel(tc, preparation, context, assistant_message, config, signal, emit)
        )
        if _is_aborted(signal):
            break

    # Concurrent execution: wrap already-finalized items so gather sees a
    # uniform list of awaitables yielding _Finalized.
    gathered = await asyncio.gather(*[_resolve(item) for item in pending])

    messages = [_create_tool_result_message(f) for f in gathered]
    return messages, _should_terminate_tool_batch(gathered)


async def _run_one_parallel(
    tc: ToolCall,
    preparation: dict,
    context: AgentContext,
    assistant_message: AssistantMessage,
    config: AgentLoopConfig,
    signal: Any,
    emit: AgentEventSink,
) -> _Finalized:
    """Execute + finalize a single prepared tool call, then emit its end.

    Used only by the parallel path. Errors from execute/after_tool_call are
    converted to error results here so ``asyncio.gather`` never rejects.
    """
    try:
        executed = await _execute_prepared_tool_call(
            tc, preparation["tool"], preparation["args"], signal, emit
        )
        finalized = await _finalize_executed_tool_call(
            executed, context, assistant_message, tc, preparation["args"], config, signal
        )
    except Exception as e:  # noqa: BLE001 — defensive: must not break gather
        finalized = _Finalized(tc, _error_result(f"Error: {e}"), True)
    await _emit_end(emit, finalized)
    return finalized


async def _prepare_tool_call(
    tc: ToolCall,
    context: AgentContext,
    assistant_message: AssistantMessage,
    config: AgentLoopConfig,
    signal: Any,
) -> dict:
    """Look up a tool, prepare and validate arguments, then run the pre-call hook.

    Returns either ``{"kind": "immediate", "finalized": _Finalized}`` (tool
    not found / blocked / aborted / validation failed) or
    ``{"kind": "prepared", "tool": ..., "args": validated}``.
    """
    tool_map = {t.name: t for t in (context.tools or [])}
    tool = tool_map.get(tc.name)
    if tool is None:
        return {
            "kind": "immediate",
            "finalized": _Finalized(
                tc, _error_result(f'Tool "{tc.name}" not found.'), True
            ),
        }

    try:
        args = _apply_prepare_arguments(tool, tc.arguments)
        validated = _validate_arguments(tool, tc, args)

        if config.before_tool_call is not None:
            before = await _maybe_await(
                config.before_tool_call(
                    BeforeToolCallContext(
                        assistant_message=assistant_message,
                        tool_call=tc,
                        args=validated,
                        context=context,
                    ),
                    signal,
                )
            )
            if _is_aborted(signal):
                return {
                    "kind": "immediate",
                    "finalized": _Finalized(tc, _error_result("Operation aborted"), True),
                }
            if before is not None and before.block:
                return {
                    "kind": "immediate",
                    "finalized": _Finalized(
                        tc,
                        _error_result(before.reason or "Tool execution was blocked"),
                        True,
                    ),
                }

        if _is_aborted(signal):
            return {
                "kind": "immediate",
                "finalized": _Finalized(tc, _error_result("Operation aborted"), True),
            }
        return {"kind": "prepared", "tool": tool, "args": validated}
    except Exception as e:  # noqa: BLE001 — validation / prepare failure
        return {
            "kind": "immediate",
            "finalized": _Finalized(tc, _error_result(str(e)), True),
        }


async def _execute_prepared_tool_call(
    tc: ToolCall,
    tool: AgentTool,
    args: dict,
    signal: Any,
    emit: AgentEventSink,
) -> _Finalized:
    """Invoke ``tool.execute`` with an ``on_update`` callback that streams partial
    results.

    The callback is gated by ``accepting_updates``: once execute settles,
    further calls are ignored. Errors become error results.

    Backward-compat: ``on_update`` is only passed when the tool's ``execute``
    declares a 4th parameter — legacy tools with ``execute(self, id, params,
    signal=None)`` keep working unchanged (Python won't silently drop extra
    positional args the way JS does).
    """
    accepting_updates = True
    update_futures: list[Any] = []

    def on_update(partial_result: AgentToolResult) -> None:
        if not accepting_updates:
            return
        fut = _emit(emit, {
            "type": "tool_execution_update",
            "tool_call_id": tc.id,
            "tool_name": tc.name,
            "args": args,
            "partial_result": partial_result,
        })
        if hasattr(fut, "__await__"):
            update_futures.append(fut)

    try:
        if _accepts_on_update(tool):
            result = await tool.execute(tc.id, args, signal, on_update)
        else:
            result = await tool.execute(tc.id, args, signal)
    except Exception as e:  # noqa: BLE001 — tool failures are error results
        accepting_updates = False
        await _drain(update_futures)
        return _Finalized(tc, _error_result(f"Error: {e}"), True)

    accepting_updates = False
    await _drain(update_futures)
    return _Finalized(tc, result, False)


async def _finalize_executed_tool_call(
    finalized: _Finalized,
    context: AgentContext,
    assistant_message: AssistantMessage,
    tc: ToolCall,
    args: dict,
    config: AgentLoopConfig,
    signal: Any,
) -> _Finalized:
    """Apply ``after_tool_call`` field-by-field overrides.

    Runs BEFORE ``tool_execution_end`` is emitted, so the end event carries the
    post-hook result. Hook exceptions become error results (without breaking
    sibling tools in the parallel path — see ``_run_one_parallel``).
    """
    if config.after_tool_call is None:
        return finalized

    result, is_error = finalized.result, finalized.is_error
    try:
        after = await _maybe_await(
            config.after_tool_call(
                AfterToolCallContext(
                    assistant_message=assistant_message,
                    tool_call=tc,
                    args=args,
                    result=result,
                    is_error=is_error,
                    context=context,
                ),
                signal,
            )
        )
        if after is not None:
            result = AgentToolResult(
                content=after.content if after.content is not None else result.content,
                details=after.details if after.details is not None else result.details,
                terminate=after.terminate if after.terminate is not None else result.terminate,
            )
            if after.is_error is not None:
                is_error = after.is_error
    except Exception as e:  # noqa: BLE001 — hook failures are error results
        result = _error_result(f"Error: {e}")
        is_error = True

    return _Finalized(tc, result, is_error)


def _apply_prepare_arguments(tool: AgentTool, arguments: dict) -> dict:
    """Run the tool's optional ``prepare_arguments`` shim.

    Synchronous, invoked before schema validation. Lets a tool normalize args
    from models that don't strictly obey its schema.
    """
    shim = getattr(tool, "prepare_arguments", None)
    if shim is None:
        return arguments
    prepared = shim(arguments)
    return prepared if prepared is not None else arguments


def _accepts_on_update(tool: AgentTool) -> bool:
    """Whether ``tool.execute`` declares a 4th ``on_update`` parameter.

    Used to keep legacy tools (``execute(self, id, params, signal=None)``)
    working: Python won't ignore an extra positional arg, so we probe the
    signature and only pass the callback when the tool opts in. ``inspect``
    transparently drops ``self`` for bound methods, so we pass ``tool.execute``
    directly (do NOT unwrap ``__func__`` — that would re-include ``self``).
    """
    try:
        sig = inspect.signature(tool.execute)
    except (TypeError, ValueError):
        # Builtins/C-funcs without a signature: be conservative, don't pass it.
        return False
    # Count non-KEYWORD-only parameters excluding *args/**kwargs.
    POSITIONAL = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    positional = [p for p in sig.parameters.values()
                  if p.kind in POSITIONAL]
    # Tool contract: (tool_call_id, params, signal, on_update) → 4 params.
    return len(positional) >= 4


def _validate_arguments(tool: AgentTool, tc: ToolCall, args: dict) -> dict:
    """Validate args against the tool's JSON Schema."""
    from agent_llm.types import Tool as LlmTool
    return validate_tool_arguments(
        LlmTool(name=tool.name, description=tool.description, parameters=tool.parameters),
        # validate_tool_arguments reads .arguments off the ToolCall; rebuild a
        # shallow copy carrying the prepared arguments.
        ToolCall(id=tc.id, name=tc.name, arguments=args),
    )


def _create_tool_result_message(finalized: _Finalized) -> ToolResultMessage:
    """Build a ToolResultMessage.

    Note: ``terminate`` is intentionally NOT carried onto the message (the
    ToolResultMessage schema has no such field); batch termination is decided
    by ``_should_terminate_tool_batch`` reading ``_Finalized.result.terminate``.
    """
    tc = finalized.tool_call
    return ToolResultMessage(
        tool_call_id=tc.id,
        tool_name=tc.name,
        content=finalized.result.content,
        details=finalized.result.details,
        is_error=finalized.is_error,
    )


def _should_terminate_tool_batch(finalized_list: "list[_Finalized]") -> bool:
    """True only when batch is non-empty AND every result has terminate=True.

    Error results (``terminate`` defaults False) therefore prevent batch
    termination — a partially-failed batch never short-circuits the loop.
    """
    return bool(finalized_list) and all(f.result.terminate is True for f in finalized_list)


# ─── tool-execution emit helpers ───────────────────────────────────────

async def _emit_start(emit: AgentEventSink, tc: ToolCall) -> None:
    await _emit(emit, {
        "type": "tool_execution_start",
        "tool_call_id": tc.id,
        "tool_name": tc.name,
        "args": tc.arguments,
    })


async def _emit_end(emit: AgentEventSink, finalized: _Finalized) -> None:
    await _emit(emit, {
        "type": "tool_execution_end",
        "tool_call_id": finalized.tool_call.id,
        "tool_name": finalized.tool_call.name,
        "result": finalized.result,
        "is_error": finalized.is_error,
    })


# ─── small async helpers ───────────────────────────────────────────────

def _is_aborted(signal: Any) -> bool:
    return signal is not None and getattr(signal, "is_set", lambda: False)()


async def _maybe_await(value: Any) -> Any:
    """Await value if awaitable, else return as-is (hook may be sync or async)."""
    if hasattr(value, "__await__"):
        return await value
    return value


async def _drain(futures: list[Any]) -> None:
    """Await any pending emit futures (e.g. tool_execution_update) in order."""
    for fut in futures:
        if hasattr(fut, "__await__"):
            await fut


async def _resolve(item: Any) -> _Finalized:
    """Used by the parallel path: pass through an already-finalized result, or
    await the pending coroutine."""
    if isinstance(item, _Finalized):
        return item
    return await item


# ─── emit helper ───────────────────────────────────────────────────────

async def _emit(emit: AgentEventSink, event: AgentEvent) -> None:
    """Await the sink, tolerating sync callbacks."""
    result = emit(event)
    if hasattr(result, "__await__"):
        await result
