"""Unified LLM providers, messages, authentication, and streaming events."""
# 核心类型
from agent_llm.types import (
    Api,
    AssistantMessage,
    AssistantMessageEvent,
    CacheRetention,
    ContentBlock,
    Context,
    ImageContent,
    KnownApi,
    Message,
    Model,
    ModelCost,
    ModelThinkingLevel,
    ProviderHeaders,
    ProviderResponse,
    ProviderStreamOptions,
    SimpleStreamOptions,
    StopReason,
    StreamFunction,
    StreamOptions,
    TextContent,
    ThinkingBudgets,
    ThinkingContent,
    ThinkingLevel,
    ThinkingLevelMap,
    Tool,
    ToolCall,
    ToolResultMessage,
    Transport,
    Usage,
    UsageCost,
    UserMessage,
)

# Event stream
from agent_llm.event_stream import AssistantMessageEventStream, EventStream, lazy_stream

# Auth
from agent_llm.auth import (
    ApiKeyAuth,
    AuthContext,
    AuthResult,
    Credential,
    CredentialStore,
    InMemoryCredentialStore,
    ModelAuth,
    ModelsError,
    OAuthAuth,
    ProviderAuth,
    default_auth_context,
    env_api_key_auth,
    resolve_provider_auth,
)

# Models / Provider
from agent_llm.models import (
    Models,
    Provider,
    calculate_cost,
    clamp_thinking_level,
    create_models,
    create_provider,
    get_supported_thinking_levels,
    has_api,
    models_are_equal,
)

# Compat 便利函数
from agent_llm.compat import complete, complete_simple, stream, stream_simple

# 内置 provider
from agent_llm.providers.deepseek import deepseek_provider
from agent_llm.providers.deepseek_models import DEEPSEEK_MODELS
from agent_llm.providers.zhipu import zhipu_provider
from agent_llm.providers.zhipu_models import ZHIPU_MODELS

# 校验
from agent_llm.utils.validation import validate_tool_arguments

__all__ = [
    # 类型
    "Api",
    "KnownApi",
    "Message",
    "UserMessage",
    "AssistantMessage",
    "ToolResultMessage",
    "TextContent",
    "ThinkingContent",
    "ImageContent",
    "ToolCall",
    "ContentBlock",
    "Context",
    "Tool",
    "Model",
    "ModelCost",
    "Usage",
    "UsageCost",
    "StopReason",
    "StreamOptions",
    "SimpleStreamOptions",
    "ProviderStreamOptions",
    "StreamFunction",
    "AssistantMessageEvent",
    "ThinkingLevel",
    "ModelThinkingLevel",
    "ThinkingLevelMap",
    "ThinkingBudgets",
    "Transport",
    "CacheRetention",
    "ProviderHeaders",
    "ProviderResponse",
    # Event stream
    "AssistantMessageEventStream",
    "EventStream",
    "lazy_stream",
    # Auth
    "ProviderAuth",
    "ApiKeyAuth",
    "OAuthAuth",
    "CredentialStore",
    "Credential",
    "ModelAuth",
    "AuthContext",
    "AuthResult",
    "ModelsError",
    "env_api_key_auth",
    "default_auth_context",
    "InMemoryCredentialStore",
    "resolve_provider_auth",
    # Models
    "Models",
    "Provider",
    "create_provider",
    "create_models",
    "has_api",
    "calculate_cost",
    "get_supported_thinking_levels",
    "clamp_thinking_level",
    "models_are_equal",
    # Compat
    "stream",
    "complete",
    "stream_simple",
    "complete_simple",
    # Providers
    "deepseek_provider",
    "DEEPSEEK_MODELS",
    "zhipu_provider",
    "ZHIPU_MODELS",
    # Validation
    "validate_tool_arguments",
]

__version__ = "0.2.0"
