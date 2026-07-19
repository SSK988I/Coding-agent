"""DeepSeek model 目录。

使用 DeepSeek 真实 API 的 model id(``deepseek-v4-flash`` /
``deepseek-v4-pro``)、定价、上下文窗口和 compat 配置。
"""
from __future__ import annotations

from agent_llm.types import Model, ModelCost

#: DeepSeek 的 compat 块。``thinking_format:"deepseek"`` 告诉 openai-completions
#: 发送 ``thinking: {type}`` 参数;``requires_reasoning_content_on_assistant_messages``
#: 在多轮对话中,回放 assistant message 时会带上空的 ``reasoning_content``。
_DEEPSEEK_COMPAT = {
    "supports_store": False,
    "supports_developer_role": False,
    "requires_reasoning_content_on_assistant_messages": True,
    "thinking_format": "deepseek",
}

#: 仅支持 high / xhigh;minimal / low / medium 显式为 null。
_DEEPSEEK_THINKING_LEVEL_MAP = {
    "minimal": None,
    "low": None,
    "medium": None,
    "high": "high",
    "xhigh": "max",
}

DEEPSEEK_MODELS: dict[str, Model] = {
    "deepseek-v4-flash": Model(
        id="deepseek-v4-flash",
        name="DeepSeek V4 Flash",
        api="openai-completions",
        provider="deepseek",
        base_url="https://api.deepseek.com",
        compat=dict(_DEEPSEEK_COMPAT),
        reasoning=True,
        thinking_level_map=dict(_DEEPSEEK_THINKING_LEVEL_MAP),  # type: ignore[arg-type]
        input=["text"],
        cost=ModelCost(input=0.14, output=0.28, cache_read=0.0028, cache_write=0),
        context_window=1_000_000,
        max_tokens=384_000,
    ),
    "deepseek-v4-pro": Model(
        id="deepseek-v4-pro",
        name="DeepSeek V4 Pro",
        api="openai-completions",
        provider="deepseek",
        base_url="https://api.deepseek.com",
        compat=dict(_DEEPSEEK_COMPAT),
        reasoning=True,
        thinking_level_map=dict(_DEEPSEEK_THINKING_LEVEL_MAP),  # type: ignore[arg-type]
        input=["text"],
        cost=ModelCost(input=0.435, output=0.87, cache_read=0.003625, cache_write=0),
        context_window=1_000_000,
        max_tokens=384_000,
    ),
}
