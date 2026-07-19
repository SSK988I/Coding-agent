"""Theme system.

Provides the ``Theme`` class with semantic color tokens (``accent``, ``error``,
``userMessageBg``, ``mdHeading``, etc.) that resolve to ANSI truecolor escape
sequences. Loaded from ``data/themes/dark.json`` (a copy of the dark theme).

``fg(color, text)`` wraps text in the color and appends ``\\x1b[39m`` (reset
only foreground, preserving other styles like bold). Similarly ``bg`` uses
``\\x1b[49m``. ``bold/italic/underline/strikethrough`` use SGR codes with
``\\x1b[0m`` reset (these are typically terminal-safe to fully reset).
"""
from __future__ import annotations

import json
from importlib import resources

from agent_tui.components.markdown import MarkdownTheme

# ─── color value resolution ─────────────────────────

def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """Parse '#RRGGBB' → (r, g, b)."""
    cleaned = hex_str.lstrip("#")
    if len(cleaned) != 6:
        raise ValueError(f"Invalid hex color: {hex_str}")
    r = int(cleaned[0:2], 16)
    g = int(cleaned[2:4], 16)
    b = int(cleaned[4:6], 16)
    return r, g, b


def _fg_ansi(color: str) -> str:
    """Build a truecolor foreground ANSI sequence for a hex color."""
    r, g, b = _hex_to_rgb(color)
    return f"\x1b[38;2;{r};{g};{b}m"


def _bg_ansi(color: str) -> str:
    """Build a truecolor background ANSI sequence for a hex color."""
    r, g, b = _hex_to_rgb(color)
    return f"\x1b[48;2;{r};{g};{b}m"


def _resolve_var(value: str, vars_map: dict[str, str], visited: set[str] | None = None) -> str:
    """Resolve a variable reference (e.g. 'accent' → '#8abeb7') recursively.

    Hex values pass through; variable names resolve through ``vars_map``.
    Detects circular references.
    """
    if value.startswith("#"):
        return value
    visited = visited or set()
    if value in visited:
        raise ValueError(f"Circular variable reference: {value}")
    if value not in vars_map:
        raise ValueError(f"Variable reference not found: {value}")
    visited.add(value)
    return _resolve_var(vars_map[value], vars_map, visited)


#: Foreground color token names.
FG_COLOR_NAMES = frozenset({
    "accent", "border", "borderAccent", "borderMuted", "success", "error",
    "warning", "muted", "dim", "text", "thinkingText", "userMessageText",
    "customMessageText", "customMessageLabel", "toolTitle", "toolOutput",
    "mdHeading", "mdLink", "mdLinkUrl", "mdCode", "mdCodeBlock",
    "mdCodeBlockBorder", "mdQuote", "mdQuoteBorder", "mdHr", "mdListBullet",
    "toolDiffAdded", "toolDiffRemoved", "toolDiffContext",
    "syntaxComment", "syntaxKeyword", "syntaxFunction", "syntaxVariable",
    "syntaxString", "syntaxNumber", "syntaxType", "syntaxOperator",
    "syntaxPunctuation",
    "thinkingOff", "thinkingMinimal", "thinkingLow", "thinkingMedium",
    "thinkingHigh", "thinkingXhigh", "bashMode",
})

#: Background color token names.
BG_COLOR_NAMES = frozenset({
    "selectedBg", "userMessageBg", "customMessageBg",
    "toolPendingBg", "toolSuccessBg", "toolErrorBg",
})


