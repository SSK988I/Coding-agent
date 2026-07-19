"""Interactive coding agent application.

Application layer that ties together agent_llm, agent_core, and agent_tui into a
complete CLI tool. Provides CLI argument parsing, AgentSession lifecycle
management, interactive TUI mode, and session persistence.

The public API exposes the application session and configuration helpers.
"""
from coding_agent.core.agent_session import AgentSession, AgentSessionConfig, AgentSessionEvent, SessionStats
from coding_agent.core.config import (
    APP_NAME,
    CONFIG_DIR_NAME,
    VERSION,
    get_agent_dir,
    get_auth_path,
    get_sessions_dir,
    get_settings_path,
)
from coding_agent.core.slash_commands import BUILTIN_SLASH_COMMANDS

__all__ = [
    # Config
    "APP_NAME",
    "CONFIG_DIR_NAME",
    "VERSION",
    "get_agent_dir",
    "get_auth_path",
    "get_sessions_dir",
    "get_settings_path",
    # Slash commands
    "BUILTIN_SLASH_COMMANDS",
    # Agent session
    "AgentSession",
    "AgentSessionConfig",
    "AgentSessionEvent",
    "SessionStats",
]

__version__ = "0.1.0"
