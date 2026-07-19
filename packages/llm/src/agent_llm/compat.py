"""顶层便利函数。

提供模块级的 ``stream`` / ``complete`` / ``stream_simple`` /
``complete_simple``,它们通过一个默认的 :class:`Models` 实例分发,该实例
预先加载了内置 provider(目前是 DeepSeek 和 Zhipu)。

只想"从一个 model 拉流"、不想自己管理 Models 集合的调用方可以直接用这些
函数。agent 层(agent_core)则采用另一种方式 —— 接收一个 ``stream_fn`` 并
显式注入,因此不依赖此全局状态。
"""
from __future__ import annotations


from agent_llm.event_stream import AssistantMessageEventStream
from agent_llm.models import Models, create_models
from agent_llm.providers.deepseek import deepseek_provider
from agent_llm.providers.zhipu import zhipu_provider
from agent_llm.types import AssistantMessage, Context, Model, SimpleStreamOptions, StreamOptions

# 懒加载的默认 Models 实例,内置 provider 已注册。
_default_models: Models | None = None


def _get_default_models() -> Models:
    global _default_models
    if _default_models is None:
        m = create_models()
        m.set_provider(deepseek_provider())
        m.set_provider(zhipu_provider())
        _default_models = m
    return _default_models


def stream(
    model: Model, context: Context, options: StreamOptions | None = None
) -> AssistantMessageEventStream:
    """通过默认 Models 实例从 ``model`` 拉流。"""
    return _get_default_models().stream(model, context, options)


async def complete(
    model: Model, context: Context, options: StreamOptions | None = None
) -> AssistantMessage:
    """完成一次请求,返回最终的 AssistantMessage。"""
    return await _get_default_models().complete(model, context, options)


def stream_simple(
    model: Model, context: Context, options: SimpleStreamOptions | None = None
) -> AssistantMessageEventStream:
    """带统一 reasoning 级别选项的拉流。"""
    return _get_default_models().stream_simple(model, context, options)


async def complete_simple(
    model: Model, context: Context, options: SimpleStreamOptions | None = None
) -> AssistantMessage:
    return await _get_default_models().complete_simple(model, context, options)
