"""Tests that editor swap navigation never stacks components in the TUI tree.

Regression guard for a bug where Esc-back navigation in /login left stale
selector/dialog components mounted in the tree. The root cause:
``_swap_editor_for`` did ``insert_after(self.editor, ...)`` followed by
``remove_child(self.editor)``, but on subsequent swaps the editor was
already absent from the tree, so ``insert_after`` fell through to its
``except ValueError: append`` branch — silently leaving the previous
component mounted and appending the new one at the end. Each Esc-back
then added another frame, producing the ugly "stack of bordered dialogs
piling up in the chat area" effect.

These tests build a real :class:`InteractiveMode` with a real TUI tree
and assert the ``tui.children`` invariant after each navigation step.
"""
from __future__ import annotations

from agent_llm import Model, ModelCost
from agent_core import SessionManager

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.modes.interactive.components.login_dialog import (
    LoginDialogComponent,
)
from coding_agent.modes.interactive.components.provider_selector import (
    ProviderSelectorComponent,
)


def _model(provider: str = "deepseek") -> Model:
    return Model(
        id="m",
        provider=provider,
        reasoning=True,
        context_window=1000,
        thinking_level_map={"minimal": None, "low": None, "medium": None,
                            "high": "high", "xhigh": "max"},
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )


def _make_mode(provider: str = "deepseek"):
    from coding_agent.modes.interactive.interactive_mode import InteractiveMode
    sm = SessionManager.create(cwd=".", in_memory=True)
    session = AgentSession(AgentSessionConfig(model=_model(provider), cwd=".", session_manager=sm))
    return InteractiveMode(session)


def _mounted_selectors(mode) -> list:
    """Return all selector/dialog components currently in the TUI tree.

    A healthy navigation flow should have at most ONE such component mounted
    at any time (the editor is swapped in-place). More than one means old
    frames are stacking up.
    """
    sel_types = (ProviderSelectorComponent, LoginDialogComponent)
    return [c for c in mode.tui.children if isinstance(c, sel_types)]


# ─── single swap (regression: original behavior must still work) ────────


def test_single_swap_mounts_exactly_one_selector():
    mode = _make_mode()
    # Sanity: tree starts with editor mounted, no selector.
    assert mode.editor in mode.tui.children
    assert _mounted_selectors(mode) == []

    mode._open_login_dialog()
    mounted = _mounted_selectors(mode)
    assert len(mounted) == 1
    assert isinstance(mounted[0], ProviderSelectorComponent)
    # Editor must be removed while a selector is up.
    assert mode.editor not in mode.tui.children


def test_single_swap_then_restore_returns_to_editor():
    mode = _make_mode()
    mode._open_login_dialog()
    mode._restore_editor()
    assert mode.editor in mode.tui.children
    assert _mounted_selectors(mode) == []


# ─── multi-step navigation (the bug scenario) ───────────────────────────


def test_selector_to_dialog_does_not_leave_selector_mounted():
    """Pick a provider → key dialog mounts, selector goes away.

    Before the fix, the selector stayed in the tree because insert_after
    couldn't find the editor and fell back to append.
    """
    mode = _make_mode()
    mode._open_login_dialog()
    selector = mode._current_selector
    assert isinstance(selector, ProviderSelectorComponent)

    # Simulate "user picked DeepSeek".
    selector._on_select("deepseek")

    # Now a key dialog should be mounted, and the selector must be gone.
    assert isinstance(mode._current_selector, LoginDialogComponent)
    mounted = _mounted_selectors(mode)
    assert len(mounted) == 1, f"expected 1 mounted, got {len(mounted)}: {mounted}"
    assert isinstance(mounted[0], LoginDialogComponent)


