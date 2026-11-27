"""Exercise edit recovery through a real AgentSession on each side of a process exit."""
from __future__ import annotations

import asyncio
import builtins
import multiprocessing
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from agent_core import BeforeToolCallResult, SessionManager
from agent_core.session.types import CompactionResult
from agent_core.tools._mutation import file_mutation_lock
from agent_llm import AssistantMessage, AssistantMessageEventStream, TextContent, ToolCall, ToolResultMessage
from agent_llm.providers.deepseek_models import DEEPSEEK_MODELS

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.plan_mode import PlanModeError


CALL_ID = "call-edit-1"
CRASH_EXIT = 73
ORIGINAL = b"a = 1\n"
EDITED = b"a = 10\n"
USER_EDIT = b"a = 42  # changed by the user\n"
PATCH = "--- example.py\n+++ example.py\n@@ -1 +1 @@\n-a = 1\n+a = 10"


def edit_request(filename: str = "example.py") -> AssistantMessage:
    return AssistantMessage(content=[ToolCall(
        id=CALL_ID, name="edit",
        arguments={"path": filename, "edits": [{"oldText": "a = 1", "newText": "a = 10"}]},
    )], stop_reason="tool_use")


def reply(message: AssistantMessage) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()
    stream.push({"type": "start", "partial": message})
    stream.push({"type": "done", "reason": message.stop_reason, "message": message})
    stream.end(message)
    return stream


def open_runtime(workspace: Path, manager: SessionManager, *, permission_mode="full_access", before_tool_call=None) -> AgentSession:
    return AgentSession(AgentSessionConfig(
        model=DEEPSEEK_MODELS["deepseek-flash"], cwd=str(workspace),
        session_manager=manager, permission_mode=permission_mode, before_tool_call=before_tool_call,
        allowed_tool_names=["edit"],
    ))


async def crash_during_edit(workspace: Path, when: str, filename: str) -> None:
    manager = SessionManager.create(cwd=str(workspace), sessions_dir=workspace.parent / "sessions", session_id="edit-test")
    session = open_runtime(workspace, manager)
    responses = iter([edit_request(filename), AssistantMessage(content=[TextContent(text="Done")])])
    session.agent.stream_fn = lambda *_: reply(next(responses))

    def on_event(event):
        if event["type"] == "tool_execution_end" and event["tool_call_id"] == CALL_ID:
            if event["status"] == "completed" and when == "after-tool":
                os._exit(CRASH_EXIT)

    session.on_event(on_event)
    target = workspace / filename
    original_open = builtins.open
    original_replace = os.replace

    @contextmanager
    def interrupt_after_close(stream):
        with stream:
            yield stream
        os._exit(CRASH_EXIT)

    def open_file(file, mode="r", *args, **kwargs):
        # An in-place edit has the same observable boundaries: before target
        # truncation and after close has flushed the new bytes to the OS.
        if "w" in mode and not isinstance(file, int) and Path(file).resolve() == target:
            if when == "before-replace":
                os._exit(CRASH_EXIT)
            if when == "after-replace":
                return interrupt_after_close(original_open(file, mode, *args, **kwargs))
        return original_open(file, mode, *args, **kwargs)

    def replace(src, dst, *args, **kwargs):
        is_target = Path(dst).resolve() == target
        if is_target and when == "before-replace":
            os._exit(CRASH_EXIT)
        original_replace(src, dst, *args, **kwargs)
        if is_target and when == "after-replace":
            os._exit(CRASH_EXIT)

    with patch("os.replace", replace), patch("builtins.open", open_file):
        await session.prompt("Change a = 1 to a = 10 in example.py.")
    raise AssertionError(f"The edit finished without reaching the {when} crash point")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "agent-home"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "example.py").write_bytes(ORIGINAL)
    return project


def run_until_crash(workspace: Path, when: str, filename: str = "example.py") -> Path:
    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(workspace), when, filename],
        capture_output=True, text=True, errors="replace", timeout=30,
    )
    assert child.returncode == CRASH_EXIT, child.stderr
    manager = SessionManager.continue_recent(cwd=str(workspace), sessions_dir=workspace.parent / "sessions")
    assert manager is not None and manager.path is not None
    messages = [entry.message for entry in manager.get_branch() if hasattr(entry, "message")]
    assert isinstance(messages[-1], AssistantMessage)
    assert messages[-1].content == edit_request(filename).content
    assert not any(isinstance(message, ToolResultMessage) for message in messages)
    return manager.path


