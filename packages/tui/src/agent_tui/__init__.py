"""Reusable terminal rendering, input, theme, and component primitives."""

# Core rendering and input primitives
from agent_tui.terminal import ProcessTerminal, Terminal
from agent_tui.tui import CURSOR_MARKER, Component, Container, Focusable, TUI, is_focusable
from agent_tui.keys import (
    is_key_release,
    is_key_repeat,
    is_kitty_protocol_active,
    matches_key,
    parse_key,
    parse_key_id,
    set_kitty_protocol_active,
)
from agent_tui.utils import (
    normalize_terminal_output,
    slice_by_column,
    truncate_to_width,
    visible_width,
    wrap_text_with_ansi,
)

# Components and themes
from agent_tui.theme import Theme, get_markdown_theme, load_theme
from agent_tui.components import (
    Box,
    DefaultTextStyle,
    Editor,
    Loader,
    LoaderIndicatorOptions,
    Markdown,
    MarkdownOptions,
    MarkdownTheme,
    Spacer,
    Text,
    TruncatedText,
)
# Editor support
from agent_tui.kill_ring import KillRing
from agent_tui.undo_stack import UndoStack

__all__ = [
    # Terminal
    "Terminal",
    "ProcessTerminal",
    # Component system
    "Component",
    "Container",
    "Focusable",
    "is_focusable",
    "CURSOR_MARKER",
    "TUI",
    # Keys
    "matches_key",
    "parse_key",
    "parse_key_id",
    "is_key_release",
    "is_key_repeat",
    "is_kitty_protocol_active",
    "set_kitty_protocol_active",
    # Utils
    "visible_width",
    "truncate_to_width",
    "wrap_text_with_ansi",
    "slice_by_column",
    "normalize_terminal_output",
    # Theme
    "Theme",
    "load_theme",
    "get_markdown_theme",
    # Components
    "Box",
    "Text",
    "Spacer",
    "TruncatedText",
    "Markdown",
    "MarkdownTheme",
    "MarkdownOptions",
    "DefaultTextStyle",
    "Loader",
    "LoaderIndicatorOptions",
    "Editor",
    # Editor support
    "KillRing",
    "UndoStack",
]

__version__ = "0.3.0"
