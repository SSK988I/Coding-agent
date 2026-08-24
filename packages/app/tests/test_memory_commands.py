from __future__ import annotations

import asyncio

from coding_agent.modes.interactive.interactive_mode import InteractiveMode


class _Session:
    settings_manager = None

    def __init__(self) -> None:
        self.forgotten: list[tuple[str, str | None]] = []
        self.cleared: list[bool] = []

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
