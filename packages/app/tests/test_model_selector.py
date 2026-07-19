"""Tests for ModelSelectorComponent."""
from __future__ import annotations

from dataclasses import dataclass

from coding_agent.modes.interactive.components.model_selector import (
    ModelSelectorComponent,
)


@dataclass
class _FakeModel:
    id: str
    name: str
    provider: str = "deepseek"
    reasoning: bool = True


def _theme():
    """Build a real dark theme (has 'border', 'dim', etc.)."""
    from agent_tui.theme import load_theme

    return load_theme("dark")


def _make(models, current=None, **kw):
    selected, cancelled = [], []

    def on_select(mid):
        selected.append(mid)

    def on_cancel():
        cancelled.append(True)

    sel = ModelSelectorComponent(
        _theme(), models, current, on_select, on_cancel, **kw
    )
    return sel, selected, cancelled


# ── initial state ──────────────────────────────────────────────────────


def test_initial_list_contains_all_models():
    models = [_FakeModel("a", "A"), _FakeModel("b", "B")]
    sel, _, _ = _make(models)
    assert [it.value for it in sel._list.items] == ["a", "b"]


def test_current_model_sorted_to_top():
    models = [_FakeModel("a", "A"), _FakeModel("b", "B"), _FakeModel("c", "C")]
    sel, _, _ = _make(models, current="c")
    # 'c' first, then a, b (alphabetical among the rest)
    assert [it.value for it in sel._list.items] == ["c", "a", "b"]
    # initial selection = top = current model
    assert sel._list.get_selected().value == "c"


def test_initial_selection_first_when_no_current():
    models = [_FakeModel("a", "A"), _FakeModel("b", "B")]
    sel, _, _ = _make(models, current=None)
    assert sel._list.get_selected().value == "a"


# ── filtering ──────────────────────────────────────────────────────────


def test_typing_narrows_list():
    models = [
        _FakeModel("deepseek-v4-flash", "Flash"),
        _FakeModel("deepseek-v4-pro", "Pro"),
    ]
    sel, _, _ = _make(models, current="deepseek-v4-flash")
    # type "pro"
    for ch in "pro":
        sel.handle_input(ch)
    assert [it.value for it in sel._list.items] == ["deepseek-v4-pro"]


def test_typing_matches_name_not_just_id():
    models = [_FakeModel("a", "Alpha Model"), _FakeModel("b", "Beta")]
    sel, _, _ = _make(models)
    for ch in "beta":
        sel.handle_input(ch)
    assert [it.value for it in sel._list.items] == ["b"]


def test_backspace_removes_last_filter_char():
    models = [_FakeModel("aa", "AA"), _FakeModel("bb", "BB")]
    sel, _, _ = _make(models)
    for ch in "a":
        sel.handle_input(ch)
    assert [it.value for it in sel._list.items] == ["aa"]
    sel.handle_input("\x7f")  # backspace
    assert [it.value for it in sel._list.items] == ["aa", "bb"]


def test_no_matches_keeps_list_empty():
    models = [_FakeModel("a", "A")]
    sel, _, _ = _make(models)
    for ch in "zzz":
        sel.handle_input(ch)
    assert sel._list.is_empty()


def test_initial_filter_pre_seeds():
    models = [_FakeModel("flash", "F"), _FakeModel("pro", "P")]
    sel, _, _ = _make(models, initial_filter="pro")
    assert [it.value for it in sel._list.items] == ["pro"]


# ── navigation ─────────────────────────────────────────────────────────


def test_down_wraps_around():
    models = [_FakeModel("a", "A"), _FakeModel("b", "B"), _FakeModel("c", "C")]
    sel, _, _ = _make(models)
    assert sel._list.selected_index == 0
    sel.handle_input("\x1b[B")  # down
    assert sel._list.selected_index == 1
    sel.handle_input("\x1b[B")  # down
    assert sel._list.selected_index == 2
    sel.handle_input("\x1b[B")  # down → wrap
    assert sel._list.selected_index == 0


def test_up_wraps_around():
    models = [_FakeModel("a", "A"), _FakeModel("b", "B")]
    sel, _, _ = _make(models)
    assert sel._list.selected_index == 0
    sel.handle_input("\x1b[A")  # up → wrap to bottom
    assert sel._list.selected_index == 1


# ── accept / cancel ────────────────────────────────────────────────────


def test_enter_fires_on_select_with_value():
    models = [_FakeModel("a", "A"), _FakeModel("b", "B")]
    sel, selected, cancelled = _make(models, current="b")
    # current is 'b' at top, already selected
    sel.handle_input("\r")  # enter
    assert selected == ["b"]
    assert not cancelled


def test_enter_after_navigation_selects_highlighted():
    models = [_FakeModel("a", "A"), _FakeModel("b", "B"), _FakeModel("c", "C")]
    sel, selected, _ = _make(models)
    sel.handle_input("\x1b[B")  # down → index 1
    sel.handle_input("\x1b[B")  # down → index 2 = 'c'
    sel.handle_input("\r")  # enter
    assert selected == ["c"]


def test_enter_on_empty_list_cancels():
    models = [_FakeModel("a", "A")]
    sel, selected, cancelled = _make(models)
    for ch in "zzz":
        sel.handle_input(ch)
    assert sel._list.is_empty()
    sel.handle_input("\r")  # enter
    assert selected == []
    assert cancelled == [True]


def test_escape_fires_on_cancel():
    models = [_FakeModel("a", "A")]
    sel, selected, cancelled = _make(models)
    sel.handle_input("\x1b")  # escape
    assert cancelled == [True]
    assert selected == []


def test_ctrl_c_fires_on_cancel():
    models = [_FakeModel("a", "A")]
    sel, selected, cancelled = _make(models)
    sel.handle_input("\x03")  # ctrl+c
    assert cancelled == [True]
    assert selected == []


# ── render ─────────────────────────────────────────────────────────────


def _strip_ansi(text):
    import re

    return re.compile(r"\x1b\[[0-9;]*m|\x1b_[a-z]:[a-z]\x07").sub("", text)


def test_render_has_top_and_bottom_borders():
    models = [_FakeModel("a", "A")]
    sel, _, _ = _make(models)
    lines = sel.render(60)
    # first and last lines are borders (strip ansi → all dashes)
    assert _strip_ansi(lines[0]) == "─" * 60
    assert _strip_ansi(lines[-1]) == "─" * 60


def test_render_includes_filter_line_and_models():
    models = [_FakeModel("deepseek-v4-flash", "Flash"), _FakeModel("deepseek-v4-pro", "Pro")]
    sel, _, _ = _make(models, current="deepseek-v4-flash")
    lines = sel.render(60)
    body = "\n".join(_strip_ansi(line) for line in lines)
    # filter prompt present
    assert "> " in body
    # both model ids shown (unfiltered)
    assert "deepseek-v4-flash" in body
    assert "deepseek-v4-pro" in body
    # at least 4 lines: border, hint, filter, 2 models, border
    assert len(lines) >= 6
