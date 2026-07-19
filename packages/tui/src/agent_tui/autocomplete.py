"""Autocomplete provider.

Two-layer completion over the editor's text-before-cursor:
  1. slash-command-name completion: typing ``/th`` filters the active
     command catalog by prefix (``th`` → ``thinking``, etc.).
  2. argument completion: once a command has a space after it
     (``/model ``), the command's ``get_argument_completions`` getter is
     called to produce a list of values (e.g. the available models).

Both reuse the same ``Suggestions`` shape and feed the same ``SelectList``
popup in the editor. Filtering is strict-prefix (the fuzzy matcher is an
enhancement that can be layered on later).

"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Protocol, runtime_checkable


@dataclass
class AutocompleteItem:
    """One completion suggestion."""
    value: str               # text inserted into the editor on accept
    label: str               # primary display text in the popup
    description: "str | None" = None


@dataclass
class Suggestions:
    """A batch of suggestions plus the text range they replace.

    ``prefix`` is the substring of the current line (before the cursor)
    that an accepted suggestion replaces — e.g. ``"/th"`` for a command
    name, or ``"claud"`` for a model argument. The caller uses it to
    compute the replacement range.
    """
    items: List[AutocompleteItem]
    prefix: str            # the raw text-before-cursor that was matched


@runtime_checkable
class AutocompleteProvider(Protocol):
    """Contract the editor talks to."""

    def get_suggestions(
        self,
        lines: List[str],
        line_idx: int,
        col: int,
        *,
        force: bool = False,
    ) -> "Suggestions | None":
        """Return suggestions for the cursor position, or None."""
        ...

    def apply_completion(
        self,
        lines: List[str],
        line_idx: int,
        col: int,
        item: AutocompleteItem,
    ) -> "tuple[List[str], int, int]":
        """Apply ``item`` to ``lines``; return (new_lines, new_line_idx, new_col)."""
        ...


@dataclass
class _CommandLike:
    """Minimal shape CombinedAutocompleteProvider reads off a command.

    Accepts ``BuiltinSlashCommand`` (which gained an optional
    ``get_argument_completions`` field) or any duck-typed object with
    ``name``/``description``/optional getter.
    """
    name: str
    description: "str | None" = None
    get_argument_completions: "Any" = None  # Callable[[str], list[AutocompleteItem] | None] | None


class CombinedAutocompleteProvider:
    """Slash-command + argument completion.

    The provider is given the active command catalog at construction.
    Command-name suggestions are produced by prefix-filtering the catalog;
    argument suggestions delegate to the matched command's
    ``get_argument_completions`` getter; commands without one return no popup.
    """

    def __init__(self, commands: "list[Any]") -> None:
        # Normalize to _CommandLike so we can read attributes uniformly and
        # tolerate commands missing the optional getter.
        self._commands: list[_CommandLike] = [
            _CommandLike(
                name=c.name,
                description=getattr(c, "description", None),
                get_argument_completions=getattr(c, "get_argument_completions", None),
            )
            for c in commands
        ]

    # ── AutocompleteProvider ──────────────────────────────────────────

    def get_suggestions(
        self,
        lines: List[str],
        line_idx: int,
        col: int,
        *,
        force: bool = False,
    ) -> "Suggestions | None":
        if line_idx < 0 or line_idx >= len(lines):
            return None
        # Slash-command completion is available only on the first line.
        if line_idx != 0:
            return None

        text_before = lines[line_idx][:col]
        stripped = text_before.lstrip()
        if not stripped.startswith("/"):
            return None

        space_idx = stripped.find(" ")
        if space_idx == -1:
            return self._command_name_suggestions(stripped)
        return self._argument_suggestions(stripped, space_idx)

    def apply_completion(
        self,
        lines: List[str],
        line_idx: int,
        col: int,
        item: AutocompleteItem,
    ) -> "tuple[List[str], int, int]":
        """Replace the matched prefix range with ``item.value``.

        For command-name completions the value already starts with ``/``;
        for argument completions the value is the bare argument. A trailing
        space is appended to command-name accepts so the user can continue
        typing the argument.
        """
        if line_idx < 0 or line_idx >= len(lines):
            return lines, line_idx, col

        line = lines[line_idx]
        before_cursor = line[:col]

        stripped = before_cursor.lstrip()
        leading_ws_len = len(before_cursor) - len(stripped)

        space_idx = stripped.find(" ")
        is_command_name = space_idx == -1

        if is_command_name:
            # Replace from the '/' through the cursor with "/name ".
            # The prefix range is [leading_ws_len, col).
            value = item.value
            if not value.endswith(" "):
                value = value + " "
            new_line = line[:leading_ws_len] + value + line[col:]
            new_col = leading_ws_len + len(value)
            new_lines = list(lines)
            new_lines[line_idx] = new_line
            return new_lines, line_idx, new_col

        # Argument completion: replace the argument prefix (after the space)
        # with the chosen value.
        # Locate where the argument prefix begins in the raw line.
        arg_start_in_line = leading_ws_len + space_idx + 1
        # The prefix may contain leading spaces already consumed by stripped;
        # recompute the actual replace range against the raw line.
        # Range to replace: [arg_start_in_line, col).
        new_line = line[:arg_start_in_line] + item.value + line[col:]
        new_col = arg_start_in_line + len(item.value)
        new_lines = list(lines)
        new_lines[line_idx] = new_line
        return new_lines, line_idx, new_col

    # ── internals ─────────────────────────────────────────────────────

    def _command_name_suggestions(self, stripped: str) -> "Suggestions | None":
        # stripped starts with '/'; the command-name prefix is stripped[1:].
        prefix = stripped[1:]
        # Case-insensitive prefix match (command names are lowercase anyway,
        # but be forgiving of accidental capitalization).
        lower = prefix.lower()
        matched = [c for c in self._commands if c.name.lower().startswith(lower)]
        if not matched:
            return None
        # If the user typed exactly the full name (no trailing chars), don't
        # Do not collapse a single-item list; the editor still needs to
        # re-show it; for cleanliness we still return it so Tab can append
        # the trailing space.
        items = [
            AutocompleteItem(
                value=f"/{c.name}",
                label=c.name,
                description=c.description,
            )
            for c in matched
        ]
        return Suggestions(items=items, prefix=stripped)

    def _argument_suggestions(self, stripped: str, space_idx: int) -> "Suggestions | None":
        cmd_name = stripped[1:space_idx]
        arg_prefix = stripped[space_idx + 1:]
        cmd = next((c for c in self._commands if c.name == cmd_name), None)
        if cmd is None or cmd.get_argument_completions is None:
            return None
        result = cmd.get_argument_completions(arg_prefix)
        if not result:
            return None
        return Suggestions(items=list(result), prefix=arg_prefix)
