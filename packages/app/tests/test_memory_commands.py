from __future__ import annotations

import asyncio
from types import SimpleNamespace

from coding_agent.modes.interactive.interactive_mode import InteractiveMode


class _Session:
    settings_manager = None

    def __init__(self) -> None:
        self.forgotten: list[tuple[str, str | None]] = []
        self.cleared: list[bool] = []
        self.auto_extract: list[bool] = []
        self.remembered: list[tuple[str, str]] = []

    def set_memory_auto_extract(self, enabled: bool) -> None:
        self.auto_extract.append(enabled)

    async def memory_remember(self, content: str, *, scope: str) -> SimpleNamespace:
        self.remembered.append((content, scope))
        return SimpleNamespace(id="mem_manual_01")

    async def memory_forget(self, key: str, scope: str | None) -> bool:
        self.forgotten.append((key, scope))
        return True

    async def memory_clear(self, *, all_scopes: bool) -> int:
        self.cleared.append(all_scopes)
        return 3


def _mode() -> tuple[InteractiveMode, _Session, list[str]]:
    mode = object.__new__(InteractiveMode)
    session = _Session()
    messages: list[str] = []
    mode._session = session  # type: ignore[attr-defined]
    mode._add_system_message = messages.append  # type: ignore[method-assign]
    mode._add_assistant_text = messages.append  # type: ignore[method-assign]
    return mode, session, messages


def test_memory_forget_preserves_scope_during_confirmation() -> None:
    mode, session, messages = _mode()

    asyncio.run(mode._cmd_memory("forget mem_01 --global"))

    assert session.forgotten == []
    assert "/memory forget mem_01 --global --confirm" in messages[-1]

    asyncio.run(mode._cmd_memory("forget mem_01 --global --confirm"))
    assert session.forgotten == [("mem_01", "global")]
    assert messages[-1] == "记忆已遗忘并写入墓碑。"


def test_memory_clear_requires_explicit_scope_and_confirmation() -> None:
    mode, session, messages = _mode()

    asyncio.run(mode._cmd_memory("clear --all"))
    assert session.cleared == []
    assert "/memory clear --all --confirm" in messages[-1]

    asyncio.run(mode._cmd_memory("clear --all --confirm"))
    assert session.cleared == [True]
    assert messages[-1] == "已遗忘 3 条记忆，并保留墓碑记录。"


def test_memory_auto_extract_updates_the_live_session() -> None:
    mode, session, messages = _mode()

    asyncio.run(mode._cmd_memory("auto off"))

    assert session.auto_extract == [False]
    assert messages[-1] == "长期记忆自动提取已关闭。"


def test_memory_remember_uses_direct_scope_aware_path() -> None:
    mode, session, messages = _mode()

    asyncio.run(mode._cmd_memory("remember 偏好中文回答 --project"))

    assert session.remembered == [("偏好中文回答", "project")]
    assert "mem_manual_01" in messages[-1]


def test_memory_remember_rejects_ambiguous_scope() -> None:
    mode, session, messages = _mode()

    asyncio.run(mode._cmd_memory("remember 内容 --global --project"))

    assert session.remembered == []
    assert "/memory remember" in messages[-1]


def test_memory_remember_keeps_flag_shaped_content_before_trailing_scope() -> None:
    mode, session, _messages = _mode()

    asyncio.run(mode._cmd_memory(
        "remember 项目安装必须使用 --frozen-lockfile --project"
    ))

    assert session.remembered == [
        ("项目安装必须使用 --frozen-lockfile", "project"),
    ]


def test_memory_remember_parameter_terminator_keeps_literal_scope_flag() -> None:
    mode, session, _messages = _mode()

    asyncio.run(mode._cmd_memory("remember 保留字面参数 -- --project"))

    assert session.remembered == [("保留字面参数 --project", "global")]


def test_spawn_retrieves_and_displays_background_task_exception() -> None:
    messages: list[str] = []
    renders: list[bool] = []

    async def scenario() -> None:
        mode = object.__new__(InteractiveMode)
        mode._background_tasks = set()  # type: ignore[attr-defined]
        mode.theme = SimpleNamespace(fg=lambda _style, text: text)  # type: ignore[attr-defined]
        mode.tui = SimpleNamespace(request_render=lambda: renders.append(True))  # type: ignore[attr-defined]
        mode._add_system_message = messages.append  # type: ignore[method-assign]

        async def fail() -> None:
            raise RuntimeError("memory status failed")

        task = mode._spawn(fail())
        assert task is not None
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert messages == ["错误：memory status failed"]
    assert renders == [True]
