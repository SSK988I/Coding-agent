"""TreeSelectorComponent —— 会话树浏览/切换组件。

用户输入 ``/tree`` 后，编辑器被替换为该组件。它把整棵会话树扁平化成
带 ``├─`` / ``└─`` / ``│`` 缩进的行，高亮当前叶所在路径（活跃分支），
用户用 ↑↓ 移动、Enter 切换、Esc 取消。

布局（自上而下）::

    ─── 顶部边框 ───
      <提示行>
      <扁平化树行>
      <扁平化树行>
    ─── 底部边框 ───

组件结构与 ModelSelectorComponent 对齐：单组件自渲染，``focused`` 属性
配合 InteractiveMode._swap_editor_for 挂载。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List

from agent_tui.keys import matches_key
from agent_tui.theme import Theme
from agent_tui.utils import visible_width

from agent_core.session.types import (
    CompactionEntry,
    ModelChangeEntry,
    SessionEntry,
    SessionInfoEntry,
    SessionMessageEntry,
    SessionTreeNode,
    ThinkingLevelChangeEntry,
)


@dataclass
class _FlatRow:
    """扁平化后的一行。

    ``entry_id`` 是对应 entry 的 id（用于回传给 on_select）；
    ``text`` 是已经拼好缩进 + 连接符 + label 的完整文本；
    ``on_active_path`` 表示该行是否在当前叶 → 根的活跃路径上。
    """
    entry_id: str
    text: str
    on_active_path: bool


class TreeSelectorComponent:
    """会话树选择器。

    Args:
        theme: 用于边框颜色 + 活跃路径着色。
        session_manager: 提供整棵树与当前叶指针。
        on_select: ``callback(entry_id: str)`` —— 用户按 Enter 选中某行时触发。
        on_cancel: ``callback()`` —— 用户按 Esc / Ctrl+C 取消时触发。
        max_visible: 树行区域最多显示多少行，超出滚动。
        border_color_fn: 边框着色函数，默认用 theme.fg("border", ...)。
    """

    def __init__(
        self,
        theme: Theme,
        session_manager: Any,
        on_select: "Callable[[str], None]",
        on_cancel: "Callable[[], None]",
        *,
        max_visible: int = 12,
        border_color_fn: "Callable[[str], str] | None" = None,
    ) -> None:
        self._theme = theme
        self._sm = session_manager
        self._on_select = on_select
        self._on_cancel = on_cancel
        self._border_color_fn = border_color_fn or (lambda s: theme.fg("border", s))
        self._max_visible = max(1, max_visible)

        # 活跃路径：从当前叶沿 parent_id 上溯到根的所有 entry.id。
        self._active_path_ids: set[str] = self._compute_active_path()

        # 扁平化整棵树。多根时把所有根当成虚拟根的 children，
        # 这样根之间也能用 ├─ / └─ 区分先后。
        self._rows: list[_FlatRow] = self._flatten_roots(self._sm.get_tree())

        # 默认选中当前叶。
        self._selected_index = 0
        leaf_id = getattr(self._sm, "leaf_id", None)
        if leaf_id is not None:
            for i, row in enumerate(self._rows):
                if row.entry_id == leaf_id:
                    self._selected_index = i
                    break

        self._scroll_offset = 0
        self.focused = True

    # ── 活跃路径 ──────────────────────────────────────────────────────────

    def _compute_active_path(self) -> set[str]:
        """从当前叶沿 parent_id 上溯到根，返回路径上所有 entry.id。"""
        path: set[str] = set()
        leaf_id = getattr(self._sm, "leaf_id", None)
        if leaf_id is None:
            return path
        by_id = {e.id: e for e in self._sm.entries}
        cur: str | None = leaf_id
        seen: set[str] = set()
        while cur is not None and cur in by_id and cur not in seen:
            seen.add(cur)
            path.add(cur)
            cur = by_id[cur].parent_id
        return path

    # ── 扁平化 ────────────────────────────────────────────────────────────

    def _flatten_roots(self, roots: list[SessionTreeNode]) -> list[_FlatRow]:
        """多根场景：把所有根当成一个虚拟根的 children。

        单根时与普通树一样。多根时，根之间用 ``├─`` / ``└─`` 连接符区分。
        """
        out: list[_FlatRow] = []
        n = len(roots)
        for i, root in enumerate(roots):
            self._flatten(root, is_last=(i == n - 1), prefix_gutters="", out=out)
        return out

    def _flatten(
        self,
        node: SessionTreeNode,
        *,
        is_last: bool,
        prefix_gutters: str,
        out: list[_FlatRow],
    ) -> None:
        """DFS 扁平化一棵子树。

        ``prefix_gutters`` 是该节点之前所有祖先层的"竖线 / 空白"前缀
        （每层 3 个字符：``│  `` 或 ``   ``）。
        ``is_last`` 表示该节点是不是它父亲的最后一个 child（决定用
        ``└─`` 还是 ``├─``）。
        """
        connector = "└─ " if is_last else "├─ "
        label = _format_entry_label(node.entry)
        text = f"{prefix_gutters}{connector}{label}"
        out.append(_FlatRow(
            entry_id=node.entry.id,
            text=text,
            on_active_path=node.entry.id in self._active_path_ids,
        ))
        children = node.children
        for i, child in enumerate(children):
            child_is_last = (i == len(children) - 1)
            # 当前节点用 └─ 时，下层前缀补 "   "；用 ├─ 时补 "│  "。
            new_gutters = prefix_gutters + ("   " if is_last else "│  ")
            self._flatten(child, is_last=child_is_last, prefix_gutters=new_gutters, out=out)

    # ── 输入 ──────────────────────────────────────────────────────────────

    def handle_input(self, data: str) -> bool:
        """Esc / Ctrl+C 取消；Enter 选中；↑↓ 移动。"""
        if matches_key(data, "escape") or matches_key(data, "ctrl+c"):
            self._on_cancel()
            return True
        if matches_key(data, "enter"):
            if self._rows:
                self._on_select(self._rows[self._selected_index].entry_id)
            else:
                self._on_cancel()
            return True
        if matches_key(data, "up"):
            self._move(-1)
            return True
        if matches_key(data, "down"):
            self._move(1)
            return True
        return False

    def _move(self, delta: int) -> None:
        if not self._rows:
            return
        n = len(self._rows)
        self._selected_index = (self._selected_index + delta) % n
        self._scroll_to_selection()

    def _scroll_to_selection(self) -> None:
        if self._selected_index < self._scroll_offset:
            self._scroll_offset = self._selected_index
        elif self._selected_index >= self._scroll_offset + self._max_visible:
            self._scroll_offset = self._selected_index - self._max_visible + 1
        max_scroll = max(0, len(self._rows) - self._max_visible)
        self._scroll_offset = max(0, min(self._scroll_offset, max_scroll))

    # ── 渲染 ──────────────────────────────────────────────────────────────

    def render(self, width: int) -> List[str]:
        border = self._border_color_fn("─" * width)
        indent = "  "
        content_width = max(1, width - len(indent))

        total = len(self._rows)
        if total == 0:
            hint = "（会话树为空）"
            return [border, indent + hint, border]

        hint = (
            f"会话树: ↑↓ 选择, Enter 切换分支, Esc 取消"
            f"  ({self._selected_index + 1}/{total})"
        )
        hint = _truncate_to_width(hint, content_width)

        lines: list[str] = [border, indent + hint]

        end = min(self._scroll_offset + self._max_visible, total)
        for i in range(self._scroll_offset, end):
            row = self._rows[i]
            is_selected = (i == self._selected_index)
            line = row.text
            # 活跃路径用 accent 着色，让用户看清当前在哪个分支。
            if row.on_active_path:
                line = self._theme.fg("accent", line)
            # 选中行整体反色（盖在 accent 之上，符合 SelectList 既有视觉）。
            if is_selected:
                line = f"\x1b[7m{line}\x1b[0m"
            line = _truncate_to_width(line, content_width)
            lines.append(indent + line)

        lines.append(border)
        return lines


# ── 模块级辅助 ────────────────────────────────────────────────────────────


def _format_entry_label(entry: SessionEntry) -> str:
    """把一个 entry 渲染成单行 label。

    - SessionMessageEntry: ``user: <文本前 N 字>`` / ``assistant: <...>``
    - CompactionEntry: ``[压缩]``
    - ModelChangeEntry: ``[模型: provider/model_id]``
    - ThinkingLevelChangeEntry: ``[思考级别: X]``
    - SessionInfoEntry: ``[命名: X]``
    - 其他: ``[<type>]``
    """
    if isinstance(entry, SessionMessageEntry):
        role = getattr(entry.message, "role", None) if entry.message else None
        role_label = {
            "user": "user",
            "assistant": "assistant",
            "tool": "tool",
        }.get(role, role or "?")
        text = _extract_message_text(entry.message)
        snippet = text.strip().replace("\n", " ")[:60]
        return f"{role_label}: {snippet}" if snippet else f"{role_label}: (空)"
    if isinstance(entry, CompactionEntry):
        return "[压缩]"
    if isinstance(entry, ModelChangeEntry):
        return f"[模型: {entry.provider}/{entry.model_id}]"
    if isinstance(entry, ThinkingLevelChangeEntry):
        return f"[思考级别: {entry.thinking_level}]"
    if isinstance(entry, SessionInfoEntry):
        return f"[命名: {entry.name}]"
    return f"[{getattr(entry, 'type', '?')}]"


def _extract_message_text(message: Any) -> str:
    """从 Message 对象抽纯文本，兼容 str / list[Block] 两种 content 形态。"""
    if message is None:
        return ""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif hasattr(block, "text"):
                parts.append(getattr(block, "text", "") or "")
        return " ".join(parts)
    return ""


def _truncate_to_width(text: str, width: int) -> str:
    """按可见宽度截断，超出末尾加 ``…``。ANSI 转义序列按 0 宽度处理。"""
    if visible_width(text) <= width:
        return text
    out = ""
    for ch in text:
        if visible_width(out + ch) >= width:
            return out + "…"
        out += ch
    return out