async def result_on_continuation(workspace: Path, session_path: Path) -> ToolResultMessage:
    manager = SessionManager.open(session_path, sessions_dir=workspace.parent / "sessions")
    session = open_runtime(workspace, manager)
    requests = []

    def provider(_model, context, _options):
        requests.append(list(context.messages))
        return reply(AssistantMessage(content=[TextContent(text="Reviewed")]))

    session.agent.stream_fn = provider
    try:
        await session.prompt("What happened to the previous edit?")
    finally:
        await session.aclose()
    messages, = requests
    call_index, = [
        index for index, message in enumerate(messages)
        if isinstance(message, AssistantMessage) and message.content == edit_request().content
    ]
    result, = [
        message for message in messages
        if isinstance(message, ToolResultMessage) and message.tool_call_id == CALL_ID
    ]
    assert messages[call_index + 1] is result
    assert result.tool_name == "edit"
    return result


@pytest.mark.parametrize(("when", "user_changed"), [
    ("after-tool", False), ("after-tool", True), ("after-replace", False),
], ids=["completed", "completed-then-user-edited", "replaced-before-completion"])
def test_reopen_recovers_original_result_without_rewriting(workspace: Path, when: str, user_changed: bool) -> None:
    session_path = run_until_crash(workspace, when)
    target = workspace / "example.py"
    assert target.read_bytes() == EDITED
    if user_changed:
        target.write_bytes(USER_EDIT)
    expected = USER_EDIT if user_changed else EDITED
    # A timestamp in the past makes an identical rewrite visible even on coarse filesystems.
    os.utime(target, (1_600_000_000, 1_600_000_000))
    original_mtime = target.stat().st_mtime_ns
    for _ in range(2):
        result = asyncio.run(result_on_continuation(workspace, session_path))
        assert target.read_bytes() == expected
        assert target.stat().st_mtime_ns == original_mtime
        assert result.status == "completed" and not result.is_error
        assert result.details["patch"] == PATCH
        assert result.content == [TextContent(text="Successfully replaced 1 block(s) in example.py.")]


@pytest.mark.parametrize("user_changed", [False, True], ids=["awaiting-explicit-retry", "external-conflict"])
def test_reopen_prepared_edit_preserves_current_file(workspace: Path, user_changed: bool) -> None:
    session_path = run_until_crash(workspace, "before-replace")
    target = workspace / "example.py"
    assert target.read_bytes() == ORIGINAL
    if user_changed:
        target.write_bytes(USER_EDIT)
    original_mtime = target.stat().st_mtime_ns
    result = asyncio.run(result_on_continuation(workspace, session_path))
    assert target.read_bytes() == (USER_EDIT if user_changed else ORIGINAL)
    assert target.stat().st_mtime_ns == original_mtime
    assert result.status == ("failed" if user_changed else "uncertain")
    expected_code = "EDIT_CONFLICT" if user_changed else "EDIT_RECOVERY_PENDING"
    assert result.details and result.details.get("code") == expected_code, result


def test_retrying_the_original_edit_twice_writes_once(workspace: Path) -> None:
    session_path = run_until_crash(workspace, "before-replace")
    manager = SessionManager.open(session_path, sessions_dir=workspace.parent / "sessions")
    source_entry = manager.get_branch()[-1]
    target = workspace / "example.py"
    assert target.read_bytes() == ORIGINAL

    async def retry_from_original_call():
        session = open_runtime(workspace, manager)

        def unexpected_model_request(*_args):
            pytest.fail("retrying an existing edit must not ask the model for a new tool call")

        session.agent.stream_fn = unexpected_model_request
        try:
            await session.retry_edit(assistant_entry_id=source_entry.id, tool_call_id=CALL_ID)
            assert target.read_bytes() == EDITED
            os.utime(target, (1_600_000_000, 1_600_000_000))
            mtime = target.stat().st_mtime_ns
            await session.retry_edit(assistant_entry_id=source_entry.id, tool_call_id=CALL_ID)
            assert target.read_bytes() == EDITED
            assert target.stat().st_mtime_ns == mtime
        finally:
            await session.aclose()

    asyncio.run(retry_from_original_call())
    result = asyncio.run(result_on_continuation(workspace, session_path))
    assert result.status == "completed"
    assert result.details["patch"] == PATCH


def test_legacy_call_without_journal_stays_uncertain_even_if_bytes_match(workspace: Path) -> None:
    target = workspace / "example.py"
    target.write_bytes(EDITED)
    manager = SessionManager.create(cwd=str(workspace), sessions_dir=workspace.parent / "sessions", session_id="legacy-edit")
    manager.append_message(edit_request())
    manager.flush()
    assert manager.path is not None
    result = asyncio.run(result_on_continuation(workspace, manager.path))
    assert result.status == "uncertain"
    assert target.read_bytes() == EDITED


