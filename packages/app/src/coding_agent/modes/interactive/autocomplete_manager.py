"""Autocomplete popup manager for the interactive mode editor.

Wires an :class:`~agent_tui.autocomplete.CombinedAutocompleteProvider` to the
editor: on every text change it asks the provider for suggestions and
shows/hides a :class:`~agent_tui.components.select_list.SelectList` popup
mounted directly below the editor in the component tree.

Keyboard handling follows the ``tui.select.*`` conventions and is intercepted
via a pre-focus :class:`~agent_tui.tui.TUI` input
listener so the keys only get swallowed while the popup is open:

  - Up/Down   move the popup selection (wrap)
  - Tab       accept the selection but DO NOT submit (keep editing)
  - Enter     accept the selection; for ``/``-prefixed text, fall through
              to the editor's normal submit
  - Escape    dismiss the popup, leave the text unchanged

The manager runs entirely on the stdin reader thread (the same thread the
editor's ``handle_input`` runs on), so no loop-hopping is needed.
"""
from __future__ import annotations


from agent_tui import TUI
from agent_tui.autocomplete import (
    AutocompleteItem,
    CombinedAutocompleteProvider,
    Suggestions,
)
from agent_tui.components import Editor
from agent_tui.components.select_list import SelectItem, SelectList
from agent_tui.keys import matches_key


class AutocompleteManager:
    """Owns the editor's autocomplete popup lifecycle.

    Args:
        tui: the TUI (the popup is inserted into its component tree, right
            after the editor).
        editor: the focused text editor.
        provider: the suggestion source (usually a
            :class:`CombinedAutocompleteProvider`).
    """

    def __init__(self, tui: TUI, editor: Editor, provider: CombinedAutocompleteProvider) -> None:
        self._tui = tui
        self._editor = editor
        self._provider = provider

        self._popup = SelectList(max_visible=6)
        self._popup_mounted = False
        self._current: Suggestions | None = None

        # Wire the editor. The editor consults on_change after every
        # mutation (typing, deletion, cursor move); that's our trigger.
        self._editor.on_change = self._on_editor_change
        self._editor.set_autocomplete_provider(provider)

        # Pre-focus key listener: swallows nav/accept/cancel keys while the
        # popup is open, lets everything else through.
        self._tui.add_input_listener(self._on_input)

    # ── popup lifecycle ───────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        return self._popup_mounted and not self._popup.is_empty()

    def _on_editor_change(self, _text: str) -> None:
        """Recompute suggestions and show/hide the popup."""
        lines = self._editor.get_lines()
        line_idx, col = self._editor.get_cursor()
        suggestions = self._provider.get_suggestions(lines, line_idx, col)
        if suggestions is None or not suggestions.items:
            self._close_popup()
            return
        # If the single suggestion equals exactly what's typed (e.g. the
        # user already completed "/model"), there's nothing to pick — close.
        if len(suggestions.items) == 1 and suggestions.prefix == suggestions.items[0].value:
            # For command names prefix is "/name"; for args it's the bare value.
            self._close_popup()
            return
        self._current = suggestions
        items = [
            SelectItem(value=it.value, label=it.label, description=it.description)
            for it in suggestions.items
        ]
        self._popup.set_items(items)
        self._mount_popup()
        self._tui.request_render()

    def _mount_popup(self) -> None:
        if not self._popup_mounted:
            # Insert directly below the editor (after it in the tree).
            self._tui.insert_after(self._editor, self._popup)
            self._popup_mounted = True

    def _close_popup(self) -> None:
        if self._popup_mounted:
            self._tui.remove_child(self._popup)
            self._popup_mounted = False
            self._tui.request_render()
        self._current = None

    # ── key handling (stdin thread) ───────────────────────────────────

    def _on_input(self, data: str) -> "dict | None":
        if not self.is_open:
            return None
        # Cancel.
        if matches_key(data, "escape") or matches_key(data, "ctrl+c"):
            self._close_popup()
            return {"consume": True}
        # Navigation.
        if matches_key(data, "up"):
            self._popup.move_up()
            self._tui.request_render()
            return {"consume": True}
        if matches_key(data, "down"):
            self._popup.move_down()
            self._tui.request_render()
            return {"consume": True}
        # Accept without submitting.
        if matches_key(data, "tab"):
            self._accept_selection(submit=False)
            return {"consume": True}
        # Accept and fall through to submit (editor.handle_input sees Enter
        # next and fires on_submit, since we return None here for Enter).
        if matches_key(data, "enter"):
            self._accept_selection(submit=True)
            # Return None so the editor also receives Enter and submits.
            # The completion already updated the editor text, so when the
            # editor re-reads its text it'll submit the completed string.
            return None
        return None

    # ── accept ────────────────────────────────────────────────────────

    def _accept_selection(self, *, submit: bool) -> None:
        item = self._popup.get_selected()
        if item is None:
            self._close_popup()
            return
        # Convert the SelectItem back to an AutocompleteItem for the provider.
        ac_item = AutocompleteItem(value=item.value, label=item.label, description=item.description)
        lines = self._editor.get_lines()
        line_idx, col = self._editor.get_cursor()
        new_lines, new_line, new_col = self._provider.apply_completion(lines, line_idx, col, ac_item)
        new_text = "\n".join(new_lines)
        # set_text_and_cursor fires on_change again; that recomputes
        # suggestions. For a command-name accept we now have "/name " —
        # which (for model/thinking) reopens the argument popup; for
        # argument accept the prefix matches the value and the popup closes.
        self._editor.set_text_and_cursor(new_text, new_line, new_col)
        # The on_change callback may have reopened or closed the popup.
        # If this is a submit accept and the prefix was a slash command,
        # force-close so the pending Enter doesn't re-trigger.
        if submit:
            self._close_popup()
        self._tui.request_render()
