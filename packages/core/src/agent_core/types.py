"""agent_core types: tool protocol, events, state.

The agent loop supports: parallel/sequential tool execution (batch-level
``tool_execution`` + per-tool ``execution_mode`` override), the
``terminate`` batch-stop flag, ``before_tool_call``/``after_tool_call`` hooks,
the per-tool ``prepare_arguments`` shim, partial-result streaming via
``tool_execution_update``, and steering/follow-up queues (QueueMode) that
drive the outer/inner dual-loop.

"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol, TypedDict, Union, runtime_checkable

from agent_llm import (
    AssistantMessage,
    AssistantMessageEvent,
    AssistantMessageEventStream,
    Context,
    Message,
    Model,
    SimpleStreamOptions,
    ThinkingLevel,
    ToolCall,
)


# ─── Tool protocol ───────────────────

@dataclass
class AgentToolResult:
    """Final or partial result produced by a tool.

    ``content`` is what's sent back to the model. ``details`` is structured
    metadata for logs/UI (not sent verbatim). ``terminate`` hints the agent
    should stop after the current tool batch.
    """
    content: list  # list[TextContent | ImageContent]
    details: Any = None
    terminate: bool = False


#: Tool batch execution strategy. Batch-level default lives on
#: ``AgentLoopConfig.tool_execution``; any tool declaring
#: ``execution_mode == "sequential"`` degrades the whole batch to sequential
#: so a batch never mixes scheduling modes.
ToolExecutionMode = Literal["sequential", "parallel"]

#: Queue drain policy. "all" drains every queued message in one
#: drain call; "one-at-a-time" drains only the oldest, leaving the rest for a
#: later drain. Both steering and follow-up queues default to "one-at-a-time".
QueueMode = Literal["all", "one-at-a-time"]

#: ``on_update`` callback shape. Scoped to a single ``execute()``;
#: calls made after execute settles are silently ignored by the loop.
AgentToolUpdateCallback = Callable[[AgentToolResult], None]


@runtime_checkable
class AgentTool(Protocol):
    """Tool definition used by the agent runtime.

    ``parameters`` is a JSON Schema dict. ``execute`` raises on failure (the
    loop catches and surfaces it as an error tool result); encode normal
    empty/edge results in ``content`` instead.

    Optional members are consulted via ``getattr`` so existing tools that do
    not declare them keep working unchanged:
      - ``prepare_arguments``: sync shim invoked before schema validation; may
        rewrite args (e.g. rename legacy field names).
      - ``execution_mode``: per-tool override; ``"sequential"`` forces the
        whole batch sequential.
      - ``execute(..., on_update=)``: optional 4th param; the loop passes an
        ``AgentToolUpdateCallback`` the tool may call to stream partial
        results (emitted as ``tool_execution_update`` events).
    """

    name: str
    label: str
    description: str
    parameters: dict  # JSON Schema

    prepare_arguments: Callable[[dict], dict]
    execution_mode: ToolExecutionMode

    async def execute(
        self,
        tool_call_id: str,
        params: dict,
        signal: Any = None,
        on_update: Any = None,
    ) -> AgentToolResult: ...


# ─── Stream function ────────────────────────────

#: Stream function used by the agent loop. ``Models.stream_simple`` satisfies
#: this shape. Must NOT throw for request/model/runtime failures — encode them
#: in the returned stream via error events.
StreamFn = Callable[
    [Model, Context, "SimpleStreamOptions | None"],
    AssistantMessageEventStream,
]


# ─── Context / config ───────

#: AgentMessage: LLM messages plus (future) custom app messages.
#: For now, it's just the LLM Message union.
AgentMessage = Message


@dataclass
class AgentContext:
    """Context snapshot passed into the agent loop."""
    system_prompt: str = ""
    messages: list = field(default_factory=list)  # list[AgentMessage]
    tools: list | None = None  # list[AgentTool]


# ─── Tool-call hook context/result ────

@dataclass
class BeforeToolCallContext:
    """Payload handed to ``before_tool_call``. ``args`` are
    already validated against the tool's schema."""
    assistant_message: AssistantMessage
    tool_call: ToolCall
    args: dict
    context: AgentContext


@dataclass
class BeforeToolCallResult:
    """``before_tool_call`` return value. Only power is to
    block the call (the loop then emits an error tool result). Cannot mutate
    args."""
    block: bool = False
    reason: str | None = None


