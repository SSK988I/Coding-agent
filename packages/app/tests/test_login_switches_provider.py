"""Tests for /login → auto-switch-provider and /model cross-provider listing.

Two coupled bugs fixed together (the user reported them as one):
  1. /login to a different provider didn't switch the session — the freshly-
     authenticated provider's default model was never applied. Verifies the
     provider-switching path.
  2. /model listed only the current session's provider's models, so users
     couldn't switch providers from /model. Verifies the configured-provider filter.

Both fixes share the new ``coding_agent.core.providers`` module, which
centralizes the provider catalog so /login, /logout, /model all see the
same set.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_llm import Model, ModelCost
from agent_core import SessionManager

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.providers import (
    ALL_PROVIDER_FACTORIES,
    DEFAULT_MODEL_PER_PROVIDER,
    get_all_models,
    get_configured_models,
    get_default_model_for_provider,
    get_provider_name,
    provider_is_configured,
)
from coding_agent.modes.interactive.interactive_mode import InteractiveMode


def _model(provider: str, mid: str = "m") -> Model:
    return Model(
        id=mid, provider=provider, reasoning=True, context_window=1000,
        thinking_level_map={},
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )


def _make_mode(provider: str = "deepseek", mid: str = "deepseek-v4-flash"):
    sm = SessionManager.create(cwd=".", in_memory=True)
    session = AgentSession(AgentSessionConfig(model=_model(provider, mid), cwd=".", session_manager=sm))
    return InteractiveMode(session)


# ─── core/providers ─────────────────────────────────────────────────────


def test_default_model_per_provider_has_both_builtins():
    assert "deepseek" in DEFAULT_MODEL_PER_PROVIDER
    assert "zai-coding-cn" in DEFAULT_MODEL_PER_PROVIDER


def test_default_model_for_deepseek_is_v4_pro():
    m = get_default_model_for_provider("deepseek")
    assert m is not None
    assert m.id == "deepseek-v4-pro"


def test_default_model_for_zhipu_is_glm_5_1():
    """The default Z.AI model is glm-5.1."""
    m = get_default_model_for_provider("zai-coding-cn")
    assert m is not None
    assert m.id == "glm-5.1"


def test_default_model_for_unknown_provider_returns_none():
    assert get_default_model_for_provider("no-such-provider") is None


def test_get_all_models_lists_every_model_from_every_provider():
    ids = {m.id for m in get_all_models()}
    # 2 DeepSeek + 6 GLM = 8 models total.
    assert ids == {
        "deepseek-v4-flash", "deepseek-v4-pro",
        "glm-4.5-air", "glm-4.7", "glm-5-turbo", "glm-5.1", "glm-5.2", "glm-5v-turbo",
    }


def test_get_all_models_preserves_provider_grouping():
    """DeepSeek models come first (factory registered first), then GLM."""
    models = get_all_models()
    providers_in_order = [m.provider for m in models]
    # All deepseek entries appear before all zai-coding-cn entries.
    ds = [i for i, p in enumerate(providers_in_order) if p == "deepseek"]
    glm = [i for i, p in enumerate(providers_in_order) if p == "zai-coding-cn"]
    assert max(ds) < min(glm)


def test_get_provider_name_returns_display_name():
    assert get_provider_name("deepseek") == "DeepSeek"
    assert get_provider_name("zai-coding-cn") == "Z.AI Coding CN"


def test_get_provider_name_falls_back_to_id():
    assert get_provider_name("unknown") == "unknown"


def test_all_provider_factories_includes_both():
    providers = [f().id for f in ALL_PROVIDER_FACTORIES]
    assert "deepseek" in providers
    assert "zai-coding-cn" in providers


# ─── provider_is_configured + get_configured_models ─────────────────────


def test_provider_is_configured_with_stored_key():
    from agent_llm import deepseek_provider
    p = deepseek_provider()
    assert provider_is_configured(
        p,
        stored_keys={"deepseek": {"type": "api_key", "key": "x"}},
        env={},
    )


def test_provider_is_configured_with_env_var():
    from agent_llm import zhipu_provider
    p = zhipu_provider()
    # Any of the three advertised env var aliases counts.
    assert provider_is_configured(p, stored_keys={}, env={"GLM_API_KEY": "x"})
    assert provider_is_configured(p, stored_keys={}, env={"ZHIPU_API_KEY": "x"})
    assert provider_is_configured(p, stored_keys={}, env={"ZAI_CODING_CN_API_KEY": "x"})


def test_provider_is_configured_false_when_nothing_set():
    from agent_llm import deepseek_provider
    p = deepseek_provider()
    assert not provider_is_configured(p, stored_keys={}, env={})


def test_provider_is_configured_ignores_empty_env_value():
    from agent_llm import deepseek_provider
    p = deepseek_provider()
    assert not provider_is_configured(p, stored_keys={}, env={"DEEPSEEK_API_KEY": ""})


def test_get_configured_models_filters_unconfigured():
    """Only models whose provider has auth should appear."""
    # No auth anywhere → empty list.
    models = get_configured_models(stored_keys={}, env={})
    assert models == []

    # GLM env var set → only GLM models.
    models = get_configured_models(stored_keys={}, env={"ZHIPU_API_KEY": "x"})
    assert all(m.provider == "zai-coding-cn" for m in models)
    assert len(models) == 6

    # Both configured → all 8.
    models = get_configured_models(
        stored_keys={"deepseek": {"type": "api_key", "key": "d"}},
        env={"ZHIPU_API_KEY": "g"},
    )
    assert len(models) == 8


# ─── Bug 1: /login switches to authenticated provider's default ─────────


def test_login_to_glm_switches_session_to_glm_5_1(monkeypatch, tmp_path):
    """Logging in to Zhipu should auto-switch the session to glm-5.1."""
    import json

    for v in ("DEEPSEEK_API_KEY", "ZAI_CODING_CN_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)

    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")
    import coding_agent.modes.interactive.interactive_mode as imod
    monkeypatch.setattr(imod, "AUTH_FILE", auth_file)

    mode = _make_mode(provider="deepseek", mid="deepseek-v4-flash")
    assert mode._session.model.provider == "deepseek"

    mode._open_login_dialog()
    mode._current_selector._on_select("zai-coding-cn")
    mode._current_selector._on_submit_outer("glm-key")

    assert mode._session.model.provider == "zai-coding-cn"
    assert mode._session.model.id == "glm-5.1"
    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert saved["zai-coding-cn"]["key"] == "glm-key"


def test_login_to_same_provider_keeps_current_model(monkeypatch, tmp_path):
    """Re-authenticating the current provider should NOT swap models."""

    for v in ("DEEPSEEK_API_KEY", "ZAI_CODING_CN_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)

    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")
    import coding_agent.modes.interactive.interactive_mode as imod
    monkeypatch.setattr(imod, "AUTH_FILE", auth_file)

    mode = _make_mode(provider="deepseek", mid="deepseek-v4-flash")
    mode._open_login_dialog()
    mode._current_selector._on_select("deepseek")
    mode._current_selector._on_submit_outer("new-ds-key")

    # Still on deepseek-v4-flash (NOT switched to default deepseek-v4-pro).
    assert mode._session.model.provider == "deepseek"
    assert mode._session.model.id == "deepseek-v4-flash"


def test_login_to_deepseek_switches_from_glm(monkeypatch, tmp_path):
    """Reverse direction: starting in a GLM session, /login to DeepSeek switches back."""

    for v in ("DEEPSEEK_API_KEY", "ZAI_CODING_CN_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)

    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")
    import coding_agent.modes.interactive.interactive_mode as imod
    monkeypatch.setattr(imod, "AUTH_FILE", auth_file)

    mode = _make_mode(provider="zai-coding-cn", mid="glm-5.2")
    mode._open_login_dialog()
    mode._current_selector._on_select("deepseek")
    mode._current_selector._on_submit_outer("ds-key")

    assert mode._session.model.provider == "deepseek"
    assert mode._session.model.id == "deepseek-v4-pro"  # DS default


# ─── /model lists configured providers only ───────────────────────────────


def _configure_only_deepseek(monkeypatch, mode):
    """Test helper: clear all auth, then set up DeepSeek-only state."""
    for v in ("DEEPSEEK_API_KEY", "ZAI_CODING_CN_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    mode._credentials = {"deepseek": {"type": "api_key", "key": "ds-key"}}


def _configure_only_glm(monkeypatch, mode):
    for v in ("DEEPSEEK_API_KEY", "ZAI_CODING_CN_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    mode._credentials = {"zai-coding-cn": {"type": "api_key", "key": "glm-key"}}


def _configure_both(monkeypatch, mode):
    for v in ("DEEPSEEK_API_KEY", "ZAI_CODING_CN_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    mode._credentials = {
        "deepseek": {"type": "api_key", "key": "ds-key"},
        "zai-coding-cn": {"type": "api_key", "key": "glm-key"},
    }


def _configure_none(monkeypatch, mode):
    for v in ("DEEPSEEK_API_KEY", "ZAI_CODING_CN_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    mode._credentials = {}


def test_model_selector_only_configured_provider_shows(monkeypatch):
    """Only DeepSeek configured → /model shows DeepSeek models only (no GLM).

    Unconfigured providers are
    hidden so users can't switch to models that will fail without an API key.
    """
    mode = _make_mode(provider="deepseek", mid="deepseek-v4-flash")
    _configure_only_deepseek(monkeypatch, mode)
    mode._open_model_selector("")
    ids = {m.id for m in mode._current_selector._models}
    assert ids == {"deepseek-v4-flash", "deepseek-v4-pro"}
    assert "glm-5.2" not in ids


def test_model_selector_only_glm_configured_shows_only_glm(monkeypatch):
    """Reverse: only GLM configured → /model shows only GLM's 6 models."""
    mode = _make_mode(provider="zai-coding-cn", mid="glm-5.1")
    _configure_only_glm(monkeypatch, mode)
    mode._open_model_selector("")
    ids = {m.id for m in mode._current_selector._models}
    assert ids == {"glm-4.5-air", "glm-4.7", "glm-5-turbo", "glm-5.1", "glm-5.2", "glm-5v-turbo"}
    assert "deepseek-v4-flash" not in ids


