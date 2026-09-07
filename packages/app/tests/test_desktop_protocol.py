from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_core import SessionManager
from agent_core import AgentContext, BeforeToolCallContext
from agent_llm import AssistantMessage, Model, ModelCost, TextContent, ToolCall, UserMessage

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.messages import convert_to_llm
from coding_agent.desktop.protocol import RpcError, parse_request, to_jsonable
from coding_agent.desktop.runtime import DesktopRuntime, _is_read_only_bash_command
from coding_agent.memory.types import MemoryIdentity, MemoryOverview


@dataclass
class _Payload:
    path: Path
    values: set[str]


def test_to_jsonable_handles_runtime_values(tmp_path: Path) -> None:
    value = to_jsonable(_Payload(path=tmp_path, values={"read", "write"}))
    assert value["path"] == str(tmp_path)
    assert sorted(value["values"]) == ["read", "write"]


def test_parse_request_accepts_versioned_rpc() -> None:
    request = parse_request('{"v":1,"id":"1","method":"runtime.ping"}')
    assert request["params"] == {}


@pytest.mark.parametrize(
    "line,code",
    [
        ("not-json", "INVALID_JSON"),
        ('{"v":2,"id":"1","method":"runtime.ping"}', "PROTOCOL_MISMATCH"),
        ('{"v":1,"method":"runtime.ping"}', "INVALID_REQUEST"),
    ],
)
def test_parse_request_rejects_invalid_input(line: str, code: str) -> None:
    with pytest.raises(RpcError) as error:
        parse_request(line)
    assert error.value.code == code


def test_agent_session_threads_desktop_tool_hooks() -> None:
    def before(*_args):
        return None

    def after(*_args):
        return None

    model = Model(
        id="desktop-test",
        provider="test",
        context_window=64_000,
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )
    session = AgentSession(AgentSessionConfig(
        model=model,
        tools=[],
        before_tool_call=before,
        after_tool_call=after,
    ))
    # AgentSession owns the outer policy gate; the desktop hook is chained
    # behind it so Plan-blocked tools never reach approval.
    assert session.agent.before_tool_call is not before
    assert session._config.before_tool_call is before
    assert session.agent.after_tool_call is after


def test_llm_conversion_repairs_legacy_incomplete_tool_history() -> None:
    converted = convert_to_llm([
        AssistantMessage(content=[ToolCall(id="call-1", name="bash", arguments={})]),
        UserMessage(content="/model"),
        AssistantMessage(
            content=[TextContent(text="")],
            stop_reason="error",
            error_message="legacy provider error",
        ),
        UserMessage(content="next request"),
    ])

    assert [message.role for message in converted] == ["assistant", "toolResult", "user"]
    assert converted[1].tool_call_id == "call-1"
    assert converted[2].content == "next request"


def test_desktop_command_catalog_only_exposes_supported_commands() -> None:
    import asyncio

    commands = asyncio.run(DesktopRuntime(lambda _event: None)._command_list({}))

    assert [command["name"] for command in commands] == [
        "help", "clear", "model", "compact", "session", "new",
        "plan", "cancel-plan", "execute-plan", "memory",
    ]


def test_memory_rpc_requires_confirmation_and_publishes_updated_status() -> None:
    import asyncio

    class MemorySession:
        memory_enabled = True
        memory_identity = MemoryIdentity("local-user", "project-a")
        settings_manager = None
        session_manager = SimpleNamespace(header=SimpleNamespace(id="memory-session"))

        async def memory_overview(self) -> MemoryOverview:
            return MemoryOverview(
                enabled=self.memory_enabled,
                user_id="local-user",
                project_id="project-a",
                global_count=1,
                project_count=2,
                conflict_count=0,
                root="memory-root",
            )

        async def memory_forget(self, key: str, scope: str | None) -> bool:
            assert key == "response.language"
            assert scope == "global"
            return True

    events: list[dict] = []
    runtime = DesktopRuntime(events.append)
    runtime._session = MemorySession()  # type: ignore[assignment]

    with pytest.raises(RpcError, match="显式确认") as error:
        asyncio.run(runtime.dispatch("memory.forget", {
            "key": "response.language", "scope": "global",
        }))
    assert error.value.code == "CONFIRMATION_REQUIRED"

    result = asyncio.run(runtime.dispatch("memory.forget", {
        "key": "response.language", "scope": "global", "confirmed": True,
    }))
    assert result["removed"] is True
    assert result["memory"]["projectCount"] == 2
    assert events[-1]["event"] == {
        "type": "memory.changed",
        "payload": result["memory"],
    }


