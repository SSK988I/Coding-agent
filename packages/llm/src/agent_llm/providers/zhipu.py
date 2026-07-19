"""Zhipu GLM Coding Plan provider 工厂。

构造基于 OpenAI-completions stream 的 ``zai-coding-cn`` provider。
auth 用的是 GLM Coding Plan 的 API key(从智谱开放平台
https://open.bigmodel.cn 获取),支持从三个环境变量任一个读取,用户用哪个
名字都行:

* ``ZAI_CODING_CN_API_KEY`` —— 规范名
* ``ZHIPU_API_KEY``         —— 社区惯用名
* ``GLM_API_KEY``           —— 短别名
"""
from __future__ import annotations

from agent_llm.auth.helpers import env_api_key_auth
from agent_llm.auth.types import ProviderAuth
from agent_llm.models import Provider, create_provider
from agent_llm.providers.zhipu_models import ZHIPU_MODELS

# 提供 stream/stream_simple 的 API 模块。懒加载以避免循环 import
# (openai_completions 会 import models,而本文件又会 import models)。
_API_STREAMS = None  # type: ignore[var-annotated]


def _get_api_streams():
    global _API_STREAMS
    if _API_STREAMS is None:
        from agent_llm.api import openai_completions
        _API_STREAMS = openai_completions
    return _API_STREAMS


def zhipu_provider() -> Provider:
    """构造 Zhipu GLM Coding Plan provider。"""
    return create_provider(
        id="zai-coding-cn",
        name="Z.AI Coding CN",
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        auth=ProviderAuth(api_key=env_api_key_auth(
            "Zhipu Coding Plan API key",
            ["ZAI_CODING_CN_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"],
        )),
        models=list(ZHIPU_MODELS.values()),
        api=_get_api_streams(),
    )
