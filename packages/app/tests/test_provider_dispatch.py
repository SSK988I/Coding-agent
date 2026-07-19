"""Tests for ``_select_provider``, ``_provider_env_var``, and
``_resolve_env_api_key`` in cli/main.py.

These are the helpers that decoupled the CLI from its DeepSeek hard-coding.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_llm import deepseek_provider, zhipu_provider
from coding_agent.cli.main import (
    _provider_env_var,
    _resolve_env_api_key,
    _select_provider,
)


def _args(provider: str | None) -> SimpleNamespace:
    return SimpleNamespace(provider=provider)


# ─── _select_provider ────────────────────────────────────────────────────


@pytest.mark.parametrize("alias", ["zhipu", "Zhipu", "GLM", "glm", "zai", "zai-coding-cn", "ZAI-CODING-CN"])
def test_zhipu_aliases_resolve_to_zhipu_provider(alias):
    p = _select_provider(_args(alias))
    assert p.id == "zai-coding-cn"
    assert p.base_url == "https://open.bigmodel.cn/api/coding/paas/v4"


def test_deepseek_is_default_when_provider_none():
    p = _select_provider(_args(None))
    assert p.id == "deepseek"


def test_explicit_deepseek_keyword_returns_deepseek():
    p = _select_provider(_args("deepseek"))
    assert p.id == "deepseek"


def test_unknown_provider_is_rejected(capsys):
    """Provider 名称拼写错误时不能静默请求其他 Provider。"""
    with pytest.raises(SystemExit) as exc:
        _select_provider(_args("some-future-provider"))
    assert exc.value.code == 2
    assert "未知 Provider" in capsys.readouterr().err


def test_provider_arg_is_case_insensitive_and_trimmed():
    assert _select_provider(_args("  ZHIPU  ")).id == "zai-coding-cn"
    assert _select_provider(_args("  Deepseek  ")).id == "deepseek"


# ─── _provider_env_var ───────────────────────────────────────────────────


def test_deepseek_env_var_is_deepseek_api_key():
    assert _provider_env_var(deepseek_provider()) == "DEEPSEEK_API_KEY"


def test_zhipu_env_var_returns_canonical_first_name():
    """First env var in the list is the canonical ZAI_CODING_CN_API_KEY."""
    assert _provider_env_var(zhipu_provider()) == "ZAI_CODING_CN_API_KEY"


def test_provider_env_var_returns_none_when_auth_lacks_env_vars():
    """Defensive: a provider without env_vars (e.g. OAuth-only) returns None."""
    class FakeAuth:
        pass

    class FakeProvider:
        auth = FakeAuth()

    assert _provider_env_var(FakeProvider()) is None


# ─── _resolve_env_api_key ────────────────────────────────────────────────


def test_resolve_env_api_key_reads_first_advertised_var(monkeypatch):
    monkeypatch.setenv("ZAI_CODING_CN_API_KEY", "primary-key")
    monkeypatch.setenv("ZHIPU_API_KEY", "alias-key")
    assert _resolve_env_api_key(zhipu_provider()) == "primary-key"


def test_resolve_env_api_key_falls_back_to_aliases(monkeypatch):
    """When the canonical env var is unset, fall back to ZHIPU_API_KEY / GLM_API_KEY."""
    monkeypatch.delenv("ZAI_CODING_CN_API_KEY", raising=False)
    monkeypatch.setenv("GLM_API_KEY", "short-alias-key")
    assert _resolve_env_api_key(zhipu_provider()) == "short-alias-key"


def test_resolve_env_api_key_returns_none_when_all_unset(monkeypatch):
    for v in ("ZAI_CODING_CN_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    assert _resolve_env_api_key(zhipu_provider()) is None


def test_resolve_env_api_key_ignores_empty_string(monkeypatch):
    """Empty env var values are treated as unset — keep searching aliases."""
    monkeypatch.setenv("ZAI_CODING_CN_API_KEY", "")
    monkeypatch.setenv("ZHIPU_API_KEY", "real")
    assert _resolve_env_api_key(zhipu_provider()) == "real"


def test_resolve_env_api_key_for_deepseek(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    assert _resolve_env_api_key(deepseek_provider()) == "ds-key"
