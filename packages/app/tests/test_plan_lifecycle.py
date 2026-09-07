"""Authorization, crash recovery, and clean-session handoff regressions."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest
from agent_core import AgentContext, BeforeToolCallContext, SessionManager
from agent_core.agent_loop import _execute_tool_calls
from agent_core.types import AgentLoopConfig
from agent_core.session.storage import entry_to_line_dict, line_dict_to_entry
from agent_core.session.types import PlanRevisionEntry, PlanRunEntry, SessionMessageEntry
from agent_llm import AssistantMessage, Model, ModelCost, TextContent, ToolCall, UserMessage

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.plan_mode import (
    PlanModeError, PlanQuestion, PlanQuestionOption, create_plan_revision,
    reduce_plan_state,
)


class Stream:
    def __init__(self, message):
        self.message = message

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def result(self):
        return self.message


def session_at(tmp_path: Path, *, disk=False):
    manager = SessionManager.create(
        cwd=str(tmp_path), sessions_dir=tmp_path / "sessions", in_memory=not disk,
    )
    return AgentSession(AgentSessionConfig(
        model=Model(id="plan-test", provider="test", context_window=1_000_000,
                    cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0)),
        cwd=str(tmp_path), session_manager=manager, collaboration_mode="plan",
        question_behavior="deferred",
    ))


def submission(call_id="submit-1", title="Title", markdown="body\r\n\n"):
    return AssistantMessage(content=[ToolCall(
        id=call_id, name="submit_plan", arguments={"title": title, "markdown": markdown},
    )], stop_reason="tool_use")


async def drive(session, message, response, *, seen=None):
    def stream(_model, context, _options=None):
        if seen is not None:
            seen.append(context)
        return Stream(response)
    session.agent.stream_fn = stream
    await session.prompt(message)


def ready(session):
    asyncio.run(drive(session, "plan this", submission()))
    assert session.plan_state.phase == "ready"
    return session.plan_state.latest_revision


def test_end_to_end_question_revision_stale_handoff_and_settled(tmp_path):
    session = session_at(tmp_path, disk=True)
    events = []
    session.on_event(events.append)
    question = PlanQuestion("q", "Scope", "Which scope?", (
        PlanQuestionOption("Core", "Change core"), PlanQuestionOption("All", "Change all"),
    ))
    asyncio.run(session._request_plan_question(question, None))
    session.agent.stream_fn = lambda *_args: Stream(submission())
    asyncio.run(session.answer_plan_question("q", "Core"))
    first = session.plan_state.latest_revision
    assert first is not None
    contexts = []
    asyncio.run(drive(session, "add crash recovery", submission("submit-2", markdown="revised"), seen=contexts))
    assert "submit_plan" in {tool.name for tool in contexts[0].tools}
    latest = session.plan_state.latest_revision
    assert latest.revision == first.revision + 1
    with pytest.raises(PlanModeError) as error:
        asyncio.run(session.execute_plan(first.plan_id, first.revision, first.digest))
    assert error.value.code == "STALE_PLAN_REVISION"
    source = session.session_manager
    session.agent.follow_up("private queued conversation")
    target = session.handoff_plan_to_new_session(latest.plan_id, latest.revision, latest.digest)
    assert target.header.parent_session == source.header.id
    assert session.state.messages == []
    assert not session.agent.has_queued_messages()
    assert len(target.entries) == 2
    assert not any(isinstance(item, SessionMessageEntry) for item in target.entries)
    assert session.plan_state.phase == "ready"
    assert session.plan_state.latest_revision.digest == latest.digest
    restored = SessionManager.open(target.path)
    assert reduce_plan_state(
        restored.get_branch(), parent_session_id=restored.header.parent_session,
    ).phase == "ready"
    assert reduce_plan_state(source.get_branch()).handoff_target_session_id == target.header.id
    session.agent.stream_fn = lambda *_args: Stream(AssistantMessage(
        content=[TextContent(text="Agent turn ended")], stop_reason="stop",
    ))
    asyncio.run(session.execute_plan(latest.plan_id, latest.revision, latest.digest))
    assert session.plan_state.phase == "settled"
    runs = [item for item in target.entries if isinstance(item, PlanRunEntry)]
    assert [item.status for item in runs] == ["started", "completed"]
    assert runs[-1].assistant_message_id
    assert runs[-1].stop_reason == "stop"
    assert events[-2]["type"] == "plan.stateChanged"
    assert events[-2]["state"]["phase"] == "settled"


@pytest.mark.parametrize("stop", ["aborted", "error", "length"])
def test_incomplete_assistant_cannot_submit(tmp_path, stop):
    session = session_at(tmp_path)
    message = submission()
    message.stop_reason = stop
    # Direct control callback and model-loop entry point both enforce this.
    session.session_manager.append_message(message)
    with pytest.raises(PlanModeError):
        asyncio.run(session._submit_plan("submit-1", "Title", "body\r\n\n"))
    assert not any(isinstance(item, PlanRevisionEntry) for item in session.session_manager.entries)


@pytest.mark.parametrize("sibling", ["read", "write", "submit_plan"])
def test_submit_with_any_parallel_call_saves_no_revision(tmp_path, sibling):
    session = session_at(tmp_path)
    message = submission()
    message.content.append(ToolCall(id="sibling", name=sibling, arguments={}))
    session.session_manager.append_message(message)
    call = message.content[0]
    context = BeforeToolCallContext(
        assistant_message=message, tool_call=call,
        args=call.arguments, context=AgentContext(tools=session.tools),
    )
    result = asyncio.run(session._before_tool_call(context, asyncio.Event()))
    assert result.block and result.code == "PLAN_SUBMIT_NOT_EXCLUSIVE"
    with pytest.raises(PlanModeError):
        asyncio.run(session._submit_plan(call.id, "Title", "body\r\n\n"))
    assert session.plan_state.phase == "drafting"
    assert not any(isinstance(item, PlanRevisionEntry) for item in session.session_manager.entries)


def test_submit_stops_even_with_queued_follow_up(tmp_path):
    session = session_at(tmp_path)
    session.agent.follow_up("continue and execute")
    contexts = []
    asyncio.run(drive(session, "make plan", submission(), seen=contexts))
    assert len(contexts) == 1
    assert session.plan_state.phase == "ready"


def test_unregistered_tool_denial_has_code_and_alternatives(tmp_path):
    session = session_at(tmp_path)
    message = AssistantMessage(content=[ToolCall(id="bad", name="bash", arguments={"command": "pytest"})])
    async def convert(messages):
        return messages
    results, _ = asyncio.run(_execute_tool_calls(
        message.content, AgentContext(tools=session.tools), message,
        AgentLoopConfig(model=session.model, convert_to_llm=convert, before_tool_call=session._before_tool_call),
        lambda _event: None, asyncio.Event(),
    ))
    assert results[0].is_error
    assert results[0].details["code"] == "PLAN_POLICY_BLOCKED"
    assert {"tool": "git_diff"} in results[0].details["alternatives"]


def test_started_without_live_owner_recovers_uncertain_and_requires_action(tmp_path):
    session = session_at(tmp_path, disk=True)
    plan = ready(session)
    session.session_manager.append_plan_run(
        plan_id=plan.plan_id, revision=plan.revision, digest=plan.digest,
        status="started", run_id="crashed",
    )
    session.session_manager.flush()
    restored = AgentSession(AgentSessionConfig(
        model=session.model, cwd=str(tmp_path),
        session_manager=SessionManager.open(session.session_manager.path),
    ))
    assert restored.plan_state.phase == "uncertain"
    assert restored.tools == []
    with pytest.raises(PlanModeError) as error:
        asyncio.run(restored.prompt("go ahead"))
    assert error.value.code == "PLAN_RECOVERY_REQUIRED"
    assert asyncio.run(restored.run_bash("pytest"))["code"] == "PLAN_POLICY_BLOCKED"
    restored.enter_plan_mode()
    assert restored.plan_state.phase == "drafting"
    assert restored.plan_state.active_plan_id != plan.plan_id


@pytest.mark.parametrize("mutation", [
    "digest", "cross_plan", "duplicate_revision", "missing_source",
    "source_call", "old_revision", "missing_run_id", "terminal_tuple",
    "duplicate_mode", "reused_mode", "wrong_cancel", "wrong_answer",
    "unanswered", "terminal_after_cancel", "forged_origin",
])
def test_reducer_rejects_invalid_sequences(tmp_path, mutation):
    session = session_at(tmp_path)
    plan = ready(session)
    entries = session.session_manager.get_branch()
    revision = next(item for item in entries if isinstance(item, PlanRevisionEntry))
    index = entries.index(revision)
    manager = session.session_manager
    if mutation == "digest":
        entries[index] = replace(revision, digest="bad")
    elif mutation == "cross_plan":
        entries[index] = replace(revision, plan_id="another")
    elif mutation == "missing_source":
        entries[index] = replace(revision, source_message_id="missing")
    elif mutation == "source_call":
        entries[index] = replace(revision, submitted_by_tool_call_id="missing")
    elif mutation == "old_revision":
        entries[index] = replace(revision, revision=0)
    elif mutation == "forged_origin":
        entries[index] = replace(revision, origin_session_id="forged")
    elif mutation == "duplicate_revision":
        entries.append(replace(revision, id="duplicate"))
    elif mutation in {"missing_run_id", "terminal_tuple", "terminal_after_cancel"}:
        started = manager.append_plan_run(
            plan_id=plan.plan_id, revision=plan.revision, digest=plan.digest,
            status="started", run_id="run",
        )
        if mutation == "missing_run_id":
            started.run_id = None
        else:
            if mutation == "terminal_after_cancel":
                manager.append_collaboration_mode_change("default", plan_id=plan.plan_id, reason="user")
            manager.append_plan_run(
                plan_id=plan.plan_id, revision=plan.revision,
                digest="wrong" if mutation == "terminal_tuple" else plan.digest,
                status="completed", run_id="run",
            )
        entries = manager.get_branch()
    elif mutation in {"duplicate_mode", "reused_mode", "wrong_cancel"}:
        if mutation == "reused_mode":
            manager.append_collaboration_mode_change("default", plan_id=plan.plan_id)
        if mutation == "wrong_cancel":
            manager.append_collaboration_mode_change("default", plan_id="wrong")
        else:
            manager.append_collaboration_mode_change("plan", plan_id=plan.plan_id)
        entries = manager.get_branch()
    else:
        manager.append_message(UserMessage(content="revise"))
        manager.append_plan_question(
            plan_id=plan.plan_id, question_id="q", header="Scope", question="Which?",
            options=[{"label": "A", "description": "A"}, {"label": "B", "description": "B"}],
        )
        if mutation == "wrong_answer":
            manager.append_plan_question_answer(plan_id=plan.plan_id, question_id="wrong", answer="A")
        else:
            manager.append_plan_revision(
                plan_id=plan.plan_id, revision=2, title="Title", markdown="body",
                digest="bad", source_message_id="missing",
            )
        entries = manager.get_branch()
    state = reduce_plan_state(entries)
    assert state.phase == "recovery_error"
    assert state.recovery_error is not None
    assert state == reduce_plan_state(entries)


def test_legacy_digest_validated_without_rewrite(tmp_path):
    session = session_at(tmp_path)
    manager = session.session_manager
    markdown = "# Legacy\n"
    manager.append_plan_revision(
        plan_id=session.plan_state.active_plan_id, revision=1, title="Legacy", markdown=markdown,
        digest=hashlib.sha256(markdown.encode()).hexdigest(), source_message_id="old-source",
    )
    session.refresh_plan_state_from_branch()
    assert session.plan_state.phase == "ready"
    assert session.plan_state.latest_revision.schema_version == 0
    entry = manager.entries[-1]
    assert line_dict_to_entry(entry_to_line_dict(entry)) == entry
    plan = session.plan_state.latest_revision
    child = session.handoff_plan_to_new_session(plan.plan_id, plan.revision, plan.digest)
    assert session.plan_state.phase == "ready"
    assert child.entries[-1].schema_version == 0


@pytest.mark.parametrize("bad_line", ['{"type":', "[]", '"text"', '{"type":"plan_run","status":"bad"}'])
def test_corrupt_jsonl_requires_recovery_and_explicit_exit(tmp_path, bad_line):
    session = session_at(tmp_path, disk=True)
    ready(session)
    path = session.session_manager.path
    with path.open("a", encoding="utf-8") as stream:
        stream.write(bad_line + "\n")
    with pytest.warns(RuntimeWarning):
        manager = SessionManager.open(path)
    restored = AgentSession(AgentSessionConfig(model=session.model, session_manager=manager))
    assert restored.plan_state.phase == "recovery_error"
    with pytest.raises(PlanModeError):
        asyncio.run(restored.prompt("execute"))
    restored.cancel_plan_mode()
    assert restored.plan_state.phase == "cancelled"
    with pytest.warns(RuntimeWarning):
        reopened = SessionManager.open(path)
    assert reduce_plan_state(reopened.get_branch(), load_issues=reopened.load_issues).phase == "cancelled"


@pytest.mark.parametrize("stage", ["target_flush", "source_append"])
def test_handoff_failure_retains_source_ready_and_keeps_child(tmp_path, monkeypatch, stage):
    session = session_at(tmp_path, disk=True)
    plan = ready(session)
    source = session.session_manager
    original_flush = SessionManager.flush
    original_append = SessionManager.append_collaboration_mode_change
    def flush(manager):
        if manager is not source and stage == "target_flush":
            original_flush(manager)
            raise OSError("target flush interrupted")
        return original_flush(manager)
    def append(manager, mode, **kwargs):
        if manager is source and stage == "source_append":
            raise OSError("source append interrupted")
        return original_append(manager, mode, **kwargs)
    monkeypatch.setattr(SessionManager, "flush", flush)
    monkeypatch.setattr(SessionManager, "append_collaboration_mode_change", append)
    with pytest.raises(OSError):
        session.handoff_plan_to_new_session(plan.plan_id, plan.revision, plan.digest)
    assert session.session_manager is source
    assert session.plan_state.phase == "ready"
    assert reduce_plan_state(SessionManager.open(source.path).get_branch()).phase == "ready"
    children = [path for path in source.path.parent.glob("*.jsonl") if path != source.path]
    assert len(children) == 1
    child = SessionManager.open(children[0])
    assert child.header.parent_session == source.header.id
    assert reduce_plan_state(child.get_branch(), parent_session_id=source.header.id).phase == "ready"
    assert not any(isinstance(item, PlanRunEntry) for item in child.entries)


def test_entry_append_failure_does_not_advance_memory_or_file(tmp_path, monkeypatch):
    import agent_core.session.storage as storage
    session = session_at(tmp_path, disk=True)
    ready(session)
    manager = session.session_manager
    original_bytes = manager.path.read_bytes()
    previous_leaf = manager.leaf_id
    def fail_fsync(_fd):
        raise OSError("fsync failed")
    monkeypatch.setattr(storage.os, "fsync", fail_fsync)
    with pytest.raises(OSError):
        manager.append_collaboration_mode_change("default", plan_id=session.plan_state.active_plan_id)
    assert manager.leaf_id == previous_leaf
    assert manager.path.read_bytes() == original_bytes


def test_execution_registration_guards_session_changes_and_early_abort(tmp_path):
    session = session_at(tmp_path)
    plan = ready(session)
    calls = []
    async def before_prompt_memory(*_args):
        for action in (session.enter_plan_mode, session.new_session, session.cancel_plan_mode):
            with pytest.raises(PlanModeError):
                action()
        with pytest.raises(PlanModeError):
            await session.prompt("unconfirmed")
        await session.abort()
    async def forbidden_prompt(*_args, **_kwargs):
        calls.append("prompt")
    session._extract_accepted_plan_memory = before_prompt_memory
    session.agent.prompt = forbidden_prompt
    asyncio.run(session.execute_plan(plan.plan_id, plan.revision, plan.digest))
    assert not calls
    assert session.plan_state.phase == "aborted"


@pytest.mark.parametrize("title,markdown", [("x" * 201, "body"), ("two\nlines", "body"), ("Title", "x" * 65537)], ids=["long_title", "multiline_title", "long_markdown"])
def test_submit_limits(title, markdown):
    with pytest.raises(PlanModeError):
        create_plan_revision(plan_id="p", revision=1, title=title, markdown=markdown,
                             source_message_id="m", submitted_by_tool_call_id="t")


def test_title_and_markdown_are_preserved_exactly():
    revision = create_plan_revision(
        plan_id="p", revision=1, title="  Title  ", markdown=" no added heading\r\n\n",
        source_message_id="m", submitted_by_tool_call_id="t",
    )
    assert revision.title == "  Title  "
    assert revision.markdown == " no added heading\n\n"
    expected = {"schemaVersion": 1, "planId": "p", "revision": 1,
                "title": revision.title, "markdown": revision.markdown}
    assert revision.digest == hashlib.sha256(json.dumps(
        expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    lone_cr = create_plan_revision(
        plan_id="p", revision=2, title="Title", markdown="one\rtwo\r\nthree",
        source_message_id="m", submitted_by_tool_call_id="t2",
    )
    assert lone_cr.markdown == "one\rtwo\nthree"


def test_deferred_question_is_durable_before_any_assistant_message(tmp_path):
    session = session_at(tmp_path, disk=True)
    asyncio.run(session._request_plan_question(PlanQuestion("q", "Scope", "Which?", (
        PlanQuestionOption("A", "A"), PlanQuestionOption("B", "B"),
    )), None))
    reopened = SessionManager.open(session.session_manager.path)
    assert reduce_plan_state(reopened.get_branch()).phase == "awaiting_answer"


@pytest.mark.parametrize("stage", ["started_flush", "terminal_append"])
def test_execution_persistence_failure_releases_ownership_and_locks_retry(tmp_path, monkeypatch, stage):
    session = session_at(tmp_path, disk=True)
    plan = ready(session)
    manager = session.session_manager
    calls = []
    session.agent.stream_fn = lambda *_args: calls.append("model") or Stream(AssistantMessage(
        content=[TextContent(text="ended")], stop_reason="stop",
    ))
    if stage == "started_flush":
        def fail_flush():
            raise OSError("started flush failed")
        monkeypatch.setattr(manager, "flush", fail_flush)
    else:
        original_append = manager.append_plan_run
        def append(**kwargs):
            if kwargs["status"] != "started":
                raise OSError("terminal append failed")
            return original_append(**kwargs)
        monkeypatch.setattr(manager, "append_plan_run", append)
    with pytest.raises(OSError):
        asyncio.run(session.execute_plan(plan.plan_id, plan.revision, plan.digest))
    assert session._active_plan_run_id is None
    assert session.plan_state.phase == "uncertain"
    assert not session.tools
    if stage == "started_flush":
        assert not calls
    assert reduce_plan_state(SessionManager.open(manager.path).get_branch()).phase == "uncertain"
    with pytest.raises(PlanModeError):
        asyncio.run(session.execute_plan(plan.plan_id, plan.revision, plan.digest))


def test_partial_initial_flush_can_resume_without_losing_buffered_entries(tmp_path, monkeypatch):
    import agent_core.session.session_manager as persistence
    manager = SessionManager.create(cwd=str(tmp_path), sessions_dir=tmp_path / "sessions")
    manager.append_collaboration_mode_change("plan", plan_id="p")
    manager.append_message(UserMessage(content="retain me"))
    original_append = persistence.append_entry_line
    def interrupted_append(path, entry):
        if isinstance(entry, SessionMessageEntry):
            raise OSError("interrupted buffer")
        original_append(path, entry)
    monkeypatch.setattr(persistence, "append_entry_line", interrupted_append)
    with pytest.raises(OSError):
        manager.flush()
    monkeypatch.setattr(persistence, "append_entry_line", original_append)
    manager.flush()
    assert SessionManager.open(manager.path).entries == manager.entries


@pytest.mark.parametrize("field,value", [
    ("revision", True), ("revision", "1"), ("schemaVersion", "1"),
    ("title", None), ("originSessionId", []), ("planId", 1),
])
def test_jsonl_plan_fields_are_not_coerced_before_digest_validation(tmp_path, field, value):
    session = session_at(tmp_path)
    ready(session)
    revision = next(item for item in session.session_manager.entries if isinstance(item, PlanRevisionEntry))
    raw = entry_to_line_dict(revision)
    raw[field] = value
    with pytest.raises(ValueError):
        line_dict_to_entry(raw)


def test_live_question_can_be_cancelled_through_desktop_rpc_without_default_continuation(tmp_path):
    from coding_agent.desktop.runtime import DesktopRuntime
    session = session_at(tmp_path)
    session._question_behavior = "blocking"
    contexts = []
    async def exercise():
        runtime = DesktopRuntime(lambda _event: None)
        runtime._session = session
        runtime._workspace = tmp_path
        async def cancel_when_ready():
            # Core publishes the question synchronously before yielding to its
            # pending-answer future. Schedule RPC cancellation on the next tick.
            await asyncio.sleep(0)
            payload = await runtime.dispatch("plan.cancel", {"planId": session.plan_state.active_plan_id})
            assert payload["planState"]["phase"] == "cancelled"
        tasks = []
        def on_event(event):
            if event["type"] == "plan.stateChanged" and event["state"]["phase"] == "awaiting_answer":
                tasks.append(asyncio.create_task(cancel_when_ready()))
        session.on_event(on_event)
        runtime._run_task = asyncio.create_task(drive(session, "ask first", AssistantMessage(content=[ToolCall(
            id="q", name="request_user_input", arguments={"questions": [{
                "header": "Scope", "question": "Which?", "options": [
                    {"label": "A", "description": "A"}, {"label": "B", "description": "B"},
                ],
            }]},
        )], stop_reason="tool_use"), seen=contexts))
        await asyncio.wait_for(runtime._run_task, timeout=3)
        await asyncio.gather(*tasks)
    asyncio.run(exercise())
    assert len(contexts) == 1
    assert session.plan_state.phase == "cancelled"
    assert not any(isinstance(item, PlanRevisionEntry) for item in session.session_manager.entries)
