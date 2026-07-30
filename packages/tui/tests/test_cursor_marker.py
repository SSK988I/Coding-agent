"""CURSOR_MARKER 提取和硬件光标定位测试。

这些测试用于验证 IME 候选窗口定位修复。如果没有相关辅助函数，Windows IME
会读取控制台的硬件光标位置，并把候选窗口放到屏幕右下角。在原始输入模式下，
硬件光标只会停在最后一次写入结束的位置，而不是编辑器的逻辑光标位置。

修复流程：
  1. 已聚焦组件（Editor / TextInput）在渲染结果的光标位置输出
     ``CURSOR_MARKER``。
  2. ``_extract_cursor_position`` 查找标记、计算可见列（CJK 字符占两列）、
     原地移除标记并返回行列坐标。
  3. ``_position_hardware_cursor`` 输出 VT 序列，把终端真实光标移动到对应坐标，
     Windows IME 的候选窗口随之移动。

测试使用终端替身捕获输出字节，不需要真实控制台。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_tui.tui import CURSOR_MARKER, TUI


class _CapturingTerminal:
    """记录每次写入的终端替身，用于断言 VT 字节。"""

    def __init__(self, cols: int = 80, rows: int = 24):
        self.columns = cols
        self.rows = rows
        self.kitty_protocol_active = False
        self.output: list[str] = []
        self._cursor_hidden = False
        self._cursor_shown = False

    def write(self, data: str) -> None:
        self.output.append(data)

    def flush(self) -> None:
        pass

    def hide_cursor(self) -> None:
        self._cursor_hidden = True
        self._cursor_shown = False

    def show_cursor(self) -> None:
        self._cursor_shown = True
        self._cursor_hidden = False


def _tui():
    """构建不启动读取循环的 TUI。"""
    term = _CapturingTerminal()
    return TUI(term), term


# ─── _extract_cursor_position ───────────────────────────────────────────


def test_extract_finds_marker_and_returns_position():
    t, _ = _tui()
    lines = [
        "第一行",
        "> abc" + CURSOR_MARKER + "\x1b[7m \x1b[0m",
        "页脚",
    ]
    pos = t._extract_cursor_position(lines, height=24)
    assert pos == (1, 5)   # 第 1 行、第 5 列（位于 "> abc" 之后）


def test_extract_strips_marker_in_place():
    """刷新前必须从行内容中移除标记字节。"""
    t, _ = _tui()
    lines = ["正文" + CURSOR_MARKER + "末尾"]
    t._extract_cursor_position(lines, height=24)
    assert CURSOR_MARKER not in lines[0]
    assert lines[0] == "正文末尾"


def test_extract_returns_none_when_no_marker():
    t, _ = _tui()
    lines = ["普通", "文本", "这里没有标记"]
    assert t._extract_cursor_position(lines, height=24) is None


def test_extract_returns_none_for_empty_lines():
    t, _ = _tui()
    assert t._extract_cursor_position([], height=24) is None


def test_extract_computes_cjk_width_as_two_columns():
    """每个 CJK 字符占两列，列位置计算必须正确处理。"""
    t, _ = _tui()
    # “你”和“好”各占两列，因此其后的标记位于第 4 列。
    lines = ["你好" + CURSOR_MARKER]
    pos = t._extract_cursor_position(lines, height=24)
    assert pos == (0, 4)


def test_extract_ignores_ansi_codes_in_column_math():
    """颜色转义序列宽度为零，列位置只能计算可见字符。"""
    t, _ = _tui()
    # 红色转义序列 \x1b[31m 不占可见宽度，ab 占两列，因此结果为第 2 列。
    lines = ["\x1b[31mab" + CURSOR_MARKER]
    pos = t._extract_cursor_position(lines, height=24)
    assert pos == (0, 2)


def test_extract_finds_last_marker_when_multiple():
    """多行包含标记时，由于从下向上遍历，应采用最后一行的标记。"""
    t, _ = _tui()
    lines = [
        "a" + CURSOR_MARKER,   # 第 0 行、第 1 列
        "b" + CURSOR_MARKER,   # 第 1 行、第 1 列，应采用这一项
    ]
    pos = t._extract_cursor_position(lines, height=24)
    assert pos == (1, 1)


def test_extract_respects_viewport_height():
    """忽略可见视口上方（已经滚出屏幕）的标记。"""
    t, _ = _tui()
    # 共 5 行、视口高度为 3，因此只有第 2、3、4 行可见。
    lines = [
        "屏幕外" + CURSOR_MARKER,  # 第 0 行，在视口上方，应忽略
        "x",
        "x",
        "x",
        "visible" + CURSOR_MARKER,     # 第 4 行，在视口内
    ]
    pos = t._extract_cursor_position(lines, height=3)
    assert pos == (4, 7)


# ─── _position_hardware_cursor ──────────────────────────────────────────


def test_position_writes_row_and_column_sequences():
    t, term = _tui()
    t._hardware_cursor_row = 0
    t._position_hardware_cursor((3, 10), total_lines=20)
    # 向下移动三行，再移动到第 11 列（CHA 从 1 开始计数）。
    out = "".join(term.output)
    assert "\x1b[3B" in out   # 向下移动三行
    assert "\x1b[11G" in out  # CHA 第 11 列（10 + 1）


def test_position_moves_up_when_target_above_current():
    t, term = _tui()
    t._hardware_cursor_row = 5
    t._position_hardware_cursor((2, 0), total_lines=10)
    out = "".join(term.output)
    assert "\x1b[3A" in out   # 向上移动三行（5 → 2）
    assert "\x1b[1G" in out   # CHA 第 1 列（0 + 1）


def test_position_clamps_row_to_total_lines():
    """target_row 超过末行时应限制到最后一行。"""
    t, term = _tui()
    t._hardware_cursor_row = 0
    t._position_hardware_cursor((100, 0), total_lines=5)
    # 限制到第 4 行（最后一行），因此向下移动四行。
    out = "".join(term.output)
    assert "\x1b[4B" in out
    assert t._hardware_cursor_row == 4


def test_position_hides_cursor_when_no_marker():
    """没有 cursor_pos 时完全隐藏硬件光标。"""
    t, term = _tui()
    t._position_hardware_cursor(None, total_lines=10)
    assert term._cursor_hidden is True


def test_position_hides_cursor_when_no_lines():
    """total_lines 为 0 时没有有效位置，应隐藏光标。"""
    t, term = _tui()
    t._position_hardware_cursor((0, 0), total_lines=0)
    assert term._cursor_hidden is True


def test_position_negative_col_clamped_to_zero():
    """target_col 小于 0 时进行防御性限制，最终使用 CHA 第 1 列。"""
    t, term = _tui()
    t._hardware_cursor_row = 0
    t._position_hardware_cursor((0, -5), total_lines=10)
    out = "".join(term.output)
    assert "\x1b[1G" in out   # 第 0 列对应 CHA 第 1 列


def test_position_updates_hardware_cursor_row_state():
    """定位后应记录新的硬件光标行，供下次渲染正确计算行差。"""
    t, _ = _tui()
    t._hardware_cursor_row = 0
    t._position_hardware_cursor((7, 0), total_lines=20)
    assert t._hardware_cursor_row == 7
    # 第二次调用应从新位置开始计算。
    t._position_hardware_cursor((3, 0), total_lines=20)
    assert t._hardware_cursor_row == 3


# ─── Integration: extract + position together ───────────────────────────


def test_extract_then_position_round_trip():
    """完整流程：提取标记，再定位硬件光标。"""
    t, term = _tui()
    t._hardware_cursor_row = 0
    lines = [
        "页头",
        "> hello" + CURSOR_MARKER + "\x1b[7m \x1b[0m",   # 位于 "> hello" 后的第 7 列
        "页脚",
    ]
    pos = t._extract_cursor_position(lines, height=24)
    assert pos == (1, 7)
    assert CURSOR_MARKER not in lines[1]   # 标记已移除
    t._position_hardware_cursor(pos, total_lines=len(lines))
    out = "".join(term.output)
    assert "\x1b[1B" in out    # 向下移动一行（0 → 1）
    assert "\x1b[8G" in out    # CHA 第 8 列（7 + 1）


def test_cursorless_selector_navigation_rewrites_current_frame():
    """没有 CURSOR_MARKER 的选择器不能追加每次方向键产生的画面。

    打开选择器会替换已聚焦的编辑器，因此首次差分渲染结束于编辑器旧光标行
    的下方。后续导航渲染必须从实际末行向上移动并覆盖当前选择器。
    """

    class _Frame:
        def __init__(self, lines: list[str]) -> None:
            self.lines = lines

        def render(self, _width: int) -> list[str]:
            return list(self.lines)

    t, term = _tui()
    frame = _Frame(["对话", "编辑器" + CURSOR_MARKER])
    t.add_child(frame)
    t._stopped = False

    # 初始编辑器已聚焦：硬件光标位于第 1 行。
    t._do_render()
    assert t._hardware_cursor_row == 1

    # 打开无光标的会话树选择器后，渲染内容写到第 5 行。
    frame.lines = [
        "对话",
        "边框",
        "会话树 (2/2)",
        "用户",
        "助手（已选择）",
        "边框",
    ]
    t._do_render()
    assert t._hardware_cursor_row == 5

    # 移动选择会改变第 2～4 行。渲染必须从第 5 行向上移动三行；如果仍使用
    # 过期的编辑器行号，就会向下移动并追加输出。
    term.output.clear()
    frame.lines = [
        "对话",
        "边框",
        "会话树 (1/2)",
        "用户（已选择）",
        "助手",
        "边框",
    ]
    t._do_render()

    output = "".join(term.output)
    assert "\x1b[3A" in output
    assert t._hardware_cursor_row == 4


# ─── Cursor visibility (show_hardware_cursor flag) ──────────────────────


def test_position_shows_cursor_when_flag_enabled():
    """show_hardware_cursor 为 True 时，定位后应显示硬件光标。"""
    t, term = _tui()
    t.set_show_hardware_cursor(True)
    t._hardware_cursor_row = 0
    t._position_hardware_cursor((1, 0), total_lines=10)
    assert term._cursor_shown is True


def test_position_hides_cursor_by_default():
    """默认行为：为 IME 定位光标，但视觉上保持隐藏。"""
    t, term = _tui()
    # _show_hardware_cursor 默认为 False。
    t._hardware_cursor_row = 0
    t._position_hardware_cursor((1, 0), total_lines=10)
    assert term._cursor_hidden is True


# ─── Viewport-aware differential rendering ─────────────────────────────


class _MutableFrame:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.render_widths: list[int] = []

    def render(self, width: int) -> list[str]:
        self.render_widths.append(width)
        return list(self.lines)


def _rendering_tui(
    lines: list[str],
    *,
    cols: int = 80,
    rows: int = 4,
) -> tuple[TUI, _CapturingTerminal, _MutableFrame]:
    term = _CapturingTerminal(cols=cols, rows=rows)
    frame = _MutableFrame(lines)
    tui = TUI(term)
    tui.add_child(frame)
    tui._stopped = False
    return tui, term, frame


def test_full_frame_tracks_scrolled_viewport_top():
    tui, _, _ = _rendering_tui([f"line {i}" for i in range(8)], rows=4)

    tui._do_render()

    assert tui._previous_viewport_top == 4
    assert tui._hardware_cursor_row == 7
    assert (
        tui._hardware_cursor_row - tui._previous_viewport_top
        == 3
    )


def test_append_at_viewport_bottom_uses_crlf_to_scroll():
    tui, term, frame = _rendering_tui(
        [f"line {i}" for i in range(4)],
        rows=4,
    )
    tui._do_render()
    term.output.clear()

    frame.lines.append("line 4")
    tui._do_render()

    output = "".join(term.output)
    assert "\x1b[?2026h\r\n\x1b[2K" in output
    assert tui._previous_viewport_top == 1
    assert tui._hardware_cursor_row == 4
    assert (
        tui._hardware_cursor_row - tui._previous_viewport_top
        == 3
    )


def test_change_above_viewport_forces_safe_full_redraw():
    tui, term, frame = _rendering_tui(
        [f"line {i}" for i in range(8)],
        rows=4,
    )
    tui._do_render()
    redraws_before = tui.full_redraws
    term.output.clear()

    frame.lines[2] = "changed above viewport"
    tui._do_render()

    assert tui.full_redraws == redraws_before + 1
    assert "\x1b[2J\x1b[H\x1b[3J" in "".join(term.output)
    assert tui._previous_viewport_top == 4


def test_shrink_ignores_cursor_marker_above_actual_viewport():
    tui, _, frame = _rendering_tui(
        [f"line {i}" for i in range(8)],
        rows=4,
    )
    tui._do_render()
    assert tui._previous_viewport_top == 4

    # Shrinking to five lines leaves the real viewport anchored at logical
    # row 4, with blank rows below. A marker on row 3 is therefore not
    # reachable even though it is in the bottom four rows of new content.
    frame.lines = [
        "line 0",
        "line 1",
        "line 2",
        "line 3" + CURSOR_MARKER,
        "line 4",
    ]
    tui._do_render()

    assert tui._previous_viewport_top == 4
    assert tui._hardware_cursor_row == 4
    assert all(CURSOR_MARKER not in line for line in tui._previous_lines)


def test_immediate_wrap_terminal_reserves_last_column():
    tui, term, frame = _rendering_tui(
        ["X" * 10],
        cols=10,
        rows=4,
    )
    term.delayed_wrap_supported = False

    tui._do_render()

    output = "".join(term.output)
    assert frame.render_widths == [9]
    assert "X" * 10 not in output
    assert "X" * 9 in output
