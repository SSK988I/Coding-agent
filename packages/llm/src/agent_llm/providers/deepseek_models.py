"""DeepSeek model 目录。

使用官方 API 名称 ``deepseek-flash``，对应 DeepSeek V4.1 Flash。
旧 Flash 入口归一化到 V4.1 Flash；V4 Pro 仍是独立服务，不跨模型迁移。
价格使用美元高峰价估算；闲时及中国法定节假日的实际单价为一半。
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

#: 官方支持 low / high / max；统一级别映射到对应的 API 参数。
_DEEPSEEK_THINKING_LEVEL_MAP = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "max",
}

#: 兼容旧 CLI 参数、设置和保存的会话，不向用户暴露已退役的选项。
DEEPSEEK_MODEL_ALIASES: dict[str, str] = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
}

DEEPSEEK_MODELS: dict[str, Model] = {
    "deepseek-flash": Model(
        id="deepseek-flash",
        name="DeepSeek V4.1 Flash",
        api="openai-responses",
        provider="deepseek",
        base_url="https://api.deepseek.com",
        compat=dict(_DEEPSEEK_COMPAT),
        reasoning=True,
        thinking_level_map=dict(_DEEPSEEK_THINKING_LEVEL_MAP),  # type: ignore[arg-type]
        input=["text", "image"],
        cost=ModelCost(input=0.3, output=1.2, cache_read=0.006, cache_write=0),
        context_window=1_048_576,
        max_tokens=393_216,
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
        cost=ModelCost(input=1.32, output=3.96, cache_read=0.044, cache_write=0),
        context_window=1_048_576,
        max_tokens=393_216,
    ),
}
