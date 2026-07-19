"""Multi-provider helpers.

Centralizes:
  * ``DEFAULT_MODEL_PER_PROVIDER`` — which model id to pick after authenticating
    against a provider.
  * ``ALL_PROVIDER_FACTORIES``    — ordered list of every built-in provider
    factory. Used by /login, /logout, /model to enumerate options without
    each call site hard-coding the same imports.
  * ``get_all_models``            — every model from every registered
    provider, flattened. The /model selector lists these so users can pick
    across configured providers.
  * ``get_configured_models``     — subset of ``get_all_models`` filtered to
    providers the user can actually call (has a stored key OR env var).
  * ``get_default_model_for_provider`` — convenience lookup with fallback.

This module exists because Zhipu GLM support exposed two coupled bugs:
/login didn't switch to the freshly-authenticated provider, and /model
only listed one provider's models. Both fixes needed the same shared
notion of "what providers exist and what's their default model".
"""
from __future__ import annotations

from typing import Callable

from agent_llm import Provider
from agent_llm.types import Model


#: Which model to auto-select when a provider is freshly authenticated.
#: Only the providers we ship are listed; adding a new provider means
#: adding one line here.
DEFAULT_MODEL_PER_PROVIDER: dict[str, str] = {
    "deepseek": "deepseek-v4-pro",
    "zai-coding-cn": "glm-5.1",
}


#: Ordered list of every built-in provider factory. Add new providers here
#: and everything else (/login, /logout, /model, --list-models) picks them up.
def _all_factories() -> list[Callable[[], Provider]]:
    from agent_llm import deepseek_provider, zhipu_provider
    return [deepseek_provider, zhipu_provider]


ALL_PROVIDER_FACTORIES = _all_factories()


def _all_providers() -> list[Provider]:
    """Instantiate every built-in provider, skipping any that fail to build."""
    out: list[Provider] = []
    for factory in ALL_PROVIDER_FACTORIES:
        try:
            out.append(factory())
        except Exception:
            continue
    return out


def get_all_models() -> list[Model]:
    """Every model from every built-in provider, in registration order.

    This is the unfiltered model catalog. Authentication filtering is applied
    separately via :func:`get_configured_models` when needed.
    """
    models: list[Model] = []
    for p in _all_providers():
        try:
            models.extend(p.get_models())
        except Exception:
            continue
    return models


def provider_is_configured(provider: Provider, *, stored_keys: dict, env: dict[str, str]) -> bool:
    """True if the user can actually call this provider right now.

    A provider is callable when EITHER a credential is stored for it OR any
    of its advertised env vars is set. Mirrors the auth resolution in
    ``agent_llm.auth.resolve`` without needing to actually resolve.
    """
    if provider.id in stored_keys:
        cred = stored_keys[provider.id]
        if isinstance(cred, dict) and cred.get("key"):
            return True
    try:
        for name in provider.auth.api_key.env_vars:
            if env.get(name):
                return True
    except AttributeError:
        pass
    return False


def get_configured_models(*, stored_keys: dict, env: dict[str, str]) -> list[Model]:
    """Subset of :func:`get_all_models` restricted to callable providers.

    Use this when listing models the user can actually run (e.g. /model's
    "switch to this model" semantics require the destination provider's
    auth to be set).
    """
    configured_ids = {
        p.id for p in _all_providers()
        if provider_is_configured(p, stored_keys=stored_keys, env=env)
    }
    return [m for m in get_all_models() if m.provider in configured_ids]


def get_default_model_for_provider(provider_id: str) -> Model | None:
    """Return the default model for ``provider_id`` after authentication.

    Falls back to the provider's first listed model if no explicit default
    is configured.
    """
    models = [m for m in get_all_models() if m.provider == provider_id]
    if not models:
        return None
    default_id = DEFAULT_MODEL_PER_PROVIDER.get(provider_id)
    if default_id:
        for m in models:
            if m.id == default_id:
                return m
    return models[0]


def get_provider_name(provider_id: str) -> str:
    """Display name for a provider id, falling back to the id itself."""
    for p in _all_providers():
        if p.id == provider_id:
            return p.name
    return provider_id
