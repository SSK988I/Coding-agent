from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from agent_core import AgentContext, BeforeToolCallContext
from agent_llm import AssistantMessage, Model, ModelCost, TextContent, ToolCall, UserMessage

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.messages import convert_to_llm
from coding_agent.desktop.protocol import RpcError, parse_request, to_jsonable
from coding_agent.desktop.runtime import DesktopRuntime, _is_read_only_bash_command


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
    assert session.agent.before_tool_call is before
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
    ]


@pytest.mark.parametrize(
    "command",
    [
        "find . -maxdepth 3 -type f | head -100",
        "rg --files packages/core | head -20",
        "git status --short",
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
