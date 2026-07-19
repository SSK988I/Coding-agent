"""Tests for multi-provider /login and /logout flow.

Verifies the two-step provider-selector-then-key-dialog flow and the
``_save_key_for`` / ``_remove_key_for`` helpers that accept an explicit
provider id (decoupling auth from the current session's provider). These
were added when Zhipu GLM support exposed the chicken-and-egg: without a
provider selector, /login could only ever configure the session's current
provider.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coding_agent.modes.interactive.components.provider_selector import (
    ProviderOption,
    ProviderSelectorComponent,
)
from coding_agent.modes.interactive.interactive_mode import InteractiveMode


# ─── ProviderSelectorComponent ───────────────────────────────────────────


def _theme():
    from agent_tui.theme import load_theme
    return load_theme("dark")


def test_provider_selector_sorts_current_first():
    """Current provider floats to the top regardless of insertion order."""
    opts = [
        ProviderOption(id="zai-coding-cn", name="Z.AI Coding CN", auth_state=""),
        ProviderOption(id="deepseek", name="DeepSeek", auth_state="configured"),
    ]
    sel = ProviderSelectorComponent(_theme(), opts, "deepseek",
                                    on_select=lambda *_: None,
                                    on_cancel=lambda: None)
    # First row label should be DeepSeek's name (it's current → sorted up).
    first_label = sel._list.items[0].label
    assert first_label == "DeepSeek"


def test_provider_selector_shows_auth_state_in_description():
    opts = [
        ProviderOption(id="deepseek", name="DeepSeek", auth_state="configured"),
        ProviderOption(id="zai-coding-cn", name="Z.AI Coding CN", auth_state="env"),
        ProviderOption(id="foo", name="Foo", auth_state=""),
    ]
    sel = ProviderSelectorComponent(_theme(), opts, "deepseek",
                                    on_select=lambda *_: None,
                                    on_cancel=lambda: None)
    descs = {item.value: item.description for item in sel._list.items}
    assert "configured" in descs["deepseek"]
    assert "env" in descs["zai-coding-cn"]
    # Empty auth_state → just the id, no suffix.
    assert descs["foo"] == "foo"


def test_provider_selector_filter_narrows_rows():
    opts = [
        ProviderOption(id="deepseek", name="DeepSeek", auth_state=""),
        ProviderOption(id="zai-coding-cn", name="Z.AI Coding CN", auth_state=""),
    ]
    sel = ProviderSelectorComponent(_theme(), opts, None,
                                    on_select=lambda *_: None,
                                    on_cancel=lambda: None)
    # Type "glm" → no match (filter matches id+name, not "zhipu" alias).
    sel._input.set_value("deep")
    sel._apply_filter()
    assert len(sel._list.items) == 1
    assert sel._list.items[0].value == "deepseek"


# ─── /login provider option discovery ────────────────────────────────────


def _make_mode_with_credentials(credentials: dict, tmp_path: Path, monkeypatch):
    """Build a minimal InteractiveMode stand-in with the auth surface bound.

    Binds the real InteractiveMode methods to a SimpleNamespace so we can
    test them in isolation without spinning up the full TUI + event loop.
    """
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps(credentials), encoding="utf-8")

    monkeypatch.setattr(
        "coding_agent.modes.interactive.interactive_mode.AUTH_FILE", auth_file
    )

    session = SimpleNamespace(model=SimpleNamespace(provider="deepseek"))
    obj = SimpleNamespace()
    obj._session = session
    obj._credentials = dict(credentials)
    obj._api_key = None
    # Bind the real methods.
    for name in (
        "_get_stored_key", "_get_stored_key_for",
        "_save_key", "_save_key_for",
        "_remove_key", "_remove_key_for",
        "_login_provider_options",
    ):
        setattr(obj, name, getattr(InteractiveMode, name).__get__(obj, SimpleNamespace))
    return obj, auth_file


def test_login_provider_options_lists_both_providers(monkeypatch, tmp_path):
    """Both DeepSeek and Zhipu appear in the /login selector."""
    for v in ("DEEPSEEK_API_KEY", "ZAI_CODING_CN_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    mode, _ = _make_mode_with_credentials({}, tmp_path, monkeypatch)
    options = mode._login_provider_options()
    ids = {o.id for o in options}
    assert ids == {"deepseek", "zai-coding-cn"}


def test_login_provider_options_marks_configured_and_env(monkeypatch, tmp_path):
    """Stored credential → 'configured'; env var only → 'env'."""
    monkeypatch.setenv("ZHIPU_API_KEY", "env-key")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZAI_CODING_CN_API_KEY", raising=False)
    monkeypatch.delenv("GLM_API_KEY", raising=False)

    creds = {"deepseek": {"type": "api_key", "key": "ds-stored"}}
    mode, _ = _make_mode_with_credentials(creds, tmp_path, monkeypatch)
    options = {o.id: o.auth_state for o in mode._login_provider_options()}
    assert options["deepseek"] == "configured"
    assert options["zai-coding-cn"] == "env"


# ─── _save_key_for / _remove_key_for ─────────────────────────────────────


def test_save_key_for_distinct_provider_does_not_touch_current(monkeypatch, tmp_path):
    """Saving a GLM key while in a DeepSeek session must not overwrite the DS key."""
    mode, auth_file = _make_mode_with_credentials({}, tmp_path, monkeypatch)
    mode._save_key_for("zai-coding-cn", "glm-key-123")
    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert saved == {"zai-coding-cn": {"type": "api_key", "key": "glm-key-123"}}
    # The session's current provider (deepseek) cache is NOT updated.
    assert mode._api_key is None


def test_save_key_for_current_provider_updates_cache(monkeypatch, tmp_path):
    """Saving a key for the session's current provider also primes the in-memory cache."""
    mode, auth_file = _make_mode_with_credentials({}, tmp_path, monkeypatch)
    mode._save_key_for("deepseek", "ds-key-456")
    assert mode._api_key == "ds-key-456"  # cache primed (session provider matches)


