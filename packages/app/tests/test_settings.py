from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.core.settings import Settings, SettingsManager


def test_web_search_is_enabled_by_default() -> None:
    assert Settings().web_search_enabled is True


def test_settings_round_trip_and_atomic_temp_cleanup(tmp_path: Path):
    path = tmp_path / "settings.json"
    manager = SettingsManager(path)
    manager.save(Settings(default_provider="zhipu", max_retries=4))

    loaded = SettingsManager(path).load()
    assert loaded.default_provider == "zhipu"
    assert loaded.max_retries == 4
    assert list(tmp_path.glob("*.tmp")) == []


def test_invalid_settings_are_backed_up_and_reset(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.warns(UserWarning, match="Ignoring invalid settings"):
        loaded = SettingsManager(path).load()

    assert loaded == Settings()
    assert len(list(tmp_path.glob("settings.corrupt-*.json"))) == 1


def test_set_value_validates_and_persists(tmp_path: Path):
    manager = SettingsManager(tmp_path / "settings.json")
    manager.load()
    manager.set_value("auto_retry", "false")
    manager.set_value("max_retries", "3")
    manager.set_value("memory_enabled", "off")
    manager.set_value("memory_auto_extract", "false")
    manager.set_value("memory_max_records", "12")
    manager.set_value("memory_token_budget", "1200")
    manager.set_value("web_search_enabled", "true")
    manager.set_value("web_search_max_results", "7")
    manager.set_value("web_search_timeout_seconds", "45")

    loaded = SettingsManager(manager.path).load()
    assert loaded.auto_retry is False
    assert loaded.max_retries == 3
    assert loaded.memory_enabled is False
    assert loaded.memory_auto_extract is False
    assert loaded.memory_max_records == 12
    assert loaded.memory_token_budget == 1200
    assert loaded.web_search_enabled is True
    assert loaded.web_search_max_results == 7
    assert loaded.web_search_timeout_seconds == 45.0

    with pytest.raises(ValueError, match="Unknown setting"):
        manager.set_value("not_real", "x")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("web_search_backend", "unknown"),
        ("web_search_max_results", "0"),
        ("web_search_timeout_seconds", "121"),
    ],
)
def test_web_search_settings_reject_invalid_values(tmp_path: Path, field: str, value: str):
    manager = SettingsManager(tmp_path / "settings.json")
    manager.load()
    with pytest.raises(ValueError):
        manager.set_value(field, value)
