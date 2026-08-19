"""Agent class.

Stateful wrapper around the low-level agent loop. Owns the transcript,
emits lifecycle events, executes tools, and exposes prompt/abort APIs.

Tool-execution config (``tool_execution`` batch strategy,
``before_tool_call``/``after_tool_call`` hooks) is threaded through to the
loop via ``AgentLoopConfig``. Steering/follow-up queues are
owned here as :class:`PendingMessageQueue` instances and surfaced via
:meth:`steer` / :meth:`follow_up`; the loop drains them through
``get_steering_messages`` / ``get_follow_up_messages`` callbacks.

The public API covers subscribe, prompt, abort, wait-for-idle, reset,
steering, follow-up messages, and queue clearing.

"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from agent_llm import AssistantMessage, Message, Model, TextContent, Usage, UsageCost

from agent_core.agent_loop import run_agent_loop
from agent_core.types import (
    AgentContext,
    AgentEvent,
    AgentLoopConfig,
    AgentState,
    AgentTool,
    QueueMode,
    StreamFn,
)


class PendingMessageQueue:
    """FIFO of pending AgentMessages with two drain modes.

    ``mode="all"`` drains every queued message in one ``drain()`` call;
    ``mode="one-at-a-time"`` drains only the oldest, leaving the rest for a
    later drain. Both steering and follow-up queues default to
    ``"one-at-a-time"``.
    """

    def __init__(self, mode: QueueMode = "one-at-a-time") -> None:
        self.mode = mode
        self._messages: list = []

    def enqueue(self, message) -> None:
        self._messages.append(message)

    def has_items(self) -> bool:
        return bool(self._messages)

    def drain(self) -> list:
        if self.mode == "all":
            drained = self._messages[:]
            self._messages = []
            return drained
        if not self._messages:
            return []
        first = self._messages[0]
        self._messages = self._messages[1:]
        return [first]

    def clear(self) -> None:
        self._messages = []


def default_convert_to_llm(messages: list) -> list[Message]:
    """Filter transcript to LLM-facing roles.

    Passes through user/assistant/toolResult; drops any future custom messages.
    """
    from agent_core.session.messages import repair_incomplete_tool_calls

    return repair_incomplete_tool_calls([
        m for m in messages
        if getattr(m, "role", None) in ("user", "assistant", "toolResult")
    ])


_EMPTY_USAGE = Usage(cost=UsageCost())


class Agent:
    """Stateful agent wrapper around the agent loop.

    Owns the current transcript, emits lifecycle events, and runs the loop.
    Callers subscribe to events and call :meth:`prompt`.

    """

    def __init__(
        self,
        *,
        model: Model,
        system_prompt: str = "",
        tools: list[AgentTool] | None = None,
        stream_fn: StreamFn,
        get_api_key: "Callable[[str], Awaitable[str | None] | str | None] | None" = None,
        convert_to_llm: Callable[[list], "Awaitable[list[Message]] | list[Message]"] = default_convert_to_llm,
        reasoning: Any = None,
        session_manager: Any = None,
        tool_execution: Any = None,
        before_tool_call: Any = None,
        after_tool_call: Any = None,
        steering_mode: QueueMode = "one-at-a-time",
        follow_up_mode: QueueMode = "one-at-a-time",
    ) -> None:
        self._state = AgentState(
            system_prompt=system_prompt,
            model=model,
            tools=list(tools) if tools else [],
        )
        self._listeners: list[Callable[[AgentEvent, asyncio.Event], "Awaitable[None] | None"]] = []
        self._active_run: _ActiveRun | None = None
        self.convert_to_llm = convert_to_llm
        self.stream_fn = stream_fn
        self.get_api_key = get_api_key
        self.reasoning = reasoning
        # Optional SessionManager: when set, messages are persisted as they land
        # in the transcript (message_end for user/assistant, turn_end for tool
        # results). Kept optional so the Agent stays usable without persistence.
        self.session_manager = session_manager
        # Tool-execution config threaded into AgentLoopConfig.
        self.tool_execution = tool_execution
        self.before_tool_call = before_tool_call
        self.after_tool_call = after_tool_call
        # Steering/follow-up queues. Drained by the
        # loop via get_steering_messages / get_follow_up_messages callbacks.
        self._steering_queue = PendingMessageQueue(steering_mode)
        self._follow_up_queue = PendingMessageQueue(follow_up_mode)

    # ── subscription ───────────────────────────────────────────────────

    def subscribe(
        self,
        listener: Callable[[AgentEvent, asyncio.Event], "Awaitable[None] | None"],
    ) -> Callable[[], None]:
        """Subscribe to agent lifecycle events. Returns an unsubscribe fn.

        Listeners receive the event and the active run's abort signal (as an
        asyncio.Event whose ``is_set()`` indicates abort).
        """
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def signal(self) -> asyncio.Event | None:
        """Active run's abort signal, if a run is active."""
        return self._active_run.abort_event if self._active_run else None

    # ── queues (steering / follow-up) ─────────────────────────────────

    def steer(self, message) -> None:
        """Enqueue a steering message.

        Steering messages are injected before the next assistant turn —
        i.e. they cut in line on the current run while it is still looping.
        """
        self._steering_queue.enqueue(message)

    def follow_up(self, message) -> None:
        """Enqueue a follow-up message.

        Follow-up messages are injected only after the inner loop naturally
        stops (no more tool calls, no steering pending), starting a new
        inner run.
        """
        self._follow_up_queue.enqueue(message)

    def clear_steering_queue(self) -> None:
        self._steering_queue.clear()

    def clear_follow_up_queue(self) -> None:
        self._follow_up_queue.clear()

    def clear_all_queues(self) -> None:
        """Clear both steering and follow-up queues."""
        self._steering_queue.clear()
        self._follow_up_queue.clear()

    def has_queued_messages(self) -> bool:
        """True if either queue has anything pending."""
        return self._steering_queue.has_items() or self._follow_up_queue.has_items()

    # ── run control ────────────────────────────────────────────────────

    async def prompt(self, message: Any) -> None:
        """Start a new prompt. Raises if already processing.

        ``message`` can be a string, a single AgentMessage, or a list.
        """
        if self._active_run is not None:
            raise RuntimeError(
                "Agent is already processing a prompt. Wait for completion before prompting again."
            )

        prompts = self._normalize_prompt_input(message)
        await self._run_with_lifecycle(
            lambda signal: run_agent_loop(
                prompts,
                self._create_context_snapshot(),
                self._create_loop_config(),
                lambda e: self._process_events(e),
                signal,
                self.stream_fn,
            )
        )

    async def abort(self) -> None:
        """Abort the current run, if one is active."""
        if self._active_run is not None:
            self._active_run.abort_event.set()

    async def wait_for_idle(self) -> None:
        """Resolve when the current run finishes (resolves immediately if idle)."""
        if self._active_run is not None:
            await self._active_run.done.wait()

    def reset(self) -> None:
        """Clear transcript, runtime state. Does not abort a running loop.

        If a session_manager is attached, ``detach_session`` first so the
        session file is left intact (callers start a fresh session separately).
        """
        self._state.messages = []
        self._state.is_streaming = False
        self._state.streaming_message = None
        self._state.pending_tool_calls = set()
        self._state.error_message = None

    def load_messages(self, messages: list) -> None:
        """Replace the transcript with the given messages (for session resume).

        Rebuilds ``_state.messages`` from a persisted session's context (which
        may start with a CompactionSummaryMessage). Does not persist again —
        the caller is responsible for reattaching/creating a session if needed.
        """
        self._state.messages = list(messages)

    def attach_session(self, session_manager: Any) -> None:
        """Attach a SessionManager so subsequent messages are persisted."""
        self.session_manager = session_manager

    def detach_session(self) -> None:
        """Detach the current SessionManager (does not touch the file)."""
        self.session_manager = None

    # ── internals ──────────────────────────────────────────────────────

    def _normalize_prompt_input(self, message: Any) -> list:
        if isinstance(message, list):
            return message
        if isinstance(message, str):
            from agent_llm import UserMessage
            return [UserMessage(content=message)]
        return [message]

    def _create_context_snapshot(self) -> AgentContext:
        return AgentContext(
            system_prompt=self._state.system_prompt,
            messages=list(self._state.messages),
            tools=list(self._state.tools),
        )

    def _create_loop_config(self) -> AgentLoopConfig:
        async_convert = _make_async_convert(self.convert_to_llm)
        return AgentLoopConfig(
            model=self._state.model,
            convert_to_llm=async_convert,
            get_api_key=self.get_api_key,
            reasoning=self.reasoning,
            tool_execution=self.tool_execution,
            before_tool_call=self.before_tool_call,
            after_tool_call=self.after_tool_call,
            # Queue drain callbacks. The loop polls these
            # to inject steering (pre-outer + post-turn) and follow-up
            # (after the inner loop stops) messages.
            get_steering_messages=self._steering_queue.drain,
            get_follow_up_messages=self._follow_up_queue.drain,
        )

    async def _run_with_lifecycle(self, executor: Callable[[asyncio.Event], Awaitable[None]]) -> None:
        """Run ``executor`` within a lifecycle: set up state, settle on exit.

        runWithLifecycle.
        """
        abort_event = asyncio.Event()
        done = asyncio.Event()
        self._active_run = _ActiveRun(abort_event=abort_event, done=done)

        self._state.is_streaming = True
        self._state.streaming_message = None
        self._state.error_message = None

        try:
            await executor(abort_event)
        except Exception as e:  # noqa: BLE001
            await self._handle_run_failure(e, abort_event.is_set())
        finally:
            self._finish_run()

    async def _handle_run_failure(self, error: Any, aborted: bool) -> None:
        """Emit a failure message sequence."""
        failure = AssistantMessage(
            content=[TextContent(text="")],
            api=self._state.model.api,
            provider=self._state.model.provider,
            model=self._state.model.id,
            usage=_EMPTY_USAGE,
            stop_reason="aborted" if aborted else "error",
            error_message=str(error) if isinstance(error, Exception) else str(error),
        )
        await self._process_events({"type": "message_start", "message": failure})
        await self._process_events({"type": "message_end", "message": failure})
        await self._process_events({"type": "turn_end", "message": failure, "tool_results": []})
        await self._process_events({"type": "agent_end", "messages": [failure]})

    def _finish_run(self) -> None:
        self._state.is_streaming = False
        self._state.streaming_message = None
        self._state.pending_tool_calls = set()
        if self._active_run is not None:
            self._active_run.done.set()
        self._active_run = None

    async def _process_events(self, event: AgentEvent) -> None:
        """Reduce internal state for a loop event, then await listeners.

        processEvents.
        """
        etype = event.get("type")
        if etype == "message_start":
            self._state.streaming_message = event["message"]
        elif etype == "message_update":
            self._state.streaming_message = event["message"]
        elif etype == "message_end":
            self._state.streaming_message = None
            self._state.messages.append(event["message"])
            # Persist user + assistant messages to the session (if attached).
            if self.session_manager is not None:
                msg = event["message"]
                try:
                    self.session_manager.append_message(msg)
                except Exception:
                    # Persistence must never break the conversation loop.
                    pass
        elif etype == "tool_execution_start":
            self._state.pending_tool_calls.add(event["tool_call_id"])
        elif etype == "tool_execution_end":
            self._state.pending_tool_calls.discard(event["tool_call_id"])
        elif etype == "turn_end":
            msg = event["message"]
            if hasattr(msg, "role") and msg.role == "assistant" and msg.error_message:
                self._state.error_message = msg.error_message
            # Persist tool results into the agent's own transcript.
            #
            # The loop keeps tool results only in its *local* context copy; if we
            # don't mirror them into state.messages here, the next prompt()'s
            # context snapshot will contain an assistant message with tool_calls
            # but no following toolResult messages — which providers like
            # DeepSeek reject with HTTP 400 ("insufficient tool messages
            # following tool_calls message"). Order is correct: message_end has
            # already appended the assistant message, so tool results land right
            # after their corresponding tool_calls.
            for tr in event.get("tool_results", []):
                self._state.messages.append(tr)
                if self.session_manager is not None:
                    try:
                        self.session_manager.append_message(tr)
                    except Exception:
                        pass
        elif etype == "agent_end":
            self._state.streaming_message = None

        # Forward to listeners.
        signal = self._active_run.abort_event if self._active_run else asyncio.Event()
        for listener in list(self._listeners):
            try:
                result = listener(event, signal)
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                # Listener errors don't break the loop.
                pass


class _ActiveRun:
    def __init__(self, *, abort_event: asyncio.Event, done: asyncio.Event) -> None:
        self.abort_event = abort_event
        self.done = done


def _make_async_convert(
    convert: Callable[[list], "Awaitable[list[Message]] | list[Message]"],
) -> Callable[[list], Awaitable[list[Message]]]:
    """Wrap a possibly-sync convert_to_llm into an async one."""
    async def _async(messages: list) -> list[Message]:
        result = convert(messages)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[assignment]
        return result  # type: ignore[return-value]
    return _async