@dataclass
class AfterToolCallContext:
    """Payload handed to ``after_tool_call``. ``result`` is
    the post-execute (pre-hook) result."""
    assistant_message: AssistantMessage
    tool_call: ToolCall
    args: dict
    result: AgentToolResult
    is_error: bool
    context: AgentContext


@dataclass
class AfterToolCallResult:
    """``after_tool_call`` return value. Fields are applied
    field-by-field (``??``-style fallback, no deep merge). Setting
    ``terminate`` is the only external way to drive batch termination."""
    content: list | None = None
    details: Any = None
    is_error: bool | None = None
    terminate: bool | None = None


@dataclass
class AgentLoopConfig:
    """Config for one agent loop run.

    Tool-execution hooks are optional (None = not configured). The
    steering/follow-up queue drain callbacks drive the
    outer/inner dual loop; both None = no queue (the loop never injects
    pending messages and stops after a single inner run).
    """
    model: Model
    convert_to_llm: Callable[[list], "Awaitable[list[Message]]"]
    get_api_key: "Callable[[str], Awaitable[str | None] | str | None] | None" = None
    # Passthrough stream options (reasoning, etc.)
    reasoning: ThinkingLevel | None = None
    # Tool execution.
    tool_execution: ToolExecutionMode | None = None
    before_tool_call: "Callable[[BeforeToolCallContext, Any], Awaitable[BeforeToolCallResult | None] | BeforeToolCallResult | None] | None" = None
    after_tool_call: "Callable[[AfterToolCallContext, Any], Awaitable[AfterToolCallResult | None] | AfterToolCallResult | None] | None" = None
    # Queue drain callbacks. The loop polls steering before the
    # outer loop, after each inner turn, and follow-up once the inner loop
    # naturally stops. Sync or async return values both accepted.
    get_steering_messages: "Callable[[], Awaitable[list] | list] | None" = None
    get_follow_up_messages: "Callable[[], Awaitable[list] | list] | None" = None


# ─── Events ───────────────────────────────────


class AgentStartEvent(TypedDict):
    type: Literal["agent_start"]


class TurnStartEvent(TypedDict):
    type: Literal["turn_start"]


class MessageStartEvent(TypedDict):
    message: AssistantMessage


class _MessageStartEvent(MessageStartEvent):
    type: Literal["message_start"]


class MessageUpdateEvent(TypedDict):
    type: Literal["message_update"]
    message: AssistantMessage
    event: AssistantMessageEvent  # the raw underlying stream event


class MessageEndEvent(TypedDict):
    type: Literal["message_end"]
    message: AssistantMessage


class ToolExecutionStartEvent(TypedDict):
    type: Literal["tool_execution_start"]
    tool_call_id: str
    tool_name: str
    args: Any


class ToolExecutionUpdateEvent(TypedDict):
    type: Literal["tool_execution_update"]
    tool_call_id: str
    tool_name: str
    args: Any
    partial_result: AgentToolResult


class ToolExecutionEndEvent(TypedDict):
    type: Literal["tool_execution_end"]
    tool_call_id: str
    tool_name: str
    result: AgentToolResult
    is_error: bool


class TurnEndEvent(TypedDict):
    type: Literal["turn_end"]
    message: AssistantMessage
    tool_results: list  # list[ToolResultMessage]


class AgentEndEvent(TypedDict):
    type: Literal["agent_end"]
    messages: list  # list[AgentMessage]


AgentEvent = Union[
    AgentStartEvent,
    TurnStartEvent,
    _MessageStartEvent,
    MessageUpdateEvent,
    MessageEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    ToolExecutionEndEvent,
    TurnEndEvent,
    AgentEndEvent,
]


#: Event sink callback. Awaited at each emission point.
AgentEventSink = Callable[[AgentEvent], "Awaitable[None] | None"]


# ─── Agent state ─────────────────────

@dataclass
class AgentState:
    """Public agent state.

    ``is_streaming`` is True while processing a prompt. ``pending_tool_calls``
    tracks tool call ids currently executing.
    """
    system_prompt: str = ""
    model: Model = field(default_factory=lambda: Model())
    messages: list = field(default_factory=list)  # list[AgentMessage]
    tools: list = field(default_factory=list)  # list[AgentTool]
    is_streaming: bool = False
    streaming_message: AgentMessage | None = None
    pending_tool_calls: set = field(default_factory=set)  # set[str]
    error_message: str | None = None
