"""Tests for the Zhipu GLM Coding Plan provider.

Verifies provider factory shape, model catalog fidelity vs the
``zai-coding-cn``, and that DeepSeek is untouched as a regression
guard.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_llm import DEEPSEEK_MODELS, ZHIPU_MODELS, deepseek_provider, zhipu_provider


# ─── Provider factory ────────────────────────────────────────────────────


def test_zhipu_provider_has_canonical_id_and_endpoint():
    p = zhipu_provider()
    assert p.id == "zai-coding-cn"
    assert p.name == "Z.AI Coding CN"
    # Coding Plan endpoint — NOT the pay-as-you-go /api/paas/v4.
    assert p.base_url == "https://open.bigmodel.cn/api/coding/paas/v4"


def test_zhipu_provider_accepts_three_env_var_aliases():
    """Users may export any of three names; ZAI_CODING_CN_API_KEY is canonical."""
    env_vars = zhipu_provider().auth.api_key.env_vars
    assert env_vars == ["ZAI_CODING_CN_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"]


def test_zhipu_provider_returns_six_models():
    ids = {m.id for m in zhipu_provider().get_models()}
    assert ids == {
        "glm-4.5-air", "glm-4.7", "glm-5-turbo",
        "glm-5.1", "glm-5.2", "glm-5v-turbo",
    }


def test_deepseek_provider_unchanged_regression():
    """Adding Zhipu must not perturb DeepSeek's factory or env var."""
    p = deepseek_provider()
    assert p.id == "deepseek"
    assert p.base_url == "https://api.deepseek.com"
    assert p.auth.api_key.env_vars == ["DEEPSEEK_API_KEY"]


# ─── Model catalog fidelity ────────────────────


def test_all_glm_models_use_zai_thinking_format():
    for mid, m in ZHIPU_MODELS.items():
        assert m.compat.get("thinking_format") == "zai", mid
        assert m.reasoning is True, mid
        assert m.api == "openai-completions", mid
        assert m.provider == "zai-coding-cn", mid
        assert m.base_url == "https://open.bigmodel.cn/api/coding/paas/v4", mid


def test_glm_5_2_is_only_model_supporting_reasoning_effort():
    for mid, m in ZHIPU_MODELS.items():
        if mid == "glm-5.2":
            assert m.compat.get("supports_reasoning_effort") is True
        else:
            assert m.compat.get("supports_reasoning_effort") is False, mid


def test_glm_5_2_thinking_level_map():
    """Z.AI reasoning levels map to the provider's supported effort values."""
    tlm = ZHIPU_MODELS["glm-5.2"].thinking_level_map
    assert tlm == {"minimal": None, "low": "high", "medium": "high", "high": "high", "xhigh": "max"}


def test_non_glm52_models_cannot_tune_effort():
    """All other GLM models map every level to None — reasoning is on/off only."""
    for mid, m in ZHIPU_MODELS.items():
        if mid == "glm-5.2":
            continue
        tlm = m.thinking_level_map or {}
        for level in ("minimal", "low", "medium", "high", "xhigh"):
            assert tlm.get(level) is None, f"{mid}.{level}"


def test_glm_4_5_air_is_only_model_without_tool_stream():
    """glm-4.5-air is the only model without zaiToolStream support."""
    for mid, m in ZHIPU_MODELS.items():
        if mid == "glm-4.5-air":
            assert m.compat.get("zai_tool_stream") is False
        else:
            assert m.compat.get("zai_tool_stream") is True, mid


def test_glm_5v_turbo_supports_image_input():
    assert "image" in ZHIPU_MODELS["glm-5v-turbo"].input
    # All other GLM models are text-only.
    for mid, m in ZHIPU_MODELS.items():
        if mid != "glm-5v-turbo":
            assert m.input == ["text"], mid


def test_glm_5_2_has_million_token_context():
    """glm-5.2 stands alone at 1M context; others are 128k-200k."""
    assert ZHIPU_MODELS["glm-5.2"].context_window == 1_000_000
    assert ZHIPU_MODELS["glm-4.5-air"].context_window == 131_072


def test_all_glm_costs_are_zero():
    """Coding Plan is a flat subscription — token prices aren't meaningful."""
    for mid, m in ZHIPU_MODELS.items():
        assert m.cost.input == 0, mid
        assert m.cost.output == 0, mid


def test_deepseek_models_still_use_deepseek_format():
    """Regression: DeepSeek's thinking_format must remain 'deepseek', not 'zai'."""
    for mid, m in DEEPSEEK_MODELS.items():
        assert m.compat.get("thinking_format") == "deepseek", mid
        assert "zai_tool_stream" not in (m.compat or {}), mid
