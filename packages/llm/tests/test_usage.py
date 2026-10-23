from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_llm.api.deepseek_responses import parse_usage
from agent_llm.api.openai_completions import parse_chunk_usage
from agent_llm.providers.deepseek_models import DEEPSEEK_MODELS
from agent_llm.types import Usage


@pytest.mark.parametrize("cached", [0, 40])
def test_completions_cache_reporting_preserves_measured_zero_and_accounting(cached: int) -> None:
    usage = parse_chunk_usage(SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    ), DEEPSEEK_MODELS["deepseek-flash"])

    assert usage.cache_read_reported is True
    assert usage.cache_read == cached
    assert usage.input == 100 - cached
    assert usage.output == 20
    assert usage.total_tokens == 120


@pytest.mark.parametrize("cached", [0, 40])
def test_completions_recognizes_deepseek_cache_field_including_zero(cached: int) -> None:
    usage = parse_chunk_usage(SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        prompt_tokens_details=SimpleNamespace(cached_tokens=None),
        prompt_cache_hit_tokens=cached,
    ), DEEPSEEK_MODELS["deepseek-flash"])

    assert usage.cache_read_reported is True
    assert usage.cache_read == cached
    assert usage.input + usage.cache_read == 100


@pytest.mark.parametrize("details", [None, SimpleNamespace(), SimpleNamespace(cached_tokens=None)])
def test_completions_missing_cache_breakdown_is_unknown_not_measured_zero(details) -> None:
    usage = parse_chunk_usage(SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        prompt_tokens_details=details,
    ), DEEPSEEK_MODELS["deepseek-flash"])

    assert usage.cache_read_reported is False
    assert usage.cache_read == 0
    assert usage.input == 100
    assert usage.total_tokens == 120


@pytest.mark.parametrize("cached", [0, 40])
def test_responses_cache_reporting_preserves_measured_zero_and_accounting(cached: int) -> None:
    usage = parse_usage(SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        input_tokens_details=SimpleNamespace(cached_tokens=cached),
        output_tokens_details=SimpleNamespace(reasoning_tokens=5),
        total_tokens=120,
    ), DEEPSEEK_MODELS["deepseek-flash"])

    assert usage.cache_read_reported is True
    assert usage.cache_read == cached
    assert usage.input == 100 - cached
    assert usage.output == 20
    assert usage.reasoning == 5
    assert usage.total_tokens == 120


@pytest.mark.parametrize("details", [None, SimpleNamespace(), SimpleNamespace(cached_tokens=None)])
def test_responses_missing_cache_breakdown_is_unknown_not_measured_zero(details) -> None:
    usage = parse_usage(SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        input_tokens_details=details,
    ), DEEPSEEK_MODELS["deepseek-flash"])

    assert usage.cache_read_reported is False
    assert usage.cache_read == 0
    assert usage.input == 100
    assert usage.total_tokens == 120


def test_absent_raw_usage_does_not_report_measured_cache_zero() -> None:
    model = DEEPSEEK_MODELS["deepseek-flash"]
    assert parse_usage(None, model).cache_read_reported is False
    assert parse_chunk_usage(None, model).cache_read_reported is False
    assert Usage().cache_read_reported is None