def test_memory_v2_status_auto_extract_remember_and_queue_event() -> None:
    import asyncio

    class MemorySession:
        memory_enabled = True
        memory_identity = MemoryIdentity("local-user", "project-a")
        memory_service = SimpleNamespace(auto_extract=True)
        settings_manager = None
        session_manager = SimpleNamespace(header=SimpleNamespace(id="memory-session"))
        model = SimpleNamespace(id="model-a", name="Model A", provider="test")
        thinking_level = None
        tools: list = []
        state = SimpleNamespace(messages=[])
        collaboration_mode = "default"
        plan_state = SimpleNamespace(to_payload=lambda: {
            "phase": "idle", "activePlanId": None,
            "latestRevision": None, "pendingQuestion": None,
        })

        def __init__(self) -> None:
            self.auto_extract = True
            self.remembered: list[tuple[str, str]] = []

        async def memory_overview(self) -> MemoryOverview:
            return MemoryOverview(
                enabled=True,
                auto_extract_enabled=self.auto_extract,
                user_id="local-user",
                project_id="project-a",
                global_count=1,
                project_count=2,
                conflict_count=0,
                pending_count=3,
                processing_count=1,
                ready_count=4,
                failed_count=2,
                last_error="extract failed",
                root="memory-root",
            )

        def set_memory_auto_extract(self, enabled: bool) -> None:
            self.auto_extract = enabled

        async def memory_remember(self, content: str, *, scope: str) -> SimpleNamespace:
            self.remembered.append((content, scope))
            return SimpleNamespace(id="mem_manual_01", content=content)

    events: list[dict] = []
    runtime = DesktopRuntime(events.append)
    session = MemorySession()
    runtime._session = session  # type: ignore[assignment]
    runtime._workspace = Path.cwd()

    status = asyncio.run(runtime.dispatch("memory.status", {}))
    assert status["autoExtractEnabled"] is True
    assert status["pendingCount"] == 3
    assert status["processingCount"] == 1
    assert status["readyCount"] == 4
    assert status["failedCount"] == 2
    assert status["lastError"] == "extract failed"

    workspace = asyncio.run(runtime._workspace_payload_with_memory())
    assert workspace["memory"]["pendingCount"] == 3
    assert workspace["memory"]["readyCount"] == 4

    updated = asyncio.run(runtime.dispatch("memory.setAutoExtract", {"enabled": False}))
    assert session.auto_extract is False
    assert updated["autoExtractEnabled"] is False

    remembered = asyncio.run(runtime.dispatch("memory.remember", {
        "content": "偏好中文回答", "scope": "project",
    }))
    assert session.remembered == [("偏好中文回答", "project")]
    assert remembered["record"]["id"] == "mem_manual_01"

    runtime._on_session_event({
        "type": "memory_queue_changed",
        "pending_count": 2,
        "processing_count": 1,
        "failed_count": 0,
        "last_error": None,
    })
    assert events[-1]["event"] == {
        "type": "memory.changed",
        "payload": {
            "pendingCount": 2,
            "processingCount": 1,
            "failedCount": 0,
            "lastError": None,
        },
    }


def test_manual_compaction_rehydrates_desktop_with_persisted_summary(tmp_path: Path) -> None:
    import asyncio

    events: list[dict] = []
    runtime = DesktopRuntime(events.append)
    summary = SimpleNamespace(
        role="compactionSummary",
        summary="durable compacted context",
        tokens_before=8120,
        timestamp=1724470000,
    )
    state = SimpleNamespace(messages=[])

    async def compact(_reason: str) -> dict:
        state.messages = [summary]
        return {"performed": True, "summary_preview": "durable compacted context"}

    runtime._workspace = tmp_path
    runtime._session = SimpleNamespace(
        compact=compact,
        session_manager=SimpleNamespace(header=SimpleNamespace(id="session-compact")),
        model=SimpleNamespace(id="m", name="Model", provider="test"),
        thinking_level=None,
        tools=[],
        state=state,
        collaboration_mode="default",
        plan_state=SimpleNamespace(to_payload=lambda: {
            "phase": "idle",
            "activePlanId": None,
            "latestRevision": None,
            "pendingQuestion": None,
        }),
    )

    result = asyncio.run(runtime._session_compact({}))

    assert result["performed"] is True
    changed = events[-1]["event"]
    assert changed["type"] == "session.changed"
    assert changed["payload"]["messages"][0]["role"] == "compactionSummary"
    assert changed["payload"]["messages"][0]["summary"] == "durable compacted context"


