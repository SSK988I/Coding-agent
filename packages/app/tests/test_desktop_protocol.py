from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from agent_core import SessionManager
from agent_llm import AssistantMessage, Model, ModelCost, TextContent, ToolCall, UserMessage

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.messages import convert_to_llm
from coding_agent.desktop.protocol import RpcError, parse_request, to_jsonable
from coding_agent.desktop.runtime import DesktopRuntime


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
