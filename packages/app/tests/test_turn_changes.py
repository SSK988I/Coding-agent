"""Immutable per-run reviews survive replay without claiming pre-existing edits."""
from __future__ import annotations

import asyncio
import json

import pytest
from agent_core import SessionManager
from agent_llm import AssistantMessage, Model, ModelCost, TextContent, UserMessage

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.desktop.protocol import RpcError
from coding_agent.desktop.runtime import DesktopRuntime
from coding_agent.desktop.workbench import WorkspaceWorkbench
from test_desktop_images import successful_stream


def make_runtime(tmp_path, manager=None):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    manager = manager or SessionManager.create(cwd=str(root), sessions_dir=tmp_path / "sessions")
    session = AgentSession(AgentSessionConfig(cwd=str(root), session_manager=manager, tools=[],
        model=Model(id="test", provider="test", cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0))))
    events = []
    runtime = DesktopRuntime(events.append)
    runtime._workspace = root
    runtime._session = session
    runtime._workbench = WorkspaceWorkbench(root, manager.header.id, tmp_path / "reviews")
    runtime._unsubscribe = session.on_event(runtime._on_session_event)
    return runtime, session, events, root


def test_two_run_reviews_are_net_diffs_immutable_and_branch_local_after_replay(tmp_path):
    async def scenario():
        runtime, session, events, root = make_runtime(tmp_path)
        file = root / "code.py"
        file.write_text("user dirty baseline\n", encoding="utf-8")
        (root / "unrelated.txt").write_text("pre-existing user edit", encoding="utf-8")
        scope = {"workspacePath": str(root), "sessionId": session.session_manager.header.id}
        run_ids = []
        for content in ("first run\n", "second run\n"):
            def stream(*_args, body=content):
                file.write_text(body, encoding="utf-8")
                return successful_stream()
            session.agent.stream_fn = stream
            result = await runtime.dispatch("run.start", {"text": "edit"})
            run_ids.append(result["runId"])
            await asyncio.wait_for(runtime._run_task, 5)
        snapshot = await runtime.dispatch("session.snapshot", {})
        summaries = snapshot["turnChanges"]
        assert [item["afterMessageIndex"] for item in summaries] == [1, 3]
        assert runtime._workspace_payload()["turnChanges"] == summaries
        assert [file["path"] for file in summaries[0]["files"]] == ["code.py"]
        first = await runtime.dispatch("workbench.changes", {**scope, "source": "turn", "runId": run_ids[0]})
        assert "-user dirty baseline" in first["files"][0]["diff"]
        assert "+first run" in first["files"][0]["diff"]
        assert "second run" not in first["files"][0]["diff"]
        kinds = [event["event"]["type"] for event in events]
        assert kinds.index("workbench.turnChanged") < kinds.index("run.completed")
        assert session.session_manager.path is not None
        restored = SessionManager.open(session.session_manager.path)
        await runtime.dispose()
        replay, restored_session, _events, _root = make_runtime(tmp_path, restored)
        try:
            assert (await replay.dispatch("session.snapshot", {}))["turnChanges"] == summaries
            restored.branch(summaries[0]["entryId"])
            restored_session.agent.state.messages = restored.build_session_context().messages
            assert len((await replay.dispatch("session.snapshot", {}))["turnChanges"]) == 1
            with pytest.raises(RpcError) as error:
                await replay.dispatch("workbench.changes", {**scope, "source": "turn", "runId": run_ids[1]})
            assert error.value.code == "TURN_NOT_IN_BRANCH"
            assert (await replay.dispatch("workbench.changes", {**scope, "source": "turn", "runId": run_ids[0]}))["files"] == first["files"]
        finally:
            await replay.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["run.completed", "run.failed", "run.cancelled"])
