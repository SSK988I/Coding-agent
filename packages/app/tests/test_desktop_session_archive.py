"""Desktop archiving is durable organization and preserves the selected transcript."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent_core import SessionManager
from agent_llm import AssistantMessage, Model, ModelCost, TextContent, UserMessage

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.config import get_sessions_dir
from coding_agent.cli.args import Args
from coding_agent.cli.main import _create_session_manager
from coding_agent.desktop.protocol import RpcError
from coding_agent.desktop.runtime import DesktopRuntime


def _saved(workspace: Path) -> SessionManager:
    manager = SessionManager.create(cwd=str(workspace), sessions_dir=get_sessions_dir())
    manager.append_message(UserMessage(content="original question"))
    manager.append_message(AssistantMessage(content=[TextContent(text="original answer")]))
    return manager


def _runtime(workspace: Path, manager: SessionManager, events: list) -> DesktopRuntime:
    runtime = DesktopRuntime(events.append)
    runtime._workspace = workspace
    runtime._session = AgentSession(AgentSessionConfig(
        cwd=str(workspace), session_manager=manager, no_builtin_tools=True,
        model=Model(id="offline", provider="offline", api="openai-completions",
                    base_url="https://offline.invalid", cost=ModelCost(0, 0, 0, 0)),
    ))
    return runtime


def test_archive_current_retains_selection_and_messages_then_restores(tmp_path):
    async def scenario():
        manager = _saved(tmp_path)
        assert manager.path is not None
        body = manager.path.read_bytes().split(b"\n", 1)[1]
        events = []
        runtime = _runtime(tmp_path, manager, events)
        original_session = runtime._session
        try:
            result = await runtime.dispatch("session.archive", {"sessionId": manager.header.id})
            assert result == {"sessionId": manager.header.id, "archived": True}
            assert runtime._session is original_session
            assert manager.path.read_bytes().split(b"\n", 1)[1] == body
            assert await runtime.dispatch("session.list", {}) == []
            archived = await runtime.dispatch("session.list", {"archived": True})
            assert [(item["id"], item["archived"]) for item in archived] == [(manager.header.id, True)]
            snapshot = await runtime.dispatch("session.snapshot", {})
            assert snapshot["archived"] is True
            assert snapshot["sessionId"] == manager.header.id
            assert len(snapshot["messages"]) == 2
            assert runtime._workspace_payload()["archived"] is True
            assert events[-1]["event"] == {"type": "session.archived", "payload": result}

            result = await runtime.dispatch("session.unarchive", {"sessionId": manager.header.id})
            assert result == {"sessionId": manager.header.id, "archived": False}
            assert runtime._session is original_session
            assert manager.path.read_bytes().split(b"\n", 1)[1] == body
            assert await runtime.dispatch("session.list", {"archived": True}) == []
            assert (await runtime.dispatch("session.list", {}))[0]["id"] == manager.header.id
            assert events[-1]["event"] == {"type": "session.unarchived", "payload": result}
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_restart_skips_archives_but_explicit_open_keeps_them_readable(tmp_path):
    async def scenario():
        saved = _saved(tmp_path)
        saved.set_archived(True)
        runtime = DesktopRuntime(lambda _event: None)
        try:
            opened = await runtime.dispatch("workspace.open", {"path": str(tmp_path), "resume": True})
            assert opened["sessionId"] != saved.header.id
            assert opened["archived"] is False
            opened = await runtime.dispatch("session.open", {"sessionId": saved.header.id})
            assert opened["sessionId"] == saved.header.id
            assert opened["archived"] is True
            assert len(opened["messages"]) == 2
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("selector", ["session_id", "exact", "prefix"])
def test_cli_explicit_archive_reference_reuses_existing_session(tmp_path, selector):
    saved = _saved(tmp_path)
    saved.set_archived(True)
    assert saved.path is not None
    before = list(saved.path.parent.glob("*.jsonl"))
    if selector == "session_id":
        args = Args(session_id=saved.header.id)
    else:
        reference = saved.header.id if selector == "exact" else saved.header.id[:12]
        args = Args(session=reference)
    selected = _create_session_manager(args, str(tmp_path))
    assert selected.path == saved.path
    assert selected.header.id == saved.header.id
    assert selected.header.archived is True
    assert selected.entries == saved.entries
    selected.append_message(UserMessage(content="explicitly resumed original session"))
    selected.flush()
    assert list(saved.path.parent.glob("*.jsonl")) == before


def test_archive_another_session_does_not_change_the_selected_session(tmp_path):
    async def scenario():
        current, target = _saved(tmp_path), _saved(tmp_path)
        runtime = _runtime(tmp_path, current, [])
        selected = runtime._session
        try:
            result = await runtime.dispatch("session.archive", {"sessionId": target.header.id})
            assert result == {"sessionId": target.header.id, "archived": True}
            assert runtime._session is selected
            snapshot = await runtime.dispatch("session.snapshot", {})
            assert snapshot["sessionId"] == current.header.id
            assert snapshot["archived"] is False
            assert target.path is not None
            assert SessionManager.open(target.path).header.archived is True
            assert [item["id"] for item in await runtime.dispatch("session.list", {})] == [current.header.id]
            await runtime.dispatch("session.unarchive", {"sessionId": target.header.id})
            assert SessionManager.open(target.path).header.archived is False
            assert runtime._session is selected
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("method", [
    "run.start", "run.steer", "run.followUp", "plan.execute", "plan.handoff", "plan.answer",
    "plan.cancel", "mode.enterPlan", "session.clear", "session.compact", "model.select",
    "subagent.spawn", "workbench.chat.send",
])
def test_archived_conversation_rejects_mutations_until_restored(tmp_path, monkeypatch, method):
    async def scenario():
        manager = _saved(tmp_path)
        manager.set_archived(True)
        runtime = _runtime(tmp_path, manager, [])
        drive = AsyncMock()
        monkeypatch.setattr(runtime, "_drive_run", drive)
        try:
            with pytest.raises(RpcError) as error:
                await runtime.dispatch(method, {"text": "continue"})
            assert error.value.code == "SESSION_ARCHIVED"
            assert runtime._run_task is None
            await runtime.dispatch("session.unarchive", {"sessionId": manager.header.id})
            result = await runtime.dispatch("run.start", {"text": "continue"})
            assert result["accepted"] is True
            task = getattr(runtime, "_run_task")
            assert task is not None
            await task
            drive.assert_awaited_once()
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("activity,code", [
    ("run", "RUN_IN_PROGRESS"), ("compaction", "COMPACTION_IN_PROGRESS"),
    ("subagent", "RUN_IN_PROGRESS"), ("side_chat", "RUN_IN_PROGRESS"),
])
def test_active_work_cannot_be_archived(tmp_path, monkeypatch, activity, code):
    async def scenario():
        manager = _saved(tmp_path)
        runtime = _runtime(tmp_path, manager, [])
        assert runtime._session is not None
        async def hold() -> None:
            await asyncio.Event().wait()
        task = asyncio.create_task(hold())
        if activity == "run":
            runtime._run_task = task
        elif activity == "compaction":
            monkeypatch.setattr(type(runtime._session), "is_compacting", property(lambda _self: True))
        elif activity == "subagent":
            monkeypatch.setattr(type(runtime._session.subagents), "has_active", property(lambda _self: True))
        else:
            monkeypatch.setattr(runtime, "_side_conversations", SimpleNamespace(records={"side": SimpleNamespace(task=task)}))
        try:
            with pytest.raises(RpcError) as error:
                await runtime.dispatch("session.archive", {"sessionId": manager.header.id})
            assert error.value.code == code
            assert manager.header.archived is False
        finally:
            runtime._run_task = None
            runtime._side_conversations = None
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await runtime.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("session_id", [None, "", "../escape", "a/b", "id\n", 123, True])
def test_archive_validates_ids_without_touching_history(tmp_path, session_id):
    async def scenario():
        manager = _saved(tmp_path)
        runtime = _runtime(tmp_path, manager, [])
        try:
            with pytest.raises(RpcError) as error:
                await runtime.dispatch("session.archive", {"sessionId": session_id})
            assert error.value.code == "INVALID_PARAMS"
            assert manager.header.archived is False
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("archived", [None, "true", 1, [], {}])
def test_archive_list_rejects_non_boolean_filter(tmp_path, archived):
    async def scenario():
        runtime = _runtime(tmp_path, _saved(tmp_path), [])
        try:
            with pytest.raises(RpcError) as error:
                await runtime.dispatch("session.list", {"archived": archived})
            assert error.value.code == "INVALID_PARAMS"
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_archive_cannot_target_another_workspace(tmp_path):
    async def scenario():
        other_workspace = tmp_path / "other"
        other_workspace.mkdir()
        current = _saved(tmp_path)
        other = _saved(other_workspace)
        runtime = _runtime(tmp_path, current, [])
        try:
            with pytest.raises(RpcError) as error:
                await runtime.dispatch("session.archive", {"sessionId": other.header.id})
            assert error.value.code == "SESSION_NOT_FOUND"
            assert other.path is not None
            assert SessionManager.open(other.path).header.archived is False
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_archive_cannot_target_another_workspace_with_colliding_directory_encoding():
    async def scenario():
        current_cwd, foreign_cwd = Path("/archive/project/nested"), Path("/archive/project-nested")
        current, foreign = _saved(current_cwd), _saved(foreign_cwd)
        assert current.path is not None and foreign.path is not None
        assert current.path.parent == foreign.path.parent
        runtime = _runtime(current_cwd, current, [])
        try:
            with pytest.raises(RpcError) as error:
                await runtime.dispatch("session.archive", {"sessionId": foreign.header.id})
            assert error.value.code == "SESSION_NOT_FOUND"
            assert SessionManager.open(foreign.path).header.archived is False
            assert [item["id"] for item in await runtime.dispatch("session.list", {})] == [current.header.id]
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("archived", [True, False])
def test_failed_archive_leaves_snapshot_and_list_unchanged(tmp_path, monkeypatch, archived):
    async def scenario():
        manager = _saved(tmp_path)
        manager.set_archived(not archived)
        events = []
        runtime = _runtime(tmp_path, manager, events)
        assert manager.path is not None
        original = manager.path.read_bytes()
        def fail(*_args):
            raise OSError("disk unavailable")
        monkeypatch.setattr("agent_core.session.session_manager.write_archive_state", fail)
        try:
            with pytest.raises(RpcError) as error:
                await runtime.dispatch("session.archive" if archived else "session.unarchive", {"sessionId": manager.header.id})
            assert error.value.code == "SESSION_ARCHIVE_FAILED"
            assert manager.header.archived is not archived
            assert manager.path.read_bytes() == original
            assert (await runtime.dispatch("session.snapshot", {}))["archived"] is not archived
            assert (await runtime.dispatch("session.list", {"archived": not archived}))[0]["id"] == manager.header.id
            assert events == []
        finally:
            await runtime.dispose()
    asyncio.run(scenario())
