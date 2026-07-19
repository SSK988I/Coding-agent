"""Tests for SelectList."""
from __future__ import annotations

import re

from agent_tui.components.select_list import SelectItem, SelectList
from agent_tui.utils import visible_width

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b_[a-z]:[a-z]\x07")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _items(n: int) -> list[SelectItem]:
    return [SelectItem(value=f"v{i}", label=f"cmd-{i}", description=f"desc-{i}") for i in range(n)]


# ── navigation ─────────────────────────────────────────────────────────


def test_empty_list_get_selected_none():
    sl = SelectList()
    assert sl.get_selected() is None
    assert sl.is_empty()
    # navigation is a no-op on empty
    sl.move_up()
    sl.move_down()
    assert sl.get_selected() is None


def test_set_items_resets_selection_to_top():
    sl = SelectList(_items(5))
    sl.set_selected_index(3)
    assert sl.selected_index == 3
    sl.set_items(_items(4))
    assert sl.selected_index == 0


def test_move_down_wraps_around():
    sl = SelectList(_items(3))
    assert sl.selected_index == 0
    sl.move_down()
    assert sl.selected_index == 1
    sl.move_down()
    assert sl.selected_index == 2
    sl.move_down()  # wrap to top
    assert sl.selected_index == 0


def test_move_up_wraps_around():
    sl = SelectList(_items(3))
    assert sl.selected_index == 0
    sl.move_up()  # wrap to bottom
    assert sl.selected_index == 2
    sl.move_up()
    assert sl.selected_index == 1


def test_set_selected_index_clamps():
    sl = SelectList(_items(3))
    sl.set_selected_index(-5)
    assert sl.selected_index == 0
    sl.set_selected_index(99)
    assert sl.selected_index == 2


# ── render ─────────────────────────────────────────────────────────────


def test_render_empty_returns_no_lines():
    assert SelectList().render(40) == []


def test_render_one_line_per_visible_item():
    sl = SelectList(_items(3), max_visible=5)
    lines = sl.render(60)
    assert len(lines) == 3
    # each line fits within width
    for line in lines:
        assert visible_width(strip_ansi(line)) <= 60


def test_render_selected_item_is_reversed():
    sl = SelectList(_items(3), max_visible=5)
    sl.set_selected_index(1)
    lines = sl.render(60)
    # exactly one line carries the reverse-video marker
    assert sum(1 for ln in lines if "\x1b[7m" in ln) == 1
    # the selected line contains the second item's label
    assert any("cmd-1" in ln for ln in lines)


def test_render_scroll_window_with_indicator():
    sl = SelectList(_items(8), max_visible=3)
    sl.set_selected_index(5)  # 1-based 6/8
    lines = sl.render(60)
    assert len(lines) == 3  # only the window
    # the selected (reversed) line shows the (N/M) indicator
    selected = next(ln for ln in lines if "\x1b[7m" in ln)
    assert "(6/8)" in selected


def test_render_no_indicator_when_fits():
    sl = SelectList(_items(3), max_visible=5)
    lines = sl.render(60)
    assert not any("(0/0)" in ln or "(1/3)" in ln for ln in lines)  # no overflow indicator


def test_render_truncates_long_label_and_description():
    long = SelectItem(value="v", label="x" * 50, description="d" * 50)
    sl = SelectList([long])
    lines = sl.render(30)
    assert len(lines) == 1
    assert visible_width(strip_ansi(lines[0])) <= 30


def test_get_selected_returns_right_item():
    sl = SelectList(_items(3))
    sl.set_selected_index(2)
    sel = sl.get_selected()
    assert sel is not None
    assert sel.value == "v2"
