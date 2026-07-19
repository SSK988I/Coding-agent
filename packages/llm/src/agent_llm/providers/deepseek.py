"""DeepSeek provider 工厂。

构造基于 OpenAI-completions stream 的 DeepSeek provider。
auth 为单个环境变量 ``DEEPSEEK_API_KEY``,通过 ``env_api_key_auth`` 读取。
"""
from __future__ import annotations

from agent_llm.auth.helpers import env_api_key_auth
from agent_llm.auth.types import ProviderAuth
from agent_llm.models import Provider, create_provider
from agent_llm.providers.deepseek_models import DEEPSEEK_MODELS

# 提供 stream/stream_simple 的 API 模块(懒加载,以避免循环 import:
# openai_completions 会 import models,而本文件又会被 models 链路 import)。
_API_STREAMS = None  # type: ignore[var-annotated]


def _get_api_streams():
    global _API_STREAMS
    if _API_STREAMS is None:
        from agent_llm.api import openai_completions
        _API_STREAMS = openai_completions
    return _API_STREAMS


def deepseek_provider() -> Provider:
    """构造 DeepSeek provider。"""
    return create_provider(
        id="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com",
        auth=ProviderAuth(api_key=env_api_key_auth("DeepSeek API key", ["DEEPSEEK_API_KEY"])),
        models=list(DEEPSEEK_MODELS.values()),
        api=_get_api_streams(),
    )
