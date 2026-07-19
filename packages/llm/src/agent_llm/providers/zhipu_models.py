"""Zhipu GLM Coding Plan 的 model 目录。

全部六个 model 都走 GLM Coding Plan 端点
``https://open.bigmodel.cn/api/coding/paas/v4``,并使用 ``zai`` thinking
格式:请求体里带 ``thinking: {type, clear_thinking}``,响应里单独返回
``reasoning_content``。

定价全部为零 —— 因为 Coding Plan 是固定订阅制。
"""
from __future__ import annotations

from agent_llm.types import Model, ModelCost

#: Coding Plan 端点(不是按量计费的 /api/paas/v4 —— Coding Plan 的 key 只能
#: 用在这个专用路径上)。
_BASE_URL = "https://open.bigmodel.cn/api/coding/paas/v4"

#: GLM 基础 compat 块。``thinking_format:"zai"`` 让 openai_completions 的
#: _inject_thinking_params 走 Zhipu 专属分支。``zai_tool_stream``(下面按
#: model 单独设置)告诉传输层在 ``tools`` 之外带上 ``tool_stream: true``,
#: 这样 GLM 才会以流式吐出 tool-call delta。
_BASE_COMPAT = {
    "supports_store": False,
    "supports_developer_role": False,
    "thinking_format": "zai",
}

#: 支持 reasoning 但不能调 effort:任何请求的 effort 都会被丢弃,只有 on/off
#: 的 ``thinking`` 开关生效。除 glm-5.2 外的所有 GLM model 都用它
#: (见 _GLM52_THINKING_LEVEL_MAP)。
_BUILTIN_THINKING_LEVEL_MAP = {
    "minimal": None,
    "low": None,
    "medium": None,
    "high": None,
    "xhigh": None,
}

#: glm-5.2 是唯一接受可调 ``reasoning_effort`` 的 GLM model。
#: "minimal" 映射到 None(等价于"关");其它级别塌缩到智谱的 {high, max} 枚举。
_GLM52_THINKING_LEVEL_MAP = {
    "minimal": None,
    "low": "high",
    "medium": "high",
    "high": "high",
    "xhigh": "max",
}


def _compat(*, tool_stream: bool, supports_reasoning_effort: bool) -> dict:
    """在 GLM 基础 compat 块之上叠加每个 model 的专属字段。"""
    d = dict(_BASE_COMPAT)
    d["zai_tool_stream"] = tool_stream
    d["supports_reasoning_effort"] = supports_reasoning_effort
    return d


ZHIPU_MODELS: dict[str, Model] = {
    # glm-4.5-air 是唯一不带 zai_tool_stream 的 model。
    "glm-4.5-air": Model(
        id="glm-4.5-air",
        name="GLM-4.5-Air",
        api="openai-completions",
        provider="zai-coding-cn",
        base_url=_BASE_URL,
        compat=_compat(tool_stream=False, supports_reasoning_effort=False),
        reasoning=True,
        thinking_level_map=dict(_BUILTIN_THINKING_LEVEL_MAP),  # type: ignore[arg-type]
        input=["text"],
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
        context_window=131_072,
        max_tokens=98_304,
    ),
    "glm-4.7": Model(
        id="glm-4.7",
        name="GLM-4.7",
        api="openai-completions",
        provider="zai-coding-cn",
        base_url=_BASE_URL,
        compat=_compat(tool_stream=True, supports_reasoning_effort=False),
        reasoning=True,
        thinking_level_map=dict(_BUILTIN_THINKING_LEVEL_MAP),  # type: ignore[arg-type]
        input=["text"],
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
        context_window=204_800,
        max_tokens=131_072,
    ),
    "glm-5-turbo": Model(
        id="glm-5-turbo",
        name="GLM-5-Turbo",
        api="openai-completions",
        provider="zai-coding-cn",
        base_url=_BASE_URL,
        compat=_compat(tool_stream=True, supports_reasoning_effort=False),
        reasoning=True,
        thinking_level_map=dict(_BUILTIN_THINKING_LEVEL_MAP),  # type: ignore[arg-type]
        input=["text"],
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
        context_window=200_000,
        max_tokens=131_072,
    ),
    "glm-5.1": Model(
        id="glm-5.1",
        name="GLM-5.1",
        api="openai-completions",
        provider="zai-coding-cn",
        base_url=_BASE_URL,
        compat=_compat(tool_stream=True, supports_reasoning_effort=False),
        reasoning=True,
        thinking_level_map=dict(_BUILTIN_THINKING_LEVEL_MAP),  # type: ignore[arg-type]
        input=["text"],
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
        context_window=200_000,
        max_tokens=131_072,
    ),
    "glm-5.2": Model(
        id="glm-5.2",
        name="GLM-5.2",
        api="openai-completions",
        provider="zai-coding-cn",
        base_url=_BASE_URL,
        compat=_compat(tool_stream=True, supports_reasoning_effort=True),
        reasoning=True,
        thinking_level_map=dict(_GLM52_THINKING_LEVEL_MAP),  # type: ignore[arg-type]
        input=["text"],
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
        context_window=1_000_000,
        max_tokens=131_072,
    ),
    "glm-5v-turbo": Model(
        id="glm-5v-turbo",
        name="GLM-5V-Turbo",
        api="openai-completions",
        provider="zai-coding-cn",
        base_url=_BASE_URL,
        compat=_compat(tool_stream=True, supports_reasoning_effort=False),
        reasoning=True,
        thinking_level_map=dict(_BUILTIN_THINKING_LEVEL_MAP),  # type: ignore[arg-type]
        input=["text", "image"],
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
        context_window=200_000,
        max_tokens=131_072,
    ),
}