def test_model_selector_both_configured_shows_all(monkeypatch):
    """Both providers configured → /model shows all 8 models."""
    mode = _make_mode(provider="deepseek", mid="deepseek-v4-flash")
    _configure_both(monkeypatch, mode)
    mode._open_model_selector("")
    ids = {m.id for m in mode._current_selector._models}
    assert len(ids) == 8


def test_model_selector_no_auth_shows_nothing_with_hint(monkeypatch):
    """Nothing configured → /model shows a "use /login" hint, no selector."""
    mode = _make_mode(provider="deepseek", mid="deepseek-v4-flash")
    _configure_none(monkeypatch, mode)
    mode._open_model_selector("")
    # No selector mounted; the system message guides the user to /login.
    assert mode._current_selector is None or not mode._current_selector.focused
    # (The hint fires via _add_system_message, which appends to chat. We
    # just assert no selector was mounted — the message text isn't easily
    # inspectable without the full TUI running.)


def test_model_selector_env_var_alone_counts_as_configured(monkeypatch):
    """A provider with only an env var (no /login) should also be listed."""
    mode = _make_mode(provider="deepseek", mid="deepseek-v4-flash")
    _configure_none(monkeypatch, mode)
    monkeypatch.setenv("ZHIPU_API_KEY", "from-env")
    mode._open_model_selector("")
    ids = {m.id for m in mode._current_selector._models}
    # GLM is configured via env, DeepSeek is not.
    assert ids == {"glm-4.5-air", "glm-4.7", "glm-5-turbo", "glm-5.1", "glm-5.2", "glm-5v-turbo"}


