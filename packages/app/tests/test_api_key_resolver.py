"""Tests for the per-provider live API key resolver.

Regression guard for a bug where the ``config.get_api_key`` closure captured
``stored_key`` / ``env_key`` at CLI launch time and returned that snapshot
regardless of which ``provider_id`` it was called with. The symptom:
launch in a DeepSeek session → /login GLM → switch to glm-5.1 → next prompt
sent the **DeepSeek** key to Zhipu, which rejected it with 401
"Authentication Fails, Your api key: ****-key is invalid".

The fix: ``_resolve_api_key_for(provider_id)`` reads auth.json and the
provider's advertised env vars **every call**, and the launch-time closure
just delegates to it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coding_agent.cli.main import _resolve_api_key_for


def _write_auth(tmp_path: Path, data: dict) -> Path:
    """Write a fake auth.json and return its path."""
    p = tmp_path / "auth.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _patch_auth(monkeypatch, auth_path: Path) -> None:
    """Point both cli.main and core.config at a fake auth.json path."""
    monkeypatch.setattr(
        "coding_agent.cli.main.get_auth_path", lambda: auth_path
    )
    monkeypatch.setattr(
        "coding_agent.core.config.get_auth_path", lambda: auth_path
    )


def _clear_env(monkeypatch):
    for v in ("DEEPSEEK_API_KEY", "ZAI_CODING_CN_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)


# ─── per-provider resolution ─────────────────────────────────────────────


def test_resolver_returns_per_provider_stored_key(monkeypatch, tmp_path):
    """Each provider gets its own stored key, not the launch-time snapshot."""
    _clear_env(monkeypatch)
    auth = _write_auth(tmp_path, {
        "deepseek": {"type": "api_key", "key": "sk-ds-AAA"},
        "zai-coding-cn": {"type": "api_key", "key": "glm-BBB-XYZ"},
    })
    _patch_auth(monkeypatch, auth)

    assert _resolve_api_key_for("deepseek") == "sk-ds-AAA"
    assert _resolve_api_key_for("zai-coding-cn") == "glm-BBB-XYZ"


def test_resolver_returns_none_when_provider_has_no_key(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    auth = _write_auth(tmp_path, {
        "deepseek": {"type": "api_key", "key": "sk-ds-AAA"},
    })
    _patch_auth(monkeypatch, auth)

    assert _resolve_api_key_for("deepseek") == "sk-ds-AAA"
    assert _resolve_api_key_for("zai-coding-cn") is None


def test_resolver_returns_none_for_unknown_provider(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    _patch_auth(monkeypatch, _write_auth(tmp_path, {}))
    assert _resolve_api_key_for("no-such-provider") is None


# ─── the bug: post-/login switch picks up the new key live ──────────────


def test_resolver_picks_up_newly_stored_key_without_restart(monkeypatch, tmp_path):
    """Adding a key to auth.json mid-session is visible on the next call.

    This is the exact scenario: launch with only DeepSeek configured, then
    /login GLM. The GLM session's next prompt must see the new GLM key,
    not None.
    """
    _clear_env(monkeypatch)
    auth = _write_auth(tmp_path, {
        "deepseek": {"type": "api_key", "key": "sk-ds"},
    })
    _patch_auth(monkeypatch, auth)

    # Launch state: GLM has no key.
    assert _resolve_api_key_for("zai-coding-cn") is None

    # User runs /login GLM mid-session — auth.json is rewritten.
    auth.write_text(json.dumps({
        "deepseek": {"type": "api_key", "key": "sk-ds"},
        "zai-coding-cn": {"type": "api_key", "key": "glm-new"},
    }), encoding="utf-8")

    # Next call must see the new key without any restart.
    assert _resolve_api_key_for("zai-coding-cn") == "glm-new"


def test_resolver_picks_up_removed_key_without_restart(monkeypatch, tmp_path):
    """Symmetric: /logout removes a key → next call sees None."""
    _clear_env(monkeypatch)
    auth = _write_auth(tmp_path, {
        "deepseek": {"type": "api_key", "key": "sk-ds"},
        "zai-coding-cn": {"type": "api_key", "key": "glm"},
    })
    _patch_auth(monkeypatch, auth)
    assert _resolve_api_key_for("zai-coding-cn") == "glm"

    # /logout GLM
    auth.write_text(json.dumps({
        "deepseek": {"type": "api_key", "key": "sk-ds"},
    }), encoding="utf-8")
    assert _resolve_api_key_for("zai-coding-cn") is None


# ─── env var fallback ────────────────────────────────────────────────────


def test_resolver_falls_back_to_env_var(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    _patch_auth(monkeypatch, _write_auth(tmp_path, {}))
    monkeypatch.setenv("ZHIPU_API_KEY", "from-env")
    assert _resolve_api_key_for("zai-coding-cn") == "from-env"


def test_resolver_falls_back_to_any_advertised_alias(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    _patch_auth(monkeypatch, _write_auth(tmp_path, {}))
    # GLM advertises ZAI_CODING_CN_API_KEY, ZHIPU_API_KEY, GLM_API_KEY.
    monkeypatch.setenv("GLM_API_KEY", "alias-key")
    assert _resolve_api_key_for("zai-coding-cn") == "alias-key"


def test_resolver_stored_key_takes_precedence_over_env(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    auth = _write_auth(tmp_path, {
        "deepseek": {"type": "api_key", "key": "from-auth-json"},
    })
    _patch_auth(monkeypatch, auth)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    assert _resolve_api_key_for("deepseek") == "from-auth-json"


# ─── --api-key override ─────────────────────────────────────────────────


def test_resolver_override_pins_single_key_for_all_providers(monkeypatch, tmp_path):
    """``--api-key`` flag forces the same key for every provider.

    When --api-key is set, it takes precedence
    over auth.json AND env for ALL providers (not just the launch one). Useful
    for one-off runs against a specific key without polluting auth.json.
    """
    _clear_env(monkeypatch)
    auth = _write_auth(tmp_path, {
        "deepseek": {"type": "api_key", "key": "stored-ds"},
        "zai-coding-cn": {"type": "api_key", "key": "stored-glm"},
    })
    _patch_auth(monkeypatch, auth)

    override = "pinned-by-flag"
    assert _resolve_api_key_for("deepseek", override=override) == override
    assert _resolve_api_key_for("zai-coding-cn", override=override) == override
    assert _resolve_api_key_for("unknown", override=override) == override


def test_resolver_override_empty_string_falls_through(monkeypatch, tmp_path):
    """An empty --api-key is treated as unset (falls back to normal resolution)."""
    _clear_env(monkeypatch)
    auth = _write_auth(tmp_path, {
        "deepseek": {"type": "api_key", "key": "stored-ds"},
    })
    _patch_auth(monkeypatch, auth)
    # Empty override is falsy → normal resolution applies.
    assert _resolve_api_key_for("deepseek", override="") == "stored-ds"


# ─── launch-time closure delegates correctly ────────────────────────────


def test_launch_closure_delegates_per_call(monkeypatch, tmp_path):
    """The closure built in main() must call _resolve_api_key_for per call.

    Builds the same closure shape main() builds and verifies it returns
    per-provider keys, not the launch-time snapshot.
    """
    _clear_env(monkeypatch)
    auth = _write_auth(tmp_path, {
        "deepseek": {"type": "api_key", "key": "ds-launch"},
        "zai-coding-cn": {"type": "api_key", "key": "glm-launch"},
    })
    _patch_auth(monkeypatch, auth)

    override_key = None  # no --api-key
    def _get_api_key(provider_id):
        return _resolve_api_key_for(provider_id, override=override_key)

    # Called from agent_loop with different provider_ids — must return each
    # provider's own key, not the launch provider's.
    assert _get_api_key("deepseek") == "ds-launch"
    assert _get_api_key("zai-coding-cn") == "glm-launch"

    # And it must stay live: rewrite auth.json, no new closure.
    auth.write_text(json.dumps({
        "deepseek": {"type": "api_key", "key": "ds-launch"},
        "zai-coding-cn": {"type": "api_key", "key": "glm-rotated"},
    }), encoding="utf-8")
    assert _get_api_key("zai-coding-cn") == "glm-rotated"
