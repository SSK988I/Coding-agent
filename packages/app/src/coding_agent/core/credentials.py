"""Safe storage for provider API keys."""
from __future__ import annotations

import json
import os
import shutil
import warnings
from datetime import datetime, timezone
from pathlib import Path

from coding_agent.core.config import get_auth_path


class CredentialStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or get_auth_path()

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("credential root must be a JSON object")
            return data
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            backup = self._backup_corrupt_file()
            suffix = f"; backup: {backup}" if backup else ""
            warnings.warn(f"Ignoring invalid credential file {self.path}: {exc}{suffix}", stacklevel=2)
            return {}

    def save(self, credentials: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(credentials, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get_api_key(self, provider_id: str) -> str | None:
        credential = self.load().get(provider_id)
        if isinstance(credential, dict) and credential.get("type") == "api_key":
            key = credential.get("key")
            if isinstance(key, str) and key:
                return key
        return None

    def _backup_corrupt_file(self) -> Path | None:
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = self.path.with_name(f"{self.path.stem}.corrupt-{stamp}{self.path.suffix}")
            shutil.copy2(self.path, backup)
            return backup
        except OSError:
            return None


__all__ = ["CredentialStore"]