def test_model_selector_can_switch_to_other_configured_provider(monkeypatch):
    """With both configured, picking a GLM model switches the session."""
    mode = _make_mode(provider="deepseek", mid="deepseek-v4-flash")
    _configure_both(monkeypatch, mode)
    mode._open_model_selector("")
    mode._current_selector._on_select("glm-5.2")

    assert mode._session.model.provider == "zai-coding-cn"
    assert mode._session.model.id == "glm-5.2"


def test_model_selector_can_switch_back_to_deepseek(monkeypatch):
    """Starting from GLM, /model can switch to a DeepSeek model (both configured)."""
    mode = _make_mode(provider="zai-coding-cn", mid="glm-5.1")
    _configure_both(monkeypatch, mode)
    mode._open_model_selector("")
    mode._current_selector._on_select("deepseek-v4-pro")

    assert mode._session.model.provider == "deepseek"
    assert mode._session.model.id == "deepseek-v4-pro"


def test_model_selector_cannot_switch_to_unconfigured_provider(monkeypatch):
    """Picking an unconfigured provider's model via exact-match is a no-op.

    This guards the /model <id> shortcut: if the user types /model glm-5.2
    but hasn't configured GLM, the switch silently doesn't happen (matches
    the availability filter runs before model resolution).
    """
    mode = _make_mode(provider="deepseek", mid="deepseek-v4-flash")
    _configure_only_deepseek(monkeypatch, mode)
    mode._open_model_selector("glm-5.2")
    # glm-5.2 isn't in the configured list, so exact-match fails → selector
    # opens instead of switching.
    from coding_agent.modes.interactive.components.model_selector import ModelSelectorComponent
    assert isinstance(mode._current_selector, ModelSelectorComponent)
    # Session unchanged.
    assert mode._session.model.provider == "deepseek"
    assert mode._session.model.id == "deepseek-v4-flash"


def test_model_selector_exact_match_switches_when_configured(monkeypatch):
    """``/model glm-5.2`` switches directly when GLM IS configured."""
    mode = _make_mode(provider="deepseek", mid="deepseek-v4-flash")
    _configure_both(monkeypatch, mode)
    mode._open_model_selector("glm-5.2")
    from coding_agent.modes.interactive.components.model_selector import ModelSelectorComponent
    assert not isinstance(mode._current_selector, ModelSelectorComponent)
    assert mode._session.model.id == "glm-5.2"
    assert mode._session.model.provider == "zai-coding-cn"


def test_model_selector_filter_only_sees_configured(monkeypatch):
    """The filter list reflects configured models, not all built-in models.

    With only DeepSeek configured, filtering by "glm" returns nothing —
    the user can't see GLM models they can't actually run.
    """
    mode = _make_mode(provider="deepseek", mid="deepseek-v4-flash")
    _configure_only_deepseek(monkeypatch, mode)
    mode._open_model_selector("")
    selector = mode._current_selector
    selector._input.set_value("glm")
    selector._apply_filter()
    visible_ids = {item.value for item in selector._list.items}
    assert visible_ids == set()  # No GLM models visible without GLM auth.