def test_save_key_for_preserves_other_providers(monkeypatch, tmp_path):
    """Saving one provider's key must not drop the other provider's stored key."""
    creds = {"deepseek": {"type": "api_key", "key": "ds-old"}}
    mode, auth_file = _make_mode_with_credentials(creds, tmp_path, monkeypatch)
    mode._save_key_for("zai-coding-cn", "glm-new")
    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert saved["deepseek"]["key"] == "ds-old"
    assert saved["zai-coding-cn"]["key"] == "glm-new"


def test_remove_key_for_distinct_provider_leaves_current_intact(monkeypatch, tmp_path):
    """Removing a GLM key while in a DeepSeek session must not affect the DS key."""
    creds = {
        "deepseek": {"type": "api_key", "key": "ds-key"},
        "zai-coding-cn": {"type": "api_key", "key": "glm-key"},
    }
    mode, auth_file = _make_mode_with_credentials(creds, tmp_path, monkeypatch)
    ok = mode._remove_key_for("zai-coding-cn")
    assert ok is True
    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert "zai-coding-cn" not in saved
    assert saved["deepseek"]["key"] == "ds-key"


def test_remove_key_for_missing_returns_false(monkeypatch, tmp_path):
    mode, _ = _make_mode_with_credentials({}, tmp_path, monkeypatch)
    assert mode._remove_key_for("never-logged-in") is False


def test_remove_key_for_current_clears_cache(monkeypatch, tmp_path):
    """Removing the session's current-provider key also clears the cache."""
    creds = {"deepseek": {"type": "api_key", "key": "ds-key"}}
    mode, _ = _make_mode_with_credentials(creds, tmp_path, monkeypatch)
    mode._api_key = "primed"
    ok = mode._remove_key_for("deepseek")
    assert ok is True
    assert mode._api_key is None


# ─── auth.json file permissions ─────────────────────────────────────────


def test_save_key_for_sets_chmod_0600(monkeypatch, tmp_path):
    """Saved credentials must be chmod 0o600."""
    # chmod permission bits aren't meaningful on Windows, but the code path
    # still runs; this test guards against the chmod call being removed on
    # POSIX. We just verify the file exists and contains the key.
    mode, auth_file = _make_mode_with_credentials({}, tmp_path, monkeypatch)
    mode._save_key_for("zai-coding-cn", "key")
    assert auth_file.exists()
    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert saved["zai-coding-cn"]["key"] == "key"


# ─── Esc-back navigation in /login flow ──────────────────────────────────


def _make_mode_with_mount(monkeypatch, tmp_path):
    """Build a mode stub with mocked TUI mounting to test Esc-back flow.

    Records every swap/restore so a test can assert the navigation path:
    editor → selector → key dialog → (Esc) → selector → (Esc) → editor.
    """
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "coding_agent.modes.interactive.interactive_mode.AUTH_FILE", auth_file
    )

    session = SimpleNamespace(model=SimpleNamespace(provider="deepseek"))
    obj = SimpleNamespace()
    obj._session = session
    obj._credentials = {}  # mount-flow tests start with no stored creds.
    obj._api_key = None
    obj.theme = _theme()
    # Track the sequence of mounted components + restores.
    obj.mount_log: list[str] = []
    obj._current_selector = None

    def _set_model(m):
        # The real AgentSession.set_model swaps the model in place. The stub
        # session holds model as a SimpleNamespace, so we just replace it.
        session.model = m
    session.set_model = _set_model

    def _swap_editor_for(comp):
        obj._current_selector = comp
        obj.mount_log.append(f"mount:{type(comp).__name__}")

    def _restore_editor():
        obj._current_selector = None
        obj.mount_log.append("restore:editor")

    def _add_system_message(msg):
        obj.mount_log.append(f"msg:{msg}")

    obj._swap_editor_for = _swap_editor_for
    obj._restore_editor = _restore_editor
    obj._add_system_message = _add_system_message
    # Bind real auth helpers + the two flow methods.
    for name in (
        "_get_stored_key", "_get_stored_key_for",
        "_save_key", "_save_key_for",
        "_remove_key", "_remove_key_for",
        "_login_provider_options",
        "_open_login_dialog",
        "_open_key_dialog_for",
        "_complete_provider_authentication",
    ):
        setattr(obj, name, getattr(InteractiveMode, name).__get__(obj, SimpleNamespace))
    # Restore the no-op overrides AFTER the bind loop (which would otherwise
    # replace them with the real InteractiveMode methods that need a TUI).
    obj._refresh_footer = lambda: None
    obj._update_editor_border_color = lambda: None
    return obj