def test_session_snapshot_includes_authoritative_plan_state() -> None:
    import asyncio

    state_payload = {
        "mode": "plan",
        "phase": "ready",
        "activePlanId": "plan-1",
        "latestRevision": {"planId": "plan-1", "revision": 2, "digest": "abc"},
        "pendingQuestion": None,
        "latestRun": None,
        "recoveryError": None,
        "handoffTargetSessionId": None,
    }
    runtime = DesktopRuntime(lambda _event: None)
    runtime._session = SimpleNamespace(
        collaboration_mode="plan",
        plan_state=SimpleNamespace(to_payload=lambda: state_payload),
        session_manager=SimpleNamespace(header=SimpleNamespace(id="session-plan")),
        state=SimpleNamespace(messages=[]),
        get_stats=lambda: {"messages": 0},
    )

    result = asyncio.run(runtime.dispatch("session.snapshot", {}))

    assert result["sessionId"] == "session-plan"
    assert result["collaborationMode"] == "plan"
    assert result["planState"] == state_payload


def test_plan_handoff_opens_fresh_review_session_after_core_persists(
    tmp_path: Path,
) -> None:
    import asyncio

    calls: list[tuple[str, int, str, bool]] = []
    target_manager = SimpleNamespace(header=SimpleNamespace(id="session-child"))

    def handoff(plan_id: str, revision: int, digest: str, *, attach: bool):
        calls.append((plan_id, revision, digest, attach))
        source.__dict__.update(target_session.__dict__)
        return target_manager

    source = SimpleNamespace(handoff_plan_to_new_session=handoff)
    ready_state = {
        "mode": "plan",
        "phase": "ready",
        "activePlanId": "plan-1",
        "latestRevision": {
            "planId": "plan-1", "revision": 2, "digest": "digest-2",
            "title": "Plan", "markdown": "body", "sourceMessageId": "message-1",
        },
        "pendingQuestion": None,
    }
    target_session = SimpleNamespace(
        session_manager=target_manager,
        state=SimpleNamespace(messages=[]),
        model=SimpleNamespace(id="model", name="Model", provider="test"),
        thinking_level=None,
        tools=[],
        collaboration_mode="plan",
        plan_state=SimpleNamespace(to_payload=lambda: ready_state),
        memory_enabled=False,
        memory_identity=None,
    )
    events: list[dict] = []
    runtime = DesktopRuntime(events.append)
    runtime._workspace = tmp_path
    runtime._session = source  # type: ignore[assignment]

    async def replace_session(
        workspace: Path, *, session_id: str | None = None, resume: bool = False,
    ) -> None:
        assert workspace == tmp_path
        assert session_id == "session-child"
        assert resume is False
        runtime._session = target_session  # type: ignore[assignment]

    runtime._replace_session = replace_session  # type: ignore[method-assign]

    result = asyncio.run(runtime.dispatch("plan.handoff", {
        "planId": "plan-1", "revision": 2, "digest": "digest-2",
    }))

    assert calls == [("plan-1", 2, "digest-2", True)]
    assert result["sessionId"] == "session-child"
    assert result["planState"] == ready_state
    assert events[-1]["event"] == {"type": "session.changed", "payload": result}