def test_esc_back_from_dialog_remounts_selector_without_stacking():
    """selector → dialog → Esc → selector must NOT stack the dialog.

    This is the exact scenario from the bug report: Esc-back should leave
    exactly one component (the new selector) in the tree, not three.
    """
    mode = _make_mode()
    mode._open_login_dialog()

    # Step 1: pick a provider → dialog mounts.
    mode._current_selector._on_select("deepseek")
    assert len(_mounted_selectors(mode)) == 1

    # Step 2: Esc in dialog → selector remounts.
    mode._current_selector._on_cancel_outer()  # type: ignore[attr-defined]
    mounted_after_esc = _mounted_selectors(mode)
    assert len(mounted_after_esc) == 1, (
        f"Esc-back stacked components: {len(mounted_after_esc)} mounted"
    )
    assert isinstance(mounted_after_esc[0], ProviderSelectorComponent)


def test_repeated_esc_back_never_stacks():
    """Stress test: many round-trips selector → dialog → Esc → selector.

    Each iteration must leave exactly ONE selector/dialog in the tree.
    A regression would show the count growing linearly with iterations.
    """
    mode = _make_mode()
    mode._open_login_dialog()
    for i in range(5):
        mode._current_selector._on_select("deepseek")
        assert len(_mounted_selectors(mode)) == 1, f"iter {i}: dialog mount"
        mode._current_selector._on_cancel_outer()  # type: ignore[attr-defined]
        mounted = _mounted_selectors(mode)
        assert len(mounted) == 1, f"iter {i} after Esc: {len(mounted)} stacked"
        assert isinstance(mounted[0], ProviderSelectorComponent)


def test_full_round_trip_selector_dialog_selector_dialog_submit():
    """selector → dialog (Esc) → selector → dialog (submit) → editor.

    End-to-end: confirms both Esc-back AND final submit leave a clean tree.
    """
    mode = _make_mode()
    mode._open_login_dialog()

    # Pick DeepSeek.
    mode._current_selector._on_select("deepseek")
    # Esc back.
    mode._current_selector._on_cancel_outer()  # type: ignore[attr-defined]
    # Pick again.
    mode._current_selector._on_select("deepseek")
    # Submit.
    mode._current_selector._on_submit_outer("the-key")  # type: ignore[attr-defined]

    # Editor must be back, no selectors left.
    assert mode.editor in mode.tui.children
    assert _mounted_selectors(mode) == []


def test_model_selector_also_does_not_stack():
    """The /model selector uses the same _swap_editor_for path — same invariant.

    Guards against the same bug resurfacing if a future change adds multi-step
    navigation to the model selector.
    """
    mode = _make_mode()
    mode._open_model_selector("")
    assert len(_mounted_selectors(mode)) == 0  # ModelSelector isn't in our type list
    # But the same tree invariant holds: editor out, exactly one non-editor
    # component (the ModelSelector) in its place.
    non_footer_non_chat = [
        c for c in mode.tui.children
        if c is not mode._footer
        and c is not mode.chat_container
        and c is not mode.status_container
    ]
    assert len(non_footer_non_chat) == 1
    assert mode.editor not in mode.tui.children

    # Restore must clean up.
    mode._restore_editor()
    assert mode.editor in mode.tui.children


# ─── TUI children count invariant ───────────────────────────────────────


def test_tui_children_count_constant_across_navigation():
    """Total children count must stay constant across any navigation step.

    The tree always has: chat_container + status_container + (editor OR
    selector/dialog) + footer = 4 children. A stacking bug would grow this.
    """
    mode = _make_mode()
    baseline = len(mode.tui.children)
    assert baseline == 4  # chat, status, editor, footer

    mode._open_login_dialog()
    assert len(mode.tui.children) == 4

    mode._current_selector._on_select("deepseek")
    assert len(mode.tui.children) == 4

    mode._current_selector._on_cancel_outer()  # type: ignore[attr-defined]
    assert len(mode.tui.children) == 4

    mode._current_selector._on_cancel()  # selector's own Esc → restore editor
    assert len(mode.tui.children) == 4
    assert mode.editor in mode.tui.children
