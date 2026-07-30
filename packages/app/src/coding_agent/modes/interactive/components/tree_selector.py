"""TreeSelectorComponent —— 会话树浏览/切换组件。

用户输入 ``/tree`` 后，编辑器被替换为该组件。它把整棵会话树扁平化成
带 ``├─`` / ``└─`` / ``│`` 缩进的行，高亮当前叶所在路径（活跃分支），
用户用 ↑↓ 移动、Enter 切换、Esc 取消。

布局（自上而下）::

    ─── 顶部边框 ───
      会话树
      <提示行>
    ─── 分隔边框 ───
      <扁平化树行>
      <扁平化树行>
      (当前位置/总数)
    ─── 底部边框 ───

组件结构与 ModelSelectorComponent 对齐：单组件自渲染，``focused`` 属性
配合 InteractiveMode._swap_editor_for 挂载。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, List

from agent_tui.keys import matches_key
from agent_tui.theme import Theme
from agent_tui.utils import slice_by_column, truncate_to_width, visible_width

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
    ``entry`` 保留原始条目，供渲染阶段按 message role 着色；
    ``text`` 是已经拼好缩进 + 连接符 + label 的完整文本；
    ``anchor_col`` 是 label 在 ``text`` 中的可见起始列，用于窄终端下
    水平平移，确保选中项正文仍然可见；
    ``on_active_path`` 表示该行是否在当前叶 → 根的活跃路径上。
    """
    entry_id: str
    entry: SessionEntry
    text: str
    anchor_col: int
    on_active_path: bool


@dataclass(frozen=True)
class _Gutter:
    """一个祖先分叉在后代行中留下的竖向连接槽。"""

    position: int
    show: bool


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
        self._tool_calls = _collect_tool_calls(self._sm.entries)

        # 先把隐藏条目的后代重新挂到最近可见祖先，再计算连接符；否则
        # tool-call-only assistant 被隐藏后会留下没有父节点的缩进空洞。
        visible_tree = _build_visible_tree(
            self._sm.get_tree(),
            current_leaf_id=getattr(self._sm, "leaf_id", None),
        )
        self._rows: list[_FlatRow] = self._flatten_roots(visible_tree)

        # 默认选中当前叶。
        self._selected_index = 0
        leaf_id = getattr(self._sm, "leaf_id", None)
        if leaf_id is not None:
            for i, row in enumerate(self._rows):
                if row.entry_id == leaf_id:
                    self._selected_index = i
                    break

        self._scroll_offset = 0
        self._scroll_to_selection()
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
        """以显式栈按 DFS 顺序扁平化整棵树。

        普通单子节点链保持同一文本列；只有父节点出现多个 children 时才
        绘制连接符并增加视觉深度。分叉后的第一代后裔额外内缩一层，让各
        分支的线性尾部形成稳定的视觉分组，之后的单链不再继续向右漂移。
        多根场景等价于一个不可见的分叉根。
        """
        out: list[_FlatRow] = []
        multiple_roots = len(roots) > 1
        # node, indent, just_branched, show_connector, is_last, gutters
        stack: list[
            tuple[SessionTreeNode, int, bool, bool, bool, tuple[_Gutter, ...]]
        ] = []

        for index in range(len(roots) - 1, -1, -1):
            stack.append((
                roots[index],
                1 if multiple_roots else 0,
                multiple_roots,
                multiple_roots,
                index == len(roots) - 1,
                (),
            ))

        while stack:
            node, indent, just_branched, show_connector, is_last, gutters = stack.pop()
            prefix = _build_tree_prefix(
                indent=indent,
                show_connector=show_connector,
                is_last=is_last,
                gutters=gutters,
            )
            on_active_path = node.entry.id in self._active_path_ids
            path_marker = "• " if on_active_path else "  "
            out.append(_FlatRow(
                entry_id=node.entry.id,
                entry=node.entry,
                text=(
                    f"{prefix}{path_marker}"
                    f"{_format_entry_label(node.entry, self._tool_calls)}"
                ),
                anchor_col=visible_width(prefix) + 2,
                on_active_path=on_active_path,
            ))

            children = node.children
            multiple_children = len(children) > 1
            if multiple_children:
                child_indent = indent + 1
            elif just_branched and indent > 0:
                child_indent = indent + 1
            else:
                child_indent = indent

            child_gutters = gutters
            if show_connector:
                child_gutters = (
                    *gutters,
                    _Gutter(position=max(0, indent - 1), show=not is_last),
                )

            for index in range(len(children) - 1, -1, -1):
                stack.append((
                    children[index],
                    child_indent,
                    multiple_children,
                    multiple_children,
                    index == len(children) - 1,
                    child_gutters,
                ))

        return out

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
        title = truncate_to_width("会话树", content_width, ellipsis="…")
        hint = truncate_to_width(
            "↑↓ 选择 · Enter 切换分支 · Esc 取消",
            content_width,
            ellipsis="…",
        )
        if total == 0:
            return [
                border,
                indent + title,
                indent + hint,
                border,
                indent + truncate_to_width(
                    "（会话树为空）",
                    content_width,
                    ellipsis="…",
                ),
                indent + "(0/0)",
                border,
            ]

        lines: list[str] = [
            border,
            indent + title,
            indent + hint,
            border,
        ]

        end = min(self._scroll_offset + self._max_visible, total)
        visible_rows = self._rows[self._scroll_offset:end]
        horizontal_scroll = _horizontal_scroll_for_selection(
            visible_rows,
            selected_entry_id=self._rows[self._selected_index].entry_id,
            viewport_width=content_width,
        )
        for i in range(self._scroll_offset, end):
            row = self._rows[i]
            is_selected = (i == self._selected_index)
            line = _style_entry_text(self._theme, row.entry, row.text)
            if horizontal_scroll:
                line = slice_by_column(
                    line,
                    horizontal_scroll,
                    content_width,
                    strict=True,
                )
            line = truncate_to_width(line, content_width, ellipsis="")
            gutter = "› " if is_selected else indent
            rendered = gutter + line
            # 选中行把导航箭头与正文一起反色，和 Pi 的固定 gutter 一致。
            if is_selected:
                rendered = f"\x1b[7m{rendered}\x1b[0m"
            lines.append(rendered)

        status = f"({self._selected_index + 1}/{total})"
        lines.append(indent + truncate_to_width(status, content_width))
        lines.append(border)
        return lines