def test_changed_files_are_recorded_for_all_run_outcomes(tmp_path, outcome):
    async def scenario():
        runtime, session, events, root = make_runtime(tmp_path)
        entered = asyncio.Event()
        async def prompt(_text):
            message = UserMessage(content="edit", timestamp=1)
            session.session_manager.append_message(message)
            session.agent.state.messages.append(message)
            (root / "created.txt").write_text("changed\n", encoding="utf-8")
            entered.set()
            if outcome == "run.failed":
                raise RuntimeError("expected failure")
            if outcome == "run.cancelled":
                await asyncio.Event().wait()
        session.prompt = prompt
        try:
            accepted = await runtime.dispatch("run.start", {"text": "edit"})
            task = runtime._run_task
            assert task is not None
            await asyncio.wait_for(entered.wait(), 5)
            if outcome == "run.cancelled":
                task.cancel()
            await asyncio.wait_for(task, 5)
            assert events[-1]["event"]["type"] == outcome
            changes = [event for event in events if event["event"]["type"] == "workbench.turnChanged"]
            assert len(changes) == 1 and changes[0]["runId"] == accepted["runId"]
            assert changes[0]["event"]["payload"]["change"]["files"][0]["path"] == "created.txt"
            assert runtime._run_id is None and runtime._run_task is None
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_no_card_for_unchanged_or_net_zero_workspace(tmp_path):
    async def scenario():
        runtime, session, events, root = make_runtime(tmp_path)
        file = root / "same.txt"
        file.write_text("old", encoding="utf-8")
        def stream(*_args):
            file.write_text("temporary", encoding="utf-8")
            file.write_text("old", encoding="utf-8")
            return successful_stream()
        session.agent.stream_fn = stream
        try:
            await runtime.dispatch("run.start", {"text": "no net edit"})
            await asyncio.wait_for(runtime._run_task, 5)
            assert not any(event["event"]["type"] == "workbench.turnChanged" for event in events)
            assert (await runtime.dispatch("session.snapshot", {}))["turnChanges"] == []
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_repeated_cancel_during_review_still_publishes_terminal_and_releases_run(tmp_path):
    async def scenario():
        runtime, session, events, _root = make_runtime(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        async def finish(*_args):
            entered.set()
            await release.wait()
        runtime._finish_workbench_turn = finish
        runtime._run_id = "review-run"
        task = asyncio.create_task(runtime._finalize_run(session, "review-run", None, ("run.completed", {})))
        runtime._run_task = task
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        await asyncio.wait_for(task, 5)
        assert events[-1]["event"]["type"] == "run.completed"
        assert runtime._run_id is None and runtime._run_task is None
        await runtime.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("bad", ["../escape", "x/y", "", None, "index.json", "a" * 129])
def test_historical_review_rejects_path_like_run_ids(tmp_path, bad):
    reader = WorkspaceWorkbench(tmp_path, "session", tmp_path / "cache")
    with pytest.raises(RpcError) as error:
        reader.turn_review(bad)
    assert error.value.code == "INVALID_PARAMS"


def test_binary_additions_deletions_truncation_and_immutable_review(tmp_path, monkeypatch):
    import coding_agent.desktop.workbench as module
    async def scenario():
        root = tmp_path / "project"
        root.mkdir()
        (root / "deleted.txt").write_text("old\n", encoding="utf-8")
        reader = WorkspaceWorkbench(root, "session", tmp_path / "cache")
        before = await reader.capture()
        (root / "deleted.txt").unlink()
        (root / "added.txt").write_text("new\n" * 40, encoding="utf-8")
        (root / "binary.bin").write_bytes(b"\0\x01")
        monkeypatch.setattr(module, "MAX_DIFF_BYTES", 64)
        summary = await reader.finish_turn("run-1", before, "entry-1")
        assert summary and summary["truncated"] and summary["additions"] == 40 and summary["deletions"] == 1
        review = reader.turn_review("run-1")
        assert {file["status"] for file in review["files"]} == {"A", "D"}
        assert next(file for file in review["files"] if file["path"] == "binary.bin")["binary"]
        assert sum(len(file["diff"].encode()) for file in review["files"]) <= 64
        with pytest.raises(FileExistsError):
            await reader.finish_turn("run-1", before, "entry-1")
        assert reader.turn_review("run-1") == review
    asyncio.run(scenario())


def test_corrupt_review_metadata_never_prevents_opening_conversation(tmp_path):
    async def scenario():
        runtime, session, _events, _root = make_runtime(tmp_path)
        try:
            reader = runtime._workbench
            assert reader is not None
            directory = reader.storage / "turns"
            directory.mkdir(parents=True)
            (directory / "index.json").write_text(json.dumps({"workspace": str(reader.workspace), "sessionId": reader.session_id,
                "turns": {"run-1": {"runId": "run-1", "entryId": "entry", "files": "damaged"}}}), encoding="utf-8")
            snapshot = await runtime.dispatch("session.snapshot", {})
            assert snapshot["messages"] == [] and snapshot["turnChanges"] == []
            assert "turnChangesError" in snapshot
            assert "turnChangesError" in runtime._workspace_payload()
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_snapshot_anchor_mapping_distinguishes_identical_timestamps_after_replay(tmp_path):
    runtime, session, _events, _root = make_runtime(tmp_path)
    entries = []
    for _ in range(2):
        for message in (UserMessage(content="same", timestamp=1), AssistantMessage(content=[TextContent(text="same")], timestamp=1)):
            entries.append(session.session_manager.append_message(message))
    session.session_manager.flush()
    assert session.session_manager.path is not None
    restored = SessionManager.open(session.session_manager.path)
    restored_session = AgentSession(AgentSessionConfig(cwd=str(tmp_path / "project"), session_manager=restored,
        tools=[], model=session.model))
    mapping = runtime._turn_message_indices(restored_session)
    assert [mapping[entry.id] for entry in entries] == [0, 1, 2, 3]
