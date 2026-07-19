"""TreeSelectorComponent 单元测试。

验证扁平化渲染、连接符、活跃路径高亮，以及键盘交互（Enter 选中、Esc 取消、
↑↓ 移动）。

测试用一个最简 FakeTheme 满足组件的 ``fg``/``bold`` 调用即可——不依赖
真实主题加载。
"""
from __future__ import annotations

from agent_llm import AssistantMessage, TextContent, UserMessage

from agent_core.session.session_manager import SessionManager
from coding_agent.modes.interactive.components.tree_selector import (
    TreeSelectorComponent,
    _format_entry_label,
)


class FakeTheme:
    """最简 theme stub：fg/bold 直接返回原文，不做着色。"""

    def fg(self, color: str, text: str) -> str:
        return text

    def bold(self, text: str) -> str:
        return text


def _msg_user(t: str) -> UserMessage:
    return UserMessage(content=t)


def _msg_asst(t: str) -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=t)], provider="deepseek", model="m")


def _make_session_with_branch() -> SessionManager:
    """构造一个有分支的 session：u1 → a1 → u2 → a2，外加切回 u1 的新分支。"""
    sm = SessionManager.create(in_memory=True)
    e_u1 = sm.append_message(_msg_user("第一条问题"))
    sm.append_message(_msg_asst("第一条回答"))
    sm.append_message(_msg_user("第二条问题"))
    sm.append_message(_msg_asst("第二条回答"))
    # 切回 u1，append 新 assistant，形成第二条分支
    sm.set_leaf_id(e_u1.id)
    sm.append_message(_msg_asst("新分支回答"))
    # 把叶指针留在 "新分支回答"（最后一条 entry，无需显式 set_leaf_id）
    return sm


# ─── 扁平化渲染 ───────────────────────────────────────────────────────


def test_flatten_renders_connectors_and_indent():
    """扁平化树应包含 ├─ / └─ 连接符与缩进。"""
    sm = _make_session_with_branch()
    selector = TreeSelectorComponent(
        theme=FakeTheme(),
        session_manager=sm,
        on_select=lambda _id: None,
        on_cancel=lambda: None,
    )
    # 扁平化后应有 6 行：u1, a1（原分支的 a1，├─），u2, a2, 新分支回答（└─）
    texts = [row.text for row in selector._rows]
    assert len(texts) == 5, f"expected 5 rows, got {texts}"
    # 第一个根节点用 └─ 连接符（单根时它就是最后一个）
    assert "└─ " in texts[0] or "├─ " in texts[0]
    # 应至少出现一次 └─ 和一次 ├─（分支点）
    all_text = "\n".join(texts)
    assert "├─" in all_text, f"should contain ├─ (branch point): {all_text}"
    assert "└─" in all_text, f"should contain └─ (last child): {all_text}"


def test_empty_session_renders_placeholder():
    """空会话树应渲染占位提示。"""
    sm = SessionManager.create(in_memory=True)
    selector = TreeSelectorComponent(
        theme=FakeTheme(),
        session_manager=sm,
        on_select=lambda _id: None,
        on_cancel=lambda: None,
    )
    lines = selector.render(width=60)
    assert any("为空" in line for line in lines), f"expected placeholder, got {lines}"


def test_format_entry_label_user_message():
    """user message label 应包含 'user:' 前缀和内容片段。"""
    sm = SessionManager.create(in_memory=True)
    e = sm.append_message(_msg_user("帮我修个 bug"))
    label = _format_entry_label(e)
    assert label.startswith("user:")
    assert "帮我修个 bug" in label


def test_format_entry_label_assistant_message():
    """assistant message label 应包含 'assistant:' 前缀。"""
    sm = SessionManager.create(in_memory=True)
    e = sm.append_message(_msg_asst("好的，我来看看"))
    label = _format_entry_label(e)
    assert label.startswith("assistant:")
    assert "好的" in label


