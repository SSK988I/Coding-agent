"""Built-in slash commands.

Defines the catalog of built-in slash commands. The actual dispatch logic
lives in InteractiveMode; this module just provides the canonical list and
descriptions.

Inactive commands remain in the catalog for discoverability but are excluded
from completion and dispatch.

"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal

if TYPE_CHECKING:
    from agent_tui.autocomplete import AutocompleteItem

SlashCommandSource = Literal["builtin", "extension", "prompt", "skill"]

#: Argument completer signature: given the argument text typed so far,
#: return a list of completion suggestions or None (no completions).
ArgumentCompleter = Callable[[str], "list[AutocompleteItem] | None"]


@dataclass
class BuiltinSlashCommand:
    """A built-in slash command definition."""
    name: str
    description: str
    #: If True, this command is available for completion and dispatch.
    active: bool = True
    #: Optional argument completer. When set, typing ``/<name> <prefix>``
    #: pops a value list (e.g. ``/model`` lists models). Defaults to None
    #: (no argument completion — the command either takes free-form args
    #: or none). Wired at runtime in InteractiveMode for commands whose
    #: values depend on the live session/provider.
    get_argument_completions: "ArgumentCompleter | None" = None


# ─── The canonical list ───────────────────────────────────────────────────

BUILTIN_SLASH_COMMANDS: list[BuiltinSlashCommand] = [
    # ── Active commands ───────────────────────────────────────────────────
    BuiltinSlashCommand("help", "Show available commands and keybindings"),
    BuiltinSlashCommand("clear", "Clear the conversation and agent state"),
    BuiltinSlashCommand("model", "Select or switch model"),
    BuiltinSlashCommand(
        "thinking",
        "Set the reasoning/thinking level (now: Shift+Tab hotkey)",
        active=False,
    ),
    BuiltinSlashCommand("login", "Configure provider authentication (API key)"),
    BuiltinSlashCommand("logout", "Remove stored provider credentials"),
    BuiltinSlashCommand("compact", "Manually compact the session context"),
    BuiltinSlashCommand("quit", "Exit the application"),
    BuiltinSlashCommand("session", "Show session info and statistics"),
    BuiltinSlashCommand("name", "Set session display name"),
    BuiltinSlashCommand("new", "Start a new session"),

    # ── Export and utility commands ───────────────────────────────────────
    BuiltinSlashCommand("export", "Export session to HTML (default) or JSONL (.jsonl path)"),
    BuiltinSlashCommand("copy", "Copy last assistant message to clipboard"),
    BuiltinSlashCommand("hotkeys", "Show keyboard shortcuts"),

    # ── Reserved commands ─────────────────────────────────────────────────
    BuiltinSlashCommand("settings", "Show or update persistent settings"),
    BuiltinSlashCommand("import", "Import and resume a session from JSONL", active=False),
    BuiltinSlashCommand("share", "Share session as a secret GitHub gist", active=False),
    BuiltinSlashCommand("changelog", "Show changelog entries", active=False),
    BuiltinSlashCommand("fork", "Create a new fork from a previous message", active=False),
    BuiltinSlashCommand("clone", "Duplicate current session at current position", active=False),
    BuiltinSlashCommand("tree", "浏览会话树并切换分支"),
    BuiltinSlashCommand("trust", "Save project trust decision", active=False),
    BuiltinSlashCommand("resume", "Resume a different session", active=False),
    BuiltinSlashCommand("reload", "Reload extensions, skills, prompts, and themes", active=False),
    BuiltinSlashCommand("scoped-models", "Enable/disable models for Ctrl+P cycling", active=False),
]


def get_active_commands() -> list[BuiltinSlashCommand]:
    """Return commands that are currently available."""
    return [c for c in BUILTIN_SLASH_COMMANDS if c.active]


def get_command(name: str) -> BuiltinSlashCommand | None:
    """Look up a built-in command by name (without the leading /)."""
    for cmd in BUILTIN_SLASH_COMMANDS:
        if cmd.name == name:
            return cmd
    return None


def is_builtin_command(text: str) -> bool:
    """Check if text looks like a built-in slash command."""
    if not text.startswith("/"):
        return False
    name = text[1:].split(maxsplit=1)[0].strip().lower()
    return get_command(name) is not None