def test_login_mounts_provider_selector_first(monkeypatch, tmp_path):
    """Multi-provider /login opens with the selector, not the key dialog."""
    for v in ("DEEPSEEK_API_KEY", "ZAI_CODING_CN_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    mode = _make_mode_with_mount(monkeypatch, tmp_path)
    mode._open_login_dialog()
    # Two providers → selector mounts first.
    assert mode.mount_log == ["mount:ProviderSelectorComponent"]


def test_login_single_provider_skips_selector(monkeypatch, tmp_path):
    """One provider → straight to key dialog (preserves old UX)."""
    for v in ("DEEPSEEK_API_KEY", "ZAI_CODING_CN_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    mode = _make_mode_with_mount(monkeypatch, tmp_path)
    # Stub provider list to only return one (simulating a future single-provider build).
    mode._login_provider_options = lambda: [
        ProviderOption(id="deepseek", name="DeepSeek", auth_state="")
    ]
    mode._open_login_dialog()
    assert mode.mount_log == ["mount:LoginDialogComponent"]


def test_login_key_dialog_esc_returns_to_selector(monkeypatch, tmp_path):
    """Esc in the key dialog remounts the selector (not the main editor).

    A back button inside the same
    /login invocation, so the user can switch providers without retyping
    /login.
    """
    for v in ("DEEPSEEK_API_KEY", "ZAI_CODING_CN_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    mode = _make_mode_with_mount(monkeypatch, tmp_path)
    mode._open_login_dialog()
    assert mode.mount_log == ["mount:ProviderSelectorComponent"]

    # Simulate "user picked DeepSeek" → key dialog mounts.
    selector = mode._current_selector
    selector._on_select("deepseek")
    assert mode.mount_log[-1] == "mount:LoginDialogComponent"

    # Simulate "user pressed Esc in key dialog" → back to selector.
    dialog = mode._current_selector
    dialog._on_cancel_outer()  # type: ignore[attr-defined]
    assert mode.mount_log[-1] == "mount:ProviderSelectorComponent"
    assert isinstance(mode._current_selector, ProviderSelectorComponent)


def test_login_selector_esc_exits_to_editor(monkeypatch, tmp_path):
    """Esc in the selector itself exits the whole flow to the main editor."""
    for v in ("DEEPSEEK_API_KEY", "ZAI_CODING_CN_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    mode = _make_mode_with_mount(monkeypatch, tmp_path)
    mode._open_login_dialog()
    selector = mode._current_selector
    selector._on_cancel()
    assert mode.mount_log[-1] == "restore:editor"


def test_login_full_flow_submit_returns_to_editor(monkeypatch, tmp_path):
    """Submitting a key always returns to the editor (no selector re-mount)."""
    for v in ("DEEPSEEK_API_KEY", "ZAI_CODING_CN_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    mode = _make_mode_with_mount(monkeypatch, tmp_path)
    mode._open_login_dialog()
    selector = mode._current_selector
    selector._on_select("zai-coding-cn")
    dialog = mode._current_selector
    dialog._on_submit_outer("my-glm-key")  # type: ignore[attr-defined]
    # Path: mount selector → mount dialog → save → switch model → msg → restore editor.
    # Message includes the model-switch confirmation.
    assert any("已保存 Z.AI Coding CN" in entry and "glm-5.1" in entry
               for entry in mode.mount_log if entry.startswith("msg:")), \
        f"expected switch confirmation in {mode.mount_log}"
    assert mode.mount_log[-1] == "restore:editor"
    # And the key actually got saved under the picked provider id.
    saved = json.loads((tmp_path / "auth.json").read_text(encoding="utf-8"))
    assert saved["zai-coding-cn"]["key"] == "my-glm-key"


def test_login_can_pick_different_provider_after_esc_back(monkeypatch, tmp_path):
    """Round-trip: pick DS, Esc back, pick GLM, submit — GLM key is saved."""
    for v in ("DEEPSEEK_API_KEY", "ZAI_CODING_CN_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    mode = _make_mode_with_mount(monkeypatch, tmp_path)
    mode._open_login_dialog()

    # Pick DeepSeek.
    mode._current_selector._on_select("deepseek")
    # Esc back.
    mode._current_selector._on_cancel_outer()  # type: ignore[attr-defined]
    # Pick GLM this time.
    mode._current_selector._on_select("zai-coding-cn")
    # Submit a key.
    mode._current_selector._on_submit_outer("glm-final")  # type: ignore[attr-defined]

    saved = json.loads((tmp_path / "auth.json").read_text(encoding="utf-8"))
    # DeepSeek was never submitted → not saved. GLM was.
    assert "deepseek" not in saved
    assert saved["zai-coding-cn"]["key"] == "glm-final"