# ── 模块级辅助 ────────────────────────────────────────────────────────────


def _build_tree_prefix(
    *,
    indent: int,
    show_connector: bool,
    is_last: bool,
    gutters: tuple[_Gutter, ...],
) -> str:
    """按三列一层构造连接符前缀。"""
    if indent <= 0:
        return ""

    gutter_by_position = {gutter.position: gutter.show for gutter in gutters}
    connector_position = indent - 1 if show_connector else -1
    parts: list[str] = []
    for position in range(indent):
        if position in gutter_by_position:
            parts.append("│  " if gutter_by_position[position] else "   ")
        elif position == connector_position:
            parts.append("└─ " if is_last else "├─ ")
        else:
            parts.append("   ")
    return "".join(parts)


def _horizontal_scroll_for_selection(
    rows: list[_FlatRow],
    *,
    selected_entry_id: str,
    viewport_width: int,
) -> int:
    """仅在选中项正文会被缩进挤出屏幕时水平平移树正文。"""
    if viewport_width <= 0 or not rows:
        return 0

    selected = next(
        (row for row in rows if row.entry_id == selected_entry_id),
        None,
    )
    if selected is None:
        return 0

    max_body_width = max(visible_width(row.text) for row in rows)
    max_scroll = max(0, max_body_width - viewport_width)
    if max_scroll == 0:
        return 0

    min_visible_content = min(
        20,
        max(4, viewport_width // 3),
    )
    if selected.anchor_col <= viewport_width - min_visible_content:
        return 0

    anchor_context = min(
        12,
        max(2, viewport_width // 4),
    )
    return min(max_scroll, max(0, selected.anchor_col - anchor_context))


def _build_visible_tree(
    roots: list[SessionTreeNode],
    *,
    current_leaf_id: str | None,
) -> list[SessionTreeNode]:
    """Reconnect descendants of hidden nodes to their nearest visible parent."""
    visible_roots: list[SessionTreeNode] = []
    stack: list[tuple[SessionTreeNode, SessionTreeNode | None]] = [
        (root, None) for root in reversed(roots)
    ]
    while stack:
        node, visible_parent = stack.pop()
        next_parent = visible_parent
        if _should_show_entry(
            node.entry,
            is_current_leaf=node.entry.id == current_leaf_id,
        ):
            visible_node = SessionTreeNode(entry=node.entry, children=[])
            if visible_parent is None:
                visible_roots.append(visible_node)
            else:
                visible_parent.children.append(visible_node)
            next_parent = visible_node
        for child in reversed(node.children):
            stack.append((child, next_parent))
    return visible_roots


def _collect_tool_calls(
    entries: list[SessionEntry],
) -> dict[str, tuple[str, dict[str, Any]]]:
    """Index assistant ToolCall blocks so results can show calls, not output."""
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    for entry in entries:
        if not isinstance(entry, SessionMessageEntry) or entry.message is None:
            continue
        if getattr(entry.message, "role", None) != "assistant":
            continue
        content = getattr(entry.message, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            if getattr(block, "type", None) != "toolCall":
                continue
            call_id = getattr(block, "id", "")
            if not call_id:
                continue
            name = getattr(block, "name", "") or "tool"
            arguments = getattr(block, "arguments", {})
            calls[call_id] = (
                name,
                arguments if isinstance(arguments, dict) else {},
            )
    return calls


def _format_tool_call(name: str, arguments: dict[str, Any]) -> str:
    """Compact common tool arguments using the same information Pi displays."""
    if name in ("read", "write", "edit"):
        path = arguments.get("path") or arguments.get("file_path") or ""
        return f"[{name}: {path}]" if path else f"[{name}]"
    if name in ("bash", "shell", "shell_command"):
        raw_command = str(arguments.get("command") or "")
        command = raw_command.replace("\n", " ").replace("\t", " ").strip()
        suffix = "…" if len(command) > 50 else ""
        return f"[{name}: {command[:50]}{suffix}]" if command else f"[{name}]"
    if name == "grep":
        pattern = arguments.get("pattern") or ""
        path = arguments.get("path") or "."
        return f"[grep: /{pattern}/ in {path}]"
    if name in ("find", "ls"):
        path = arguments.get("path") or "."
        pattern = arguments.get("pattern")
        if pattern:
            return f"[{name}: {pattern} in {path}]"
        return f"[{name}: {path}]"
    if not arguments:
        return f"[{name}]"
    encoded = json.dumps(arguments, ensure_ascii=False, default=str)
    suffix = "…" if len(encoded) > 40 else ""
    return f"[{name}: {encoded[:40]}{suffix}]"


def _should_show_entry(
    entry: SessionEntry,
    *,
    is_current_leaf: bool,
) -> bool:
    """Apply Pi's default visibility rule for tool-only assistant turns."""
    if not isinstance(entry, SessionMessageEntry) or entry.message is None:
        return True
    message = entry.message
    if getattr(message, "role", None) != "assistant" or is_current_leaf:
        return True
    if _extract_message_text(message).strip():
        return True
    if getattr(message, "error_message", None):
        return True
    stop_reason = getattr(message, "stop_reason", None)
    return bool(
        stop_reason
        and stop_reason not in ("stop", "tool_use", "toolUse")
    )


def _style_entry_text(theme: Theme, entry: SessionEntry, text: str) -> str:
    """Color visible tree rows by semantic role instead of active-path state."""
    if isinstance(entry, SessionMessageEntry) and entry.message is not None:
        role = getattr(entry.message, "role", None)
        if role == "user":
            return theme.fg("accent", text)
        if role == "assistant":
            return theme.fg("success", text)
        if role == "toolResult":
            return theme.fg("muted", text)
    if isinstance(entry, CompactionEntry):
        return theme.fg("warning", text)
    return theme.fg("muted", text)


def _format_entry_label(
    entry: SessionEntry,
    tool_calls: dict[str, tuple[str, dict[str, Any]]] | None = None,
) -> str:
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
        if role == "toolResult":
            call_id = getattr(entry.message, "tool_call_id", "")
            tool_call = tool_calls.get(call_id) if tool_calls and call_id else None
            tool_name = getattr(entry.message, "tool_name", "") or "tool"
            error_prefix = "错误: " if getattr(entry.message, "is_error", False) else ""
            if tool_call:
                formatted = _format_tool_call(*tool_call)
                return f"[错误] {formatted}" if error_prefix else formatted
            return f"[{error_prefix}{tool_name}]"
        role_label = {
            "user": "user",
            "assistant": "assistant",
        }.get(role, role or "?")
        text = _extract_message_text(entry.message)
        snippet = text.strip().replace("\n", " ")[:200]
        if role == "assistant" and not snippet:
            error = getattr(entry.message, "error_message", None)
            if error:
                return f"assistant: {str(error).strip()[:60]}"
            if getattr(entry.message, "stop_reason", None) == "aborted":
                return "assistant: (已中止)"
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
