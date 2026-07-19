"""Tests for the ``zai`` thinking format and ``tool_stream`` injection in
``openai_completions.py``.

These verify the wire-level request shape Zhipu expects without making any
network call — every assertion targets ``build_params`` / ``_inject_thinking_params``
output dicts.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_llm.api.openai_completions import _inject_thinking_params, _resolve_compat, build_params
from agent_llm.providers.deepseek_models import DEEPSEEK_MODELS
from agent_llm.providers.zhipu_models import ZHIPU_MODELS
from agent_llm.types import Context, Tool, UserMessage


def _ctx_with_tool() -> Context:
    return Context(
        messages=[UserMessage(content="hi")],
        tools=[Tool(name="foo", description="d", parameters={"type": "object", "properties": {}})],
    )


def _ctx_no_tool() -> Context:
    return Context(messages=[UserMessage(content="hi")])


# ─── _inject_thinking_params: zai format ─────────────────────────────────


def test_glm_5_2_with_reasoning_emits_enabled_thinking_with_clear_thinking_false():
    """glm-5.2 + reasoning='medium' → thinking{enabled,clear_thinking:false} + reasoning_effort=high."""
    m = ZHIPU_MODELS["glm-5.2"]
    params: dict = {}
    _inject_thinking_params(params, m, {"reasoning": "medium"}, _resolve_compat(m))
    extra = params["extra_body"]
    assert extra["thinking"] == {"type": "enabled", "clear_thinking": False}
    assert extra["reasoning_effort"] == "high"  # medium maps to "high" in glm-5.2's TLM


def test_glm_5_2_xhigh_reasoning_maps_to_max():
    m = ZHIPU_MODELS["glm-5.2"]
    params: dict = {}
    _inject_thinking_params(params, m, {"reasoning": "xhigh"}, _resolve_compat(m))
    assert params["extra_body"]["reasoning_effort"] == "max"


def test_glm_5_2_no_reasoning_emits_disabled():
    m = ZHIPU_MODELS["glm-5.2"]
    params: dict = {}
    _inject_thinking_params(params, m, {}, _resolve_compat(m))
    assert params["extra_body"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in params["extra_body"]


def test_glm_5_2_minimal_level_drops_effort():
    """minimal → None in thinking_level_map → no reasoning_effort, but thinking still enabled."""
    m = ZHIPU_MODELS["glm-5.2"]
    params: dict = {}
    _inject_thinking_params(params, m, {"reasoning": "minimal"}, _resolve_compat(m))
    assert params["extra_body"]["thinking"] == {"type": "enabled", "clear_thinking": False}
    assert "reasoning_effort" not in params["extra_body"]


def test_non_glm52_model_never_emits_reasoning_effort():
    """glm-4.7 supports reasoning but NOT effort tuning — no reasoning_effort key."""
    m = ZHIPU_MODELS["glm-4.7"]
    for level in ("low", "medium", "high", "xhigh"):
        params: dict = {}
        _inject_thinking_params(params, m, {"reasoning": level}, _resolve_compat(m))
        assert params["extra_body"]["thinking"] == {"type": "enabled", "clear_thinking": False}, level
        assert "reasoning_effort" not in params["extra_body"], level


# ─── build_params: tool_stream injection ─────────────────────────────────


def test_tool_stream_set_true_for_glm_5_2_with_tools():
    p = build_params(ZHIPU_MODELS["glm-5.2"], _ctx_with_tool(), {"api_key": "x"}, _resolve_compat(ZHIPU_MODELS["glm-5.2"]))
    assert p["extra_body"]["tool_stream"] is True


def test_tool_stream_set_true_for_glm_4_7_with_tools():
    p = build_params(ZHIPU_MODELS["glm-4.7"], _ctx_with_tool(), {"api_key": "x"}, _resolve_compat(ZHIPU_MODELS["glm-4.7"]))
    assert p["extra_body"]["tool_stream"] is True


def test_tool_stream_absent_for_glm_4_5_air_even_with_tools():
    """glm-4.5-air is the only GLM without zai_tool_stream — flag must NOT be set."""
    m = ZHIPU_MODELS["glm-4.5-air"]
    p = build_params(m, _ctx_with_tool(), {"api_key": "x"}, _resolve_compat(m))
    assert "tool_stream" not in p.get("extra_body", {})


def test_tool_stream_absent_for_glm_models_without_tools():
    p = build_params(ZHIPU_MODELS["glm-5.2"], _ctx_no_tool(), {"api_key": "x"}, _resolve_compat(ZHIPU_MODELS["glm-5.2"]))
    assert "tool_stream" not in p.get("extra_body", {})


# ─── DeepSeek regression ─────────────────────────────────────────────────


def test_deepseek_never_emits_tool_stream():
    """DeepSeek's compat has no zai_tool_stream — build_params must not add tool_stream."""
    for mid, m in DEEPSEEK_MODELS.items():
        p = build_params(m, _ctx_with_tool(), {"api_key": "x"}, _resolve_compat(m))
        assert "tool_stream" not in p.get("extra_body", {}), mid


def test_deepseek_thinking_format_uses_simpler_shape():
    """DeepSeek: thinking={type:'enabled'} without clear_thinking (NOT zai shape)."""
    m = DEEPSEEK_MODELS["deepseek-v4-pro"]
    params: dict = {}
    _inject_thinking_params(params, m, {"reasoning": "high"}, _resolve_compat(m))
    assert params["extra_body"]["thinking"] == {"type": "enabled"}
    assert "clear_thinking" not in params["extra_body"]["thinking"]


# ─── Combined: full build_params for glm-5.2 ─────────────────────────────


def test_glm_5_2_full_build_params_with_tools_and_reasoning():
    """End-to-end: a real glm-5.2 request carries tools + tool_stream + thinking + effort."""
    m = ZHIPU_MODELS["glm-5.2"]
    p = build_params(m, _ctx_with_tool(), {"api_key": "x", "reasoning": "high"}, _resolve_compat(m))
    assert p["model"] == "glm-5.2"
    assert p["tools"]  # converted tool list
    assert p["extra_body"]["tool_stream"] is True
    assert p["extra_body"]["thinking"] == {"type": "enabled", "clear_thinking": False}
    assert p["extra_body"]["reasoning_effort"] == "high"  # high → high