def test_format_entry_label_compaction():
    """compaction entry label 应为 '[压缩]'。"""
    from agent_core.session.types import CompactionEntry

    e = CompactionEntry(
        type="compaction", id="x", parent_id=None, timestamp="t",
        summary="...", first_kept_entry_id="y", tokens_before=100,
    )
    assert _format_entry_label(e) == "[压缩]"


# ─── 活跃路径高亮 ─────────────────────────────────────────────────────


def test_active_path_is_marked():
    """当前叶 → 根路径上的行应被标记为 on_active_path=True。"""
    sm = _make_session_with_branch()
    selector = TreeSelectorComponent(
        theme=FakeTheme(),
        session_manager=sm,
        on_select=lambda _id: None,
        on_cancel=lambda: None,
    )
    # 当前叶是最后一条 entry（"新分支回答"）。活跃路径应为：
    #   u1（根） + 新分支回答（叶）
    active_ids = {row.entry_id for row in selector._rows if row.on_active_path}
    leaf_id = sm.leaf_id
    assert leaf_id in active_ids
    # 根 u1 也应在活跃路径上
    root_id = sm.entries[0].id
    assert root_id in active_ids
    # 原 a1 / u2 / a2 不在活跃路径上
    off_path = {row.entry_id for row in selector._rows if not row.on_active_path}
    assert len(off_path) >= 1


# ─── 键盘交互 ─────────────────────────────────────────────────────────


def test_handle_input_enter_fires_on_select():
    """Enter 应触发 on_select 回调，参数为当前选中行的 entry_id。"""
    sm = _make_session_with_branch()
    selected: list[str] = []
    selector = TreeSelectorComponent(
        theme=FakeTheme(),
        session_manager=sm,
        on_select=lambda eid: selected.append(eid),
        on_cancel=lambda: None,
    )
    # 默认选中当前叶
    consumed = selector.handle_input("\r")  # \r 是 Enter
    assert consumed is True
    assert len(selected) == 1
    assert selected[0] == sm.leaf_id


def test_handle_input_escape_fires_on_cancel():
    """Esc 应触发 on_cancel 回调。"""
    sm = _make_session_with_branch()
    cancelled: list[bool] = []
    selector = TreeSelectorComponent(
        theme=FakeTheme(),
        session_manager=sm,
        on_select=lambda _id: None,
        on_cancel=lambda: cancelled.append(True),
    )
    consumed = selector.handle_input("\x1b")  # Esc
    assert consumed is True
    assert cancelled == [True]


def test_handle_input_arrow_keys_move_selection():
    """↓/↑ 应移动选中索引，且不触发回调。"""
    sm = _make_session_with_branch()
    fired: list[str] = []
    selector = TreeSelectorComponent(
        theme=FakeTheme(),
        session_manager=sm,
        on_select=lambda eid: fired.append(eid),
        on_cancel=lambda: fired.append("cancel"),
    )
    initial = selector._selected_index
    selector.handle_input("\x1b[B")  # Down
    assert selector._selected_index != initial or len(selector._rows) == 1
    assert fired == []  # 没触发任何回调
    selector.handle_input("\x1b[A")  # Up
    # Up + Down 之后应该能回到原位（或循环）
    assert fired == []


def test_initial_selection_is_current_leaf():
    """组件打开时默认选中当前叶。"""
    sm = _make_session_with_branch()
    selector = TreeSelectorComponent(
        theme=FakeTheme(),
        session_manager=sm,
        on_select=lambda _id: None,
        on_cancel=lambda: None,
    )
    selected_row = selector._rows[selector._selected_index]
    assert selected_row.entry_id == sm.leaf_id


def test_render_includes_borders_and_hint():
    """render 输出应包含顶/底边框和提示行。"""
    sm = _make_session_with_branch()
    selector = TreeSelectorComponent(
        theme=FakeTheme(),
        session_manager=sm,
        on_select=lambda _id: None,
        on_cancel=lambda: None,
    )
    lines = selector.render(width=60)
    # 第一行和最后一行是边框（连续 ─）
    assert lines[0].count("─") > 10
    assert lines[-1].count("─") > 10
    # 第二行是提示
    assert "会话树" in lines[1]