@pytest.mark.parametrize("when", ["after-tool", "before-replace"])
def test_fork_copies_only_confirmed_outcomes(workspace: Path, when: str) -> None:
    path = run_until_crash(workspace, when)
    manager = SessionManager.open(path, sessions_dir=workspace.parent / "sessions")
    fork = SessionManager.fork(manager, sessions_dir=workspace.parent / "forks")
    messages = fork.build_session_context().messages
    results = [message for message in messages if isinstance(message, ToolResultMessage)]
    if when == "after-tool":
        result, = results
        assert result.status == "completed" and result.details["patch"] == PATCH
    else:
        assert not results
    assert (workspace / "example.py").read_bytes() == (EDITED if when == "after-tool" else ORIGINAL)


def test_sensitive_edit_check_requests_permission_only_when_selected(workspace: Path) -> None:
    target = workspace / ".env"
    target.write_bytes(ORIGINAL)
    session_path = run_until_crash(workspace, "before-replace", target.name)

    async def check():
        manager = SessionManager.open(session_path)
        source_id = manager.get_branch()[-1].id
        approvals = []

        def gate(context, _signal):
            approvals.append((context.tool_call.name, context.args["path"]))

        session = open_runtime(workspace, manager, permission_mode="ask", before_tool_call=gate)
        events = []
        session.on_event(lambda event: events.append(event) if event["type"] == "edit_recovery_updated" else None)
        session.agent.stream_fn = lambda *_: reply(AssistantMessage(content=[TextContent(text="Reviewed")]))
        try:
            await session.prompt("What happened?")
            assert approvals == []
            await session.refresh_edit_recovery(source_id, CALL_ID)
            assert approvals == [("edit", target.name)]
            result, = [m for m in session.state.messages if isinstance(m, ToolResultMessage)]
            assert result.details["retryable"] is True
            assert events[-1]["results"] == [result]
            assert result.details["assistantEntryId"] == source_id
            count = len(events)
            await session.refresh_edit_recovery(source_id, CALL_ID)
            assert len(events) == count
            assert target.read_bytes() == ORIGINAL
        finally:
            await session.aclose()

    asyncio.run(check())


def test_resume_in_different_cwd_checks_and_edits_original_target(workspace: Path) -> None:
    session_path = run_until_crash(workspace, "before-replace")
    other = workspace.parent / "other-project"
    other.mkdir()
    other_target = other / "example.py"
    other_target.write_bytes(ORIGINAL)
    original = workspace / "example.py"

    async def scenario():
        manager = SessionManager.open(session_path)
        entry_id = manager.get_branch()[-1].id
        approvals = []

        def allow_original(context, _signal):
            authorized_path = Path(context.args["path"])
            approvals.append((context.tool_call.name, authorized_path))
            if authorized_path != original:
                return BeforeToolCallResult(block=True, reason="Unexpected target")

        session = open_runtime(other, manager, before_tool_call=allow_original, permission_mode="ask")
        session.agent.stream_fn = lambda *_: reply(AssistantMessage(content=[TextContent(text="Noted")]))
        try:
            await session.prompt("What happened?")
            assert approvals == []
            await session.refresh_edit_recovery(entry_id, CALL_ID)
            assert approvals == [("edit", original)]
            result = await session.retry_edit(entry_id, CALL_ID)
            assert approvals == [("edit", original)] * 2
            assert result.status == "completed"
            assert original.read_bytes() == EDITED
            assert other_target.read_bytes() == ORIGINAL
        finally:
            await session.aclose()

    asyncio.run(scenario())


def test_branch_selection_and_compacted_reload_keep_only_in_scope_edit_results(workspace: Path) -> None:
    manager = SessionManager.open(run_until_crash(workspace, "after-tool"))
    before_edit, source = manager.get_branch()
    target = workspace / "example.py"
    target.write_bytes(USER_EDIT)

    async def scenario():
        session = open_runtime(workspace, manager)
        try:
            manager.branch(before_edit.id)
            session.load_branch_messages(manager.build_session_context().messages)
            assert not any(isinstance(m, ToolResultMessage) for m in session.state.messages)
            manager.branch(source.id)
            manager.append_compaction(CompactionResult(summary="Earlier context", first_kept_entry_id=source.id,
                                                       tokens_before=100))
            # This is the reload used by CompactionOrchestrator after commit.
            session.agent.load_messages(manager.build_session_context().messages)
            summary, assistant, completed = session.state.messages
            assert summary.role == "compactionSummary"
            assert assistant is source.message and isinstance(completed, ToolResultMessage)
            assert completed.status == "completed" and completed.details["patch"] == PATCH
            assert target.read_bytes() == USER_EDIT
        finally:
            await session.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["deny", "permission", "branch", "archive", "plan"])