class Theme:
    """Semantic color theme.

    ``fg(color_name, text)`` wraps text in a foreground color; ``bg`` in a
    background color. ``bold/italic/underline/strikethrough`` apply text
    decorations. Colors are resolved from a theme JSON's ``vars`` + ``colors``.
    """

    def __init__(
        self,
        fg_colors: dict[str, str],
        bg_colors: dict[str, str],
        *,
        name: str | None = None,
    ) -> None:
        self.name = name
        # Precompute ANSI sequences.
        self._fg: dict[str, str] = {}
        for key, hex_val in fg_colors.items():
            self._fg[key] = _fg_ansi(hex_val)
        self._bg: dict[str, str] = {}
        for key, hex_val in bg_colors.items():
            self._bg[key] = _bg_ansi(hex_val)

    def fg(self, color: str, text: str) -> str:
        """Wrap ``text`` in a foreground color.

        Appends ``\\x1b[39m`` (reset foreground only, preserving bold/etc.).
        """
        ansi = self._fg.get(color)
        if ansi is None:
            raise KeyError(f"Unknown theme fg color: {color}")
        return f"{ansi}{text}\x1b[39m"

    def bg(self, color: str, text: str) -> str:
        """Wrap ``text`` in a background color.

        Appends ``\\x1b[49m`` (reset background only).
        """
        ansi = self._bg.get(color)
        if ansi is None:
            raise KeyError(f"Unknown theme bg color: {color}")
        return f"{ansi}{text}\x1b[49m"

    def bold(self, text: str) -> str:
        return f"\x1b[1m{text}\x1b[22m"

    def italic(self, text: str) -> str:
        return f"\x1b[3m{text}\x1b[23m"

    def underline(self, text: str) -> str:
        return f"\x1b[4m{text}\x1b[24m"

    def strikethrough(self, text: str) -> str:
        return f"\x1b[9m{text}\x1b[29m"

    def get_fg_ansi(self, color: str) -> str:
        """Raw ANSI sequence for a fg color (for manual composition)."""
        ansi = self._fg.get(color)
        if ansi is None:
            raise KeyError(f"Unknown theme fg color: {color}")
        return ansi

    def get_bg_ansi(self, color: str) -> str:
        """Raw ANSI sequence for a bg color."""
        ansi = self._bg.get(color)
        if ansi is None:
            raise KeyError(f"Unknown theme bg color: {color}")
        return ansi


# ─── theme loading ──────────────────────────────────

def _load_theme_data(name: str) -> dict:
    """Load a theme JSON from the packaged data/themes/ directory."""
    pkg = resources.files("agent_tui").joinpath("data", "themes", f"{name}.json")
    return json.loads(pkg.read_text(encoding="utf-8"))


def _create_theme(theme_json: dict) -> Theme:
    """Build a Theme from parsed JSON.

    Resolves var references, splits colors into fg/bg by name.
    """
    vars_map: dict[str, str] = {
        k: v for k, v in (theme_json.get("vars") or {}).items()
    }
    colors_raw: dict[str, str] = theme_json.get("colors") or {}

    fg_colors: dict[str, str] = {}
    bg_colors: dict[str, str] = {}
    for key, value in colors_raw.items():
        resolved = _resolve_var(value, vars_map)
        if key in BG_COLOR_NAMES:
            bg_colors[key] = resolved
        else:
            fg_colors[key] = resolved

    return Theme(fg_colors, bg_colors, name=theme_json.get("name"))


def load_theme(name: str = "dark") -> Theme:
    """Load a named theme. Falls back to 'dark' on error."""
    try:
        return _create_theme(_load_theme_data(name))
    except Exception:
        return _create_theme(_load_theme_data("dark"))


# ─── default markdown theme factory ───────────────

def get_markdown_theme(theme: Theme) -> MarkdownTheme:
    """Build a MarkdownTheme from a Theme.

    Returns a dataclass of style functions mapping markdown element types to
    ANSI-styled strings, using the theme's semantic color tokens.
    """
    return MarkdownTheme(
        heading=lambda text: theme.fg("mdHeading", text),
        link=lambda text: theme.fg("mdLink", text),
        link_url=lambda text: theme.fg("mdLinkUrl", text),
        code=lambda text: theme.fg("mdCode", text),
        code_block=lambda text: theme.fg("mdCodeBlock", text),
        code_block_border=lambda text: theme.fg("mdCodeBlockBorder", text),
        quote=lambda text: theme.fg("mdQuote", text),
        quote_border=lambda text: theme.fg("mdQuoteBorder", text),
        hr=lambda text: theme.fg("mdHr", text),
        list_bullet=lambda text: theme.fg("mdListBullet", text),
        bold=theme.bold,
        italic=theme.italic,
        underline=theme.underline,
        strikethrough=theme.strikethrough,
    )
