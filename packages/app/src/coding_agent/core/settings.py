"""Persistent user settings with validation and atomic writes."""
from __future__ import annotations

import json
import os
import shutil
import warnings
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coding_agent.core.config import get_settings_path


@dataclass
class Settings:
    """Small, stable settings surface required by the first public release."""

    version: int = 1
    default_provider: str | None = None
    default_model: str | None = None
    thinking_level: str | None = None
    theme: str = "dark"
    auto_retry: bool = True
    max_retries: int = 2
    retry_initial_delay: float = 1.0
    retry_max_delay: float = 8.0


class SettingsManager:
    """Load and save ``settings.json`` without risking partial files."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or get_settings_path()
        self.settings = Settings()

    def load(self) -> Settings:
        if not self.path.exists():
            self.settings = Settings()
            return self.settings
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("settings root must be a JSON object")
            allowed = {item.name for item in fields(Settings)}
            values = {key: value for key, value in raw.items() if key in allowed}
            self.settings = self._validate(Settings(**values))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            backup = self._backup_corrupt_file()
            suffix = f"; backup: {backup}" if backup else ""
            warnings.warn(f"Ignoring invalid settings file {self.path}: {exc}{suffix}", stacklevel=2)
            self.settings = Settings()
        return self.settings

    def save(self, settings: Settings | None = None) -> None:
        value = self._validate(settings or self.settings)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(asdict(value), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
        self.settings = value

    def set_value(self, key: str, raw_value: str) -> Settings:
        """Validate one user-facing value and persist it."""
        if key not in {item.name for item in fields(Settings)} - {"version"}:
            raise ValueError(f"Unknown setting: {key}")

        current = getattr(self.settings, key)
        value: Any = raw_value.strip()
        if isinstance(current, bool):
            normalized = value.lower()
            if normalized not in {"true", "false", "on", "off", "1", "0"}:
                raise ValueError(f"{key} must be true or false")
            value = normalized in {"true", "on", "1"}
        elif isinstance(current, int):
            value = int(value)
        elif isinstance(current, float):
            value = float(value)
        elif value.lower() in {"none", "null", ""}:
            value = None

        setattr(self.settings, key, value)
        self.save(self.settings)
        return self.settings

    @staticmethod
    def _validate(settings: Settings) -> Settings:
        valid_thinking = {None, "off", "minimal", "low", "medium", "high", "xhigh"}
        if settings.thinking_level not in valid_thinking:
            raise ValueError(f"invalid thinking_level: {settings.thinking_level}")
        if settings.default_provider is not None and not isinstance(settings.default_provider, str):
            raise ValueError("default_provider must be a string or null")
        if settings.default_model is not None and not isinstance(settings.default_model, str):
            raise ValueError("default_model must be a string or null")
        if settings.theme != "dark":
            raise ValueError("theme must currently be 'dark'")
        if not isinstance(settings.auto_retry, bool):
            raise ValueError("auto_retry must be a boolean")
        if isinstance(settings.max_retries, bool) or not 0 <= settings.max_retries <= 10:
            raise ValueError("max_retries must be between 0 and 10")
        if not 0 <= settings.retry_initial_delay <= 60:
            raise ValueError("retry_initial_delay must be between 0 and 60")
        if not 0 <= settings.retry_max_delay <= 300:
            raise ValueError("retry_max_delay must be between 0 and 300")
        if settings.retry_max_delay < settings.retry_initial_delay:
            raise ValueError("retry_max_delay must be >= retry_initial_delay")
        return settings

    def _backup_corrupt_file(self) -> Path | None:
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = self.path.with_name(f"{self.path.stem}.corrupt-{stamp}{self.path.suffix}")
            shutil.copy2(self.path, backup)
            return backup
        except OSError:
            return None


__all__ = ["Settings", "SettingsManager"]