def test_original_retry_rechecks_the_live_session(workspace: Path, change: str) -> None:
    session_path = run_until_crash(workspace, "before-replace")
    manager = SessionManager.open(session_path)
    branch = manager.get_branch()
    source = branch[-1]

    async def scenario():
        waiting = asyncio.Event()
        answer = asyncio.Event()

        async def gate(context, _signal):
            assert context.tool_call.id == CALL_ID and context.tool_call.name == "edit"
            waiting.set()
            await answer.wait()
            return BeforeToolCallResult(block=True, reason="Denied") if change == "deny" else None

        session = open_runtime(workspace, manager, before_tool_call=gate)
        try:
            attempt = asyncio.create_task(session.retry_edit(source.id, CALL_ID))
            await asyncio.wait_for(waiting.wait(), 2)
            duplicate = await session.retry_edit(source.id, CALL_ID)
            assert duplicate.status == "blocked"
            if change in {"permission", "plan"}:
                # The normal controls guard the open approval. Stop the attempt
                # first, then apply the change once that approval has settled.
                with pytest.raises(PlanModeError, match=".+"):
                    if change == "plan":
                        session.enter_plan_mode()
                    else:
                        session.set_permission_mode("ask", persist=False)
                await session.abort()
            elif change == "branch":
                manager.branch(branch[0].id)
            elif change == "archive":
                manager.set_archived(True)
            answer.set()
            assert (await attempt).status == "blocked"
            if change == "plan":
                session.enter_plan_mode()
                assert (await session.retry_edit(source.id, CALL_ID)).status == "blocked"
            elif change == "permission":
                session.set_permission_mode("ask", persist=False)
                assert session.permission_mode == "ask"
            assert (workspace / "example.py").read_bytes() == ORIGINAL
        finally:
            answer.set()
            await session.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("retry", [False, True], ids=["initial-edit", "original-retry"])
def test_archiving_while_waiting_for_file_lock_stops_edit(workspace: Path, retry: bool) -> None:
    target = workspace / "example.py"
    if retry:
        manager = SessionManager.open(run_until_crash(workspace, "before-replace"))
    else:
        manager = SessionManager.create(cwd=str(workspace), sessions_dir=workspace.parent / "sessions")

    async def scenario():
        approved = asyncio.Event()

        def gate(context, _signal):
            if context.tool_call.name == "edit":
                approved.set()

        session = open_runtime(workspace, manager, before_tool_call=gate)
        responses = iter([edit_request(), AssistantMessage(content=[TextContent(text="Done")])])
        session.agent.stream_fn = lambda *_: reply(next(responses))
        try:
            async with file_mutation_lock(str(target)):
                if retry:
                    operation = session.retry_edit(manager.get_branch()[-1].id, CALL_ID)
                else:
                    operation = session.prompt("Edit example.py")
                task = asyncio.create_task(operation)
                await asyncio.wait_for(approved.wait(), 2)
                assert manager.path is not None
                SessionManager.open(manager.path).set_archived(True)
            result = await asyncio.wait_for(task, 5)
            if not retry:
                result, = [m for m in session.state.messages if isinstance(m, ToolResultMessage)]
            assert result.status == "blocked"
            assert target.read_bytes() == ORIGINAL
        finally:
            await session.aclose()

    asyncio.run(scenario())


def _concurrent_retry(workspace, session_path, barrier, reports):
    manager = SessionManager.open(session_path, sessions_dir=workspace.parent / "sessions")
    entry = manager.get_branch()[-1]
    target = workspace / "example.py"
    original_replace = os.replace
    writes = 0

    def count_replace(src, dst, *args, **kwargs):
        nonlocal writes
        original_replace(src, dst, *args, **kwargs)
        if Path(dst).resolve() == target:
            writes += 1

    async def run():
        session = open_runtime(workspace, manager)
        try:
            barrier.wait(timeout=15)
            with patch("os.replace", count_replace):
                result = await session.retry_edit(assistant_entry_id=entry.id, tool_call_id=CALL_ID)
            reports.put((result.status, writes))
        finally:
            await session.aclose()

    asyncio.run(run())


def test_two_processes_retry_original_edit_only_once(workspace: Path) -> None:
    session_path = run_until_crash(workspace, "before-replace")
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    reports = context.Queue()
    children = [context.Process(target=_concurrent_retry, args=(workspace, session_path, barrier, reports))
                for _ in range(2)]
    try:
        for child in children:
            child.start()
        for child in children:
            child.join(25)
            assert child.exitcode == 0
        assert sorted(reports.get(timeout=2) for _ in children) == [("completed", 0), ("completed", 1)]
        assert (workspace / "example.py").read_bytes() == EDITED
        assert asyncio.run(result_on_continuation(workspace, session_path)).details["patch"] == PATCH
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(5)
            child.close()
        reports.close()


if __name__ == "__main__":
    asyncio.run(crash_during_edit(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