def test_opening_saved_session_does_not_persist_abandoned_empty_session(
    tmp_path: Path,
) -> None:
    import asyncio
    import coding_agent.core.config as config

    workspace = Path(tmp_path.anchor)

    saved = SessionManager.create(
        cwd=str(workspace),
        sessions_dir=config.get_sessions_dir(),
    )
    saved.append_message(UserMessage(content="existing question"))
    saved.append_message(AssistantMessage(content=[TextContent(text="existing answer")]))

    async def exercise() -> None:
        runtime = DesktopRuntime(lambda _event: None)
        try:
            opened = await runtime.dispatch(
                "workspace.open",
                {"path": str(workspace), "resume": True},
            )
            assert opened["sessionId"] == saved.header.id

            created = await runtime.dispatch("session.new", {})
            assert created["sessionId"] != saved.header.id
            assert [item["id"] for item in await runtime.dispatch("session.list", {})] == [
                saved.header.id,
            ]

            await runtime.dispatch("session.open", {"sessionId": saved.header.id})
            assert [item["id"] for item in await runtime.dispatch("session.list", {})] == [
                saved.header.id,
            ]
        finally:
            await runtime.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "command",
    [
        "find . -maxdepth 3 -type f | head -100",
        "rg --files packages/core | head -20",
        "git status --short",
        "git log --oneline -20 --no-merges 2>&1 | head -30",
        "cd packages/core && git log --oneline -5",
    ],
)
def test_read_only_bash_commands_skip_approval(command: str) -> None:
    assert _is_read_only_bash_command(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "find . -delete",
        "find . -exec rm {} ;",
        "rm -rf build",
        "git checkout -- file.py",
        "cat file > copy",
        "cat missing.txt 2> errors.txt",
        "cat $(pwd)/secret",
        "git branch new-feature",
        "git diff --output=changes.patch",
        "rg --pre 'touch marker' pattern .",
        "sort input.txt -o output.txt",
        "uniq input.txt output.txt",
        "tree -o tree.txt",
        "rg token ..\\private",
        "bash -c 'pwd'",
    ],
)
def test_mutating_or_ambiguous_bash_commands_still_require_approval(command: str) -> None:
    assert _is_read_only_bash_command(command) is False


def _tool_context(name: str, args: dict) -> BeforeToolCallContext:
    tool_call = ToolCall(id="tool-call-1", name=name, arguments=args)
    return BeforeToolCallContext(
        assistant_message=AssistantMessage(content=[tool_call]),
        tool_call=tool_call,
        args=args,
        context=AgentContext(),
    )


def test_read_only_bash_hook_does_not_wait_for_approval() -> None:
    import asyncio

    events: list[dict] = []
    runtime = DesktopRuntime(events.append)
    context = _tool_context(
        "bash",
        {"command": "find . -maxdepth 3 -type f | head -100"},
    )

    result = asyncio.run(runtime._before_tool_call(context, asyncio.Event()))

    assert result is None
    assert events == []


def test_mutating_tool_waits_for_and_accepts_explicit_approval() -> None:
    import asyncio

    async def scenario() -> tuple[object, list[dict]]:
        events: list[dict] = []
        runtime = DesktopRuntime(events.append)
        task = asyncio.create_task(runtime._before_tool_call(
            _tool_context("write", {"path": "answer.txt", "content": "ok"}),
            asyncio.Event(),
        ))
        await asyncio.sleep(0)
        approval = events[0]["event"]["payload"]
        await runtime._approval_resolve({
            "approvalId": approval["approvalId"],
            "approved": True,
        })
        return await task, events

    result, events = asyncio.run(scenario())

    assert result is None
    assert [event["event"]["type"] for event in events] == ["approval.requested"]


def test_unanswered_approval_expires_instead_of_waiting_forever() -> None:
    import asyncio

    events: list[dict] = []
    runtime = DesktopRuntime(events.append, approval_timeout_seconds=0.001)

    result = asyncio.run(runtime._before_tool_call(
        _tool_context("bash", {"command": "rm -rf build"}),
        asyncio.Event(),
    ))

    assert result is not None
    assert result.block is True
    assert result.reason == "工具审批已超时"
    assert [event["event"]["type"] for event in events] == [
        "approval.requested",
        "approval.expired",
    ]


def test_default_approval_preserves_upstream_fd_redirect_support() -> None:
    from coding_agent.desktop.runtime import _is_read_only_bash_command
    assert _is_read_only_bash_command("git log --oneline -20 --no-merges 2>&1 | head -30")
    assert not _is_read_only_bash_command("cat missing.txt 2> errors.txt")
