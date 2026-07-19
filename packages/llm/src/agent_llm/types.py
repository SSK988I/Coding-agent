"""
本模块定义了完整的类型系统:message 角色、content block、model/provider
目录类型、streaming 事件,以及 stream 选项。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol, TypedDict, Union, runtime_checkable

# ─── API / provider 标识符 ─────────────────────────────────────────────

#: 所有已知的 API 后端。也允许自定义字符串 API。
KnownApi = Literal[
    "openai-completions",
    "mistral-conversations",
    "openai-responses",
    "azure-openai-responses",
    "openai-codex-responses",
    "anthropic-messages",
    "bedrock-converse-stream",
    "google-generative-ai",
    "google-vertex",
]
Api = Union[KnownApi, str]

#: Thinking/reasoning 级别。
ThinkingLevel = Literal["minimal", "low", "medium", "high", "xhigh"]
ModelThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh"]
#: 将 thinking 级别映射到 provider 取值;``None`` 表示不支持。
ThinkingLevelMap = dict[ModelThinkingLevel, Union[str, None]]

#: 每个 thinking 级别的 token 预算,用于按 token 计费的 provider。
class ThinkingBudgets(TypedDict, total=False):
    minimal: int
    low: int
    medium: int
    high: int


#: Cache 保留偏好。
CacheRetention = Literal["none", "short", "long"]
#: 线路传输方式。
Transport = Literal["sse", "websocket", "websocket-cached", "auto"]

#: Provider 作用域的环境变量覆盖(优先级高于进程级环境变量)。
ProviderEnv = dict[str, str]
#: Provider 作用域的 headers;None 表示屏蔽某个默认 header。
ProviderHeaders = dict[str, Union[str, None]]

#: 一个 assistant 轮次的最终 stop_reason。
StopReason = Literal["stop", "length", "tool_use", "error", "aborted"]


# ─── 费用 / usage ──────────────────────────────────────────────────────

@dataclass
class ModelCost:
    """每百万 token 的美元单价。"""
    input: float
    output: float
    cache_read: float
    cache_write: float


@dataclass
class UsageCost:
    """单次请求的计算美元费用。"""
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    total: float = 0.0


@dataclass
class Usage:
    """单次请求的 token 记账。

    ``reasoning`` 是 ``output`` 的子集。``cache_write_1h`` 是 Anthropic
    专属的 ``cache_write`` 细分项。
    """
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cache_write_1h: int = 0
    reasoning: int | None = None
    total_tokens: int = 0
    cost: UsageCost = field(default_factory=UsageCost)


# ─── Content block ─────────────────────────────────────────────────────

@dataclass
class TextContent:
    """文本内容块。"""
    type: Literal["text"] = "text"
    text: str = ""
    text_signature: str | None = None  # OpenAI responses message 的元数据


@dataclass
class ThinkingContent:
    """思维链内容块。"""
    type: Literal["thinking"] = "thinking"
    thinking: str = ""
    thinking_signature: str | None = None  # 不透明签名,用于多轮复用
    redacted: bool = False  # True 表示安全过滤器已遮蔽该载荷


@dataclass
class ImageContent:
    """图片内容块。``data`` 为 base64 编码。"""
    type: Literal["image"] = "image"
    data: str = ""
    mime_type: str = ""


@dataclass
class ToolCall:
    """assistant 请求的一次 tool 调用。"""
    type: Literal["toolCall"] = "toolCall"
    id: str = ""
    name: str = ""
    arguments: dict = field(default_factory=dict)
    thought_signature: str | None = None  # Google 专属的 thought 复用签名


#: assistant message 可能包含的 content block 的联合类型。
ContentBlock = Union[TextContent, ThinkingContent, ImageContent, ToolCall]
#: user / toolResult message 可能包含的 content block 的联合类型。
UserContentBlock = Union[TextContent, ImageContent]


# ─── Message ────────────────────────────────────────────────────────────

def _now_ms() -> float:
    """Unix 时间戳(毫秒)。"""
    return time.time() * 1000.0


@dataclass
class UserMessage:
    role: Literal["user"] = "user"
    content: Union[str, list[UserContentBlock]] = ""
    timestamp: float = field(default_factory=_now_ms)


@dataclass
class AssistantMessage:
    """
    stream 出现 error/abort 时,``stop_reason`` 为 ``"error"``/``"aborted"``,
    ``error_message`` 携带详情;``content`` 可能是 partial 或空。
    """
    role: Literal["assistant"] = "assistant"
    content: list[ContentBlock] = field(default_factory=list)
    api: Api = ""
    provider: str = ""
    model: str = ""
    response_model: str | None = None  # 当 chunk.model 与请求不同时记录实际值
    response_id: str | None = None
    usage: Usage = field(default_factory=Usage)
    stop_reason: StopReason = "stop"
    error_message: str | None = None
    timestamp: float = field(default_factory=_now_ms)


@dataclass
class ToolResultMessage:
    """把 tool 的输出带回给 model。"""
    role: Literal["toolResult"] = "toolResult"
    tool_call_id: str = ""
    tool_name: str = ""
    content: list[UserContentBlock] = field(default_factory=list)
    details: Any = None  # 给日志/UI 用的结构化详情,不会原样发给 model
    is_error: bool = False
    timestamp: float = field(default_factory=_now_ms)


#: 三种面向 LLM 的 message 角色。
Message = Union[UserMessage, AssistantMessage, ToolResultMessage]


# ─── Tool ──────────────────────────────────────────────────────────────

@dataclass
class Tool:
    """发给 model 的 tool 定义。

    ``parameters`` 是一个 JSON Schema 字典。
    """
    name: str = ""
    description: str = ""
    parameters: dict = field(default_factory=dict)


# ─── Context ───────────────────────────────────────────────────────────

@dataclass
class Context:
    """单次 completion 请求的输入。"""
    system_prompt: str | None = None
    messages: list[Message] = field(default_factory=list)
    tools: list[Tool] | None = None


# ─── Model ─────────────────────────────────────────────────────────────

@dataclass
class Model:
    """provider 暴露的一个 model。

    ``compat`` 是一个字典,携带 API 专属的兼容性覆盖项
    (OpenAICompletionsCompat / OpenAIResponsesCompat / AnthropicMessagesCompat)。
    它的结构由 API 模块自省读取,而非静态类型化。
    """
    id: str = ""
    name: str = ""
    api: Api = ""
    provider: str = ""
    base_url: str = ""
    reasoning: bool = False
    thinking_level_map: ThinkingLevelMap | None = None
    input: list[str] = field(default_factory=list)  # ["text"] or ["text","image"]
    cost: ModelCost = field(default_factory=ModelCost)
    context_window: int = 0
    max_tokens: int = 0
    headers: ProviderHeaders | None = None
    compat: dict | None = None


# ─── Streaming 事件 ─────────────────────────────────
#
# 所有非终止事件都携带 ``partial``:正在累积的 AssistantMessage。只有
# ``done`` / ``error`` 是终止事件。stream 契约:先发 ``start``,再发若干
# partial 更新,最后发且仅发一个终止事件。

class _EventPartial(TypedDict):
    """所有携带 partial AssistantMessage 的事件共享的字段。"""
    partial: AssistantMessage


class StartEvent(_EventPartial):
    type: Literal["start"]


class TextStartEvent(_EventPartial):
    type: Literal["text_start"]
    content_index: int


class TextDeltaEvent(_EventPartial):
    type: Literal["text_delta"]
    content_index: int
    delta: str


class TextEndEvent(_EventPartial):
    type: Literal["text_end"]
    content_index: int
    content: str


class ThinkingStartEvent(_EventPartial):
    type: Literal["thinking_start"]
    content_index: int


class ThinkingDeltaEvent(_EventPartial):
    type: Literal["thinking_delta"]
    content_index: int
    delta: str


class ThinkingEndEvent(_EventPartial):
    type: Literal["thinking_end"]
    content_index: int
    content: str


class ToolCallStartEvent(_EventPartial):
    type: Literal["toolcall_start"]
    content_index: int


class ToolCallDeltaEvent(_EventPartial):
    type: Literal["toolcall_delta"]
    content_index: int
    delta: str


class ToolCallEndEvent(_EventPartial):
    type: Literal["toolcall_end"]
    content_index: int
    tool_call: ToolCall


class DoneEvent(TypedDict):
    type: Literal["done"]
    reason: Literal["stop", "length", "tool_use"]
    message: AssistantMessage


class ErrorEvent(TypedDict):
    type: Literal["error"]
    reason: Literal["aborted", "error"]
    error: AssistantMessage


AssistantMessageEvent = Union[
    StartEvent,
    TextStartEvent,
    TextDeltaEvent,
    TextEndEvent,
    ThinkingStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ToolCallStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    DoneEvent,
    ErrorEvent,
]


# ─── Stream 选项 ───────────────────────────────────────────────────────

class ProviderResponse(TypedDict):
    status: int
    headers: dict[str, str]


class StreamOptions(TypedDict, total=False):
    """所有 provider 共享的基础 stream 选项。

    所有字段可选;provider 各取所需,只读自己理解的字段。
    """
    temperature: float
    max_tokens: int
    api_key: str
    transport: Transport
    cache_retention: CacheRetention
    session_id: str
    on_payload: Callable[[Any, Model], Any]
    on_response: Callable[[ProviderResponse, Model], Any]
    headers: ProviderHeaders
    timeout_ms: int
    websocket_connect_timeout_ms: int
    max_retries: int
    max_retry_delay_ms: int
    metadata: dict[str, Any]
    env: ProviderEnv


class SimpleStreamOptions(StreamOptions, total=False):
    """带 reasoning 级别的统一选项。"""
    reasoning: ThinkingLevel
    thinking_budgets: ThinkingBudgets


#: 每个 provider 的 stream 选项:StreamOptions 加上 provider 专属的额外字段。
ProviderStreamOptions = StreamOptions


# ─── Provider streams 契约 ─────────────────────────────────────────────
#
# 通过 TYPE_CHECKING 前向声明以避免运行时循环引用:AssistantMessageEventStream
# 类定义在 event_stream.py 中,而它又 import 本模块的类型。

if False:  # TYPE_CHECKING
    from agent_llm.event_stream import AssistantMessageEventStream


@runtime_checkable
class ProviderStreams(Protocol):
    """每个 ``src/api/*.py`` 模块都要满足的统一契约。

    每个 API 模块都要导出符合此形状的 ``stream`` 和 ``stream_simple``
    可调用对象。
    """

    def __call_stream(
        self,
        model: Model,
        context: Context,
        options: StreamOptions | None,
    ) -> "AssistantMessageEventStream": ...

    def __call_stream_simple(
        self,
        model: Model,
        context: Context,
        options: SimpleStreamOptions | None,
    ) -> "AssistantMessageEventStream": ...


#: stream 函数:(model, context, options?) -> AssistantMessageEventStream。
#: 失败必须编码进返回的 stream 中,绝不能抛出异常。
StreamFunction = Callable[
    [Model, Context, "StreamOptions | None"],
    "AssistantMessageEventStream",
]


# ─── OpenAI-completions compat ─────────────────────────────────────────

class OpenAICompletionsCompat(TypedDict, total=False):
    """OpenAI 兼容 completions API 的兼容性覆盖项。

    当 model 上未设置时,这些值由 API 模块根据 base_url 自动探测。
    """
    supports_store: bool
    supports_developer_role: bool
    supports_reasoning_effort: bool
    supports_usage_in_streaming: bool
    max_tokens_field: Literal["max_completion_tokens", "max_tokens"]
    requires_tool_result_name: bool
    requires_assistant_after_tool_result: bool
    requires_thinking_as_text: bool
    requires_reasoning_content_on_assistant_messages: bool
    thinking_format: Literal[
        "openai",
        "openrouter",
        "deepseek",
        "together",
        "zai",
        "qwen",
        "chat-template",
        "qwen-chat-template",
        "string-thinking",
        "ant-ling",
    ]
    chat_template_kwargs: dict[str, Any]
    supports_strict_mode: bool
    cache_control_format: Literal["anthropic"]
    send_session_affinity_headers: bool
    supports_long_cache_retention: bool
