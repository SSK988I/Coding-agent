"""DeepSeek model 目录。

使用 DeepSeek 真实 API 的 model id(``deepseek-v4-flash`` /
``deepseek-v4-pro``)、定价、上下文窗口和 compat 配置。
"""
from __future__ import annotations

from agent_llm.types import Model, ModelCost

#: DeepSeek Responses API 兼容配置。thinking_format 仍保留为 deepseek，供
#: 自定义目录和诊断代码识别 provider 的推理语义。
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
        api="openai-responses",
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
        api="openai-responses",
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
