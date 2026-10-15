"""Retired Flash ids migrate; explicit Pro selections keep their model identity."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_core import SessionManager
from agent_llm import Context, UserMessage, deepseek_provider, zhipu_provider
from agent_llm.api.deepseek_responses import build_params
from coding_agent.cli.args import Args
from coding_agent.cli.main import _normalize_model_options, _resolve_model
from coding_agent.core import config
from coding_agent.core.settings import Settings, SettingsManager
from coding_agent.desktop.runtime import DesktopRuntime


LEGACY_IDS = ["deepseek-v4-flash", "deepseek-v4-flash-vision-exp"]


@pytest.mark.parametrize("legacy_id", LEGACY_IDS)
@pytest.mark.parametrize("qualified", [False, True])
def test_legacy_cli_model_arguments_send_canonical_model(legacy_id: str, qualified: bool) -> None:
    value = f"deepseek/{legacy_id}:low" if qualified else legacy_id
    args = Args(model=value)
    _normalize_model_options(args)
    model = _resolve_model(args, deepseek_provider())
    params = build_params(model, Context(), {"reasoning": args.thinking})
    assert model.name == "DeepSeek V4.1 Flash"
    assert params["model"] == "deepseek-flash"
    assert params["reasoning"]["effort"] == ("low" if qualified else "none")


@pytest.mark.parametrize("legacy_id", LEGACY_IDS)
@pytest.mark.parametrize("provider", [None, "deepseek"])
def test_old_settings_migrate_and_preserve_preferences(
    tmp_path: Path, legacy_id: str, provider: str | None,
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "default_provider": provider, "default_model": legacy_id,
        "thinking_level": "low", "max_retries": 4,
    }), encoding="utf-8")
    manager = SettingsManager(path)
    loaded = manager.load()
    assert loaded.default_model == "deepseek-flash"
    assert loaded.thinking_level == "low"
    assert loaded.max_retries == 4
    manager.save()
    assert json.loads(path.read_text(encoding="utf-8"))["default_model"] == "deepseek-flash"


def test_aliases_do_not_change_other_providers_or_accept_unknown_models(tmp_path: Path) -> None:
    manager = SettingsManager(tmp_path / "settings.json")
    manager.save(Settings(default_provider="custom", default_model="deepseek-v4-pro"))
    assert manager.load().default_model == "deepseek-v4-pro"
    with pytest.raises(SystemExit):
        _resolve_model(Args(model="deepseek-v4-pro"), zhipu_provider())
    with pytest.raises(SystemExit):
        _resolve_model(Args(model="deepseek-v4.1-pro"), deepseek_provider())


@pytest.mark.parametrize("legacy_id", [*LEGACY_IDS, "deepseek-v4-pro"])
def test_desktop_restores_legacy_deepseek_session_over_glm_default(
    tmp_path: Path, legacy_id: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    SettingsManager().save(Settings(
        default_provider="zai-coding-cn", default_model="glm-5.1",
        memory_enabled=False, web_search_enabled=False,
    ))
    saved = SessionManager.create(cwd=str(workspace), sessions_dir=config.get_sessions_dir())
    saved.append_model_change(provider="deepseek", model_id=legacy_id)
    saved.append_message(UserMessage(content="existing question"))
    saved.flush()

    async def exercise() -> None:
        runtime = DesktopRuntime(lambda _event: None)
        try:
            await runtime.dispatch("workspace.open", {
                "path": str(workspace), "sessionId": saved.header.id, "resume": True,
            })
            models = await runtime.dispatch("model.list", {})
            deepseek_models = [model for model in models if model["provider"] == "deepseek"]
            expected_id = "deepseek-v4-pro" if legacy_id == "deepseek-v4-pro" else "deepseek-flash"
            assert {model["id"] for model in deepseek_models} == {"deepseek-flash", "deepseek-v4-pro"}
            assert [model["id"] for model in deepseek_models if model["current"]] == [expected_id]
            assert runtime._session is not None
            assert runtime._session.state.messages[0].content == "existing question"
            selected = await runtime.dispatch("model.select", {
                "provider": "deepseek", "modelId": legacy_id,
            })
            assert selected["id"] == expected_id
            assert runtime._session.session_manager.build_session_context().model == {
                "provider": "deepseek", "model_id": expected_id,
            }
        finally:
            await runtime.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("qualified", [False, True])
def test_explicit_pro_cli_does_not_silently_select_flash(qualified):
    args = Args(model="deepseek/deepseek-v4-pro:low" if qualified else "deepseek-v4-pro")
    _normalize_model_options(args)
    model = _resolve_model(args, deepseek_provider())
    assert model.id == "deepseek-v4-pro"
    assert build_params(model, Context(), {"reasoning": args.thinking})["model"] == "deepseek-v4-pro"
    assert model.input == ["text"]


@pytest.mark.parametrize("provider", [None, "deepseek"])
def test_saved_pro_and_existing_flash_choices_remain_distinct(tmp_path, provider):
    for model_id in ("deepseek-v4-pro", "deepseek-flash"):
        path = tmp_path / f"{model_id}.json"
        path.write_text(json.dumps({"default_provider": provider, "default_model": model_id}), encoding="utf-8")
        manager = SettingsManager(path)
        assert manager.load().default_model == model_id
        manager.save()
        assert json.loads(path.read_text(encoding="utf-8"))["default_model"] == model_id
