"""Path configuration for the coding agent.

Provides canonical paths for the agent config directory, session storage,
auth file, and settings file. All path resolution is centralized here so
the rest of the application does not hardcode paths.

"""
from __future__ import annotations

import os
from pathlib import Path

# ─── Application identity ──────────────────────────────────────────────────

APP_NAME: str = "coding-agent"
CONFIG_DIR_NAME: str = ".coding-agent"
VERSION: str = "0.1.0"

# ─── Agent config directory (~/.coding-agent/) ───────────────────────────────

_ENV_AGENT_DIR = "CODING_AGENT_HOME"


def get_agent_dir() -> Path:
    """Agent config directory: ~/.coding-agent/ (or env override).

    All sessions, settings, credentials, and user-installed resources live
    under this directory.
    """
    env_dir = os.environ.get(_ENV_AGENT_DIR)
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return Path.home() / CONFIG_DIR_NAME


def get_sessions_dir() -> Path:
    """Session storage directory: ~/.coding-agent/sessions/."""
    return get_agent_dir() / "sessions"


def get_settings_path() -> Path:
    """Global settings file: ~/.coding-agent/settings.json."""
    return get_agent_dir() / "settings.json"


def get_auth_path() -> Path:
    """Auth credentials file: ~/.coding-agent/auth.json."""
    return get_agent_dir() / "auth.json"


def get_models_path() -> Path:
    """Custom models file: ~/.coding-agent/models.json."""
    return get_agent_dir() / "models.json"


def get_custom_themes_dir() -> Path:
    """User custom themes: ~/.coding-agent/themes/."""
    return get_agent_dir() / "themes"


def get_skills_dir() -> Path:
    """User skills: ~/.coding-agent/skills/."""
    return get_agent_dir() / "skills"


def get_extensions_dir() -> Path:
    """User extensions: ~/.coding-agent/extensions/."""
    return get_agent_dir() / "extensions"


def get_prompts_dir() -> Path:
    """User prompt templates: ~/.coding-agent/prompts/."""
    return get_agent_dir() / "prompts"


def get_debug_log_path() -> Path:
    """Debug log: ~/.coding-agent/coding-agent-debug.log."""
    return get_agent_dir() / f"{APP_NAME}-debug.log"
