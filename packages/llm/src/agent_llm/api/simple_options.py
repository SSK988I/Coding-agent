"""SimpleStreamOptions 辅助函数。

把统一的 ``SimpleStreamOptions``(带 reasoning 级别)映射成裸的
``StreamOptions``,并处理 thinking 预算的算术,这样每个 API 模块的
``stream_simple`` 就可以直接委派给它自己的原始 ``stream``。
"""
from __future__ import annotations

from typing import Any

from agent_llm.types import (
    Context,
    Model,
    SimpleStreamOptions,
    StreamOptions,
    ThinkingBudgets,
    ThinkingLevel,
)

#: 每个 thinking 级别的默认 token 预算。
_DEFAULT_BUDGETS: ThinkingBudgets = {"minimal": 1024, "low": 2048, "medium": 8192, "high": 16384}


def clamp_reasoning(effort: ThinkingLevel) -> ThinkingLevel:
    """把 ``"xhigh"`` 收敛为 ``"high"``;其它原样透传。"""
    if effort == "xhigh":
        return "high"  # type: ignore[return-value]
    return effort


def clamp_max_tokens_to_context(model: Model, context: Context, max_tokens: int) -> int:
    """把 max_tokens 限制在 contextWindow - 预估 prompt token - 4096 以内,最小为 1。

    用一个粗略的 字符数/4 估算值作为 prompt token 数的近似。
    """
    estimated = _estimate_context_tokens(context)
    cap = max(1, model.context_window - estimated - 4096)
    return min(max_tokens, cap)


def _estimate_context_tokens(context: Context) -> int:
    """粗略的 token 估算:system + messages 的字符数 / 4(仅作回退)。"""
    total = 0
    if context.system_prompt:
        total += len(context.system_prompt)
    for msg in context.messages:
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                text = getattr(block, "text", None) or getattr(block, "thinking", None) or ""
                total += len(text)
        # tool_call arguments
        for attr in ("tool_call_id", "tool_name"):
            v = getattr(msg, attr, None)
            if v:
                total += len(str(v))
    return total // 4


def build_base_options(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
    api_key: str | None = None,
) -> StreamOptions:
    """把 SimpleStreamOptions 映射为裸 StreamOptions,丢掉 reasoning 相关字段。

    会对 max_tokens 应用上下文窗口限制。
    """
    opts: dict[str, Any] = {}
    if options:
        for k in (
            "temperature",
            "transport",
            "cache_retention",
            "session_id",
            "on_payload",
            "on_response",
            "headers",
            "timeout_ms",
            "websocket_connect_timeout_ms",
            "max_retries",
            "max_retry_delay_ms",
            "metadata",
            "env",
        ):
            if k in options:
                opts[k] = options[k]
        # max_tokens 会在下面被限制。
        if "max_tokens" in options:
            opts["max_tokens"] = options["max_tokens"]
    if api_key:
        opts["api_key"] = api_key
    # 把 max_tokens 限制到上下文窗口内。
    if "max_tokens" in opts:
        opts["max_tokens"] = clamp_max_tokens_to_context(model, context, opts["max_tokens"])
    return opts  # type: ignore[return-value]


def adjust_max_tokens_for_thinking(
    base_max_tokens: int | None,
    model_max_tokens: int,
    reasoning_level: ThinkingLevel,
    custom_budgets: ThinkingBudgets | None = None,
) -> "tuple[int, int]":
    """为 reasoning 请求计算 (max_tokens, thinking_budget)。

    将自定义预算合并到默认值之上。若调用方没有显式给上限,则
    max_tokens = model 上限;否则 max_tokens = min(base + budget, model 上限)。
    如果没有给输出留出空间,就把预算缩减到 max(0, max_tokens - 1024)。
    """
    budgets = {**_DEFAULT_BUDGETS, **(custom_budgets or {})}
    budget = budgets.get(reasoning_level, budgets.get("high", 16384))
    if base_max_tokens is None:
        max_tokens = model_max_tokens
    else:
        max_tokens = min(base_max_tokens + budget, model_max_tokens)
    if max_tokens <= budget:
        budget = max(0, max_tokens - 1024)
    return max_tokens, budget
