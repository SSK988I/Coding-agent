"""Shared pytest fixtures for coding-agent.

The single most important job here is the autouse ``isolate_agent_dir``
fixture: it forces every test to use a throwaway config/auth/session
directory instead of the user's real ``~/.coding-agent/``.

WHY THIS EXISTS
---------------
``coding_agent.modes.interactive.interactive_mode`` binds a module-level
constant ``AUTH_FILE = get_auth_path()`` at import time, and ``_save_key_for``
writes the live API-key file via ``AUTH_FILE.write_text(...)``. Before this
fixture existed, any test that exercised the login/submit path without
individually redirecting ``AUTH_FILE`` (e.g. ``test_editor_swap_no_stacking``
submitting the placeholder string ``"the-key"``) corrupted the developer's
REAL ``~/.coding-agent/auth.json``, silently replacing their DeepSeek key with
``the-key`` and causing mysterious 401s in the live agent on the next run.

The fix is global rather than per-test: redirecting once here means future
tests cannot repeat the accident even if their author forgets to monkeypatch.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_agent_dir(monkeypatch, tmp_path):
    """Point every config path at a per-test tmp dir.

    Redirects the env var ``CODING_AGENT_HOME`` (read by
    ``core.config.get_agent_dir``) AND the already-imported
    ``interactive_mode.AUTH_FILE`` / ``cli.main`` symbols, because the latter
    are bound at import time and won't re-read the env var.

    Scope: function (default). Each test gets its own clean dir.
    """
    fake_dir = tmp_path / "fake-agent-dir"
    fake_dir.mkdir(parents=True, exist_ok=True)

    # 1. Env var: anything that reads config at call time (the live resolvers
    #    in core.config, session storage, etc.) sees the fake dir.
    monkeypatch.setenv("CODING_AGENT_HOME", str(fake_dir))

    # 2. Already-bound module constants. These were resolved at import from
    #    the REAL home, so env-var redirection alone does not move them.
    import coding_agent.modes.interactive.interactive_mode as imod
    import coding_agent.core.config as config

    fake_auth = fake_dir / "auth.json"
    # Seed with placeholder keys for both built-in providers so that
    # ``_available_models()`` (which only lists providers the user has
    # configured) returns a non-empty list. Tests that don't care about auth
    # (e.g. the editor-swap tree-invariant tests) still see models; tests that
    # DO care about auth resolution overwrite this file or monkeypatch further.
    # Using a clearly-fake placeholder makes any accidental leak obvious.
    import json as _json
    fake_auth.write_text(_json.dumps({
        "deepseek": {"type": "api_key", "key": "test-placeholder-do-not-use"},
        "zai-coding-cn": {"type": "api_key", "key": "test-placeholder-do-not-use"},
    }), encoding="utf-8")
    monkeypatch.setattr(imod, "AUTH_FILE", fake_auth)
    # config.get_auth_path() is also called directly in some paths — keep it
    # consistent by pointing the function's result at the same file.
    monkeypatch.setattr(config, "get_auth_path", lambda: fake_auth)
    monkeypatch.setattr(config, "get_agent_dir", lambda: fake_dir)
    monkeypatch.setattr(config, "get_sessions_dir", lambda: fake_dir / "sessions")

    yield
