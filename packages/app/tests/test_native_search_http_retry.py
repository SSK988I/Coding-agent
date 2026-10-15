"""Exercise the real SDK and provider; replace only the HTTP transport."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from agent_core import SessionManager
from agent_llm import Context, UserMessage
from agent_llm.api.deepseek_responses import stream
from agent_llm.providers.deepseek_models import DEEPSEEK_MODELS

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.retry import RetryPolicy
from coding_agent.desktop.side_conversations import SideConversations


@pytest.fixture
def mock_http(monkeypatch):
    """Keep AsyncOpenAI and HTTPX real, injecting a local transport only."""
    clients = []
    initialize = httpx.AsyncClient.__init__

    def install(handler):
        def initialize_with_transport(client, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            kwargs["trust_env"] = False
            initialize(client, *args, **kwargs)
            clients.append(client)
        monkeypatch.setattr(httpx.AsyncClient, "__init__", initialize_with_transport)
        return clients
    return install


def failure_response(request, failure):
    if failure == "timeout":
        raise httpx.ReadTimeout("Local simulated timeout", request=request)
    return httpx.Response(
        int(failure), headers={"retry-after-ms": "1"},
        json={"error": {"message": "temporarily unavailable", "type": "server_error"}},
    )


def successful_response():
    event = {
        "type": "response.completed", "sequence_number": 1,
        "response": {
            "id": "local-response", "object": "response", "created_at": 0,
            "model": "deepseek-flash", "status": "completed",
            "output": [{
                "type": "message", "id": "local-message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "done", "annotations": []}],
            }],
        },
    }
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content=f"event: response.completed\ndata: {json.dumps(event)}\n\ndata: [DONE]\n\n",
    )


@pytest.mark.parametrize("failure", ["503", "429", "timeout"])
@pytest.mark.parametrize("core_retries", [0, 1])
@pytest.mark.parametrize("permission", ["full_access", "ask"])
def test_real_sdk_retry_cannot_keep_native_search_after_parent_enters_plan(tmp_path, mock_http, failure, core_retries, permission):
    async def scenario():
        parent = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=DEEPSEEK_MODELS["deepseek-flash"],
            session_manager=SessionManager.create(cwd=str(tmp_path), sessions_dir=tmp_path / "sessions"),
            get_api_key=lambda _provider: "local-test-key", web_search_enabled=True,
            retry_policy=RetryPolicy(max_retries=core_retries, initial_delay=0, max_delay=0),
        ))
        requests, approvals, checks, retries = [], [], [], []
        waiting = asyncio.Event()
        def emit(_event, payload):
            if payload.get("approval"):
                approvals.append(payload["approval"]["approvalId"])
                waiting.set()
        def handle(request):
            assert request.method == "POST" and request.url.path.endswith("/responses")
            requests.append((json.loads(request.content), parent.collaboration_mode, request.headers.get("x-stainless-retry-count")))
            if len(requests) == 1:
                parent.enter_plan_mode()
            if len(requests) == 1 or core_retries == 0:
                return failure_response(request, failure)
            return successful_response()
        clients = mock_http(handle)
        side = SideConversations(parent, tmp_path / "cache", emit, lambda: permission)
        identity = side.open()["conversationId"]
        live = side.records[identity]
        authorize = live.session._config.before_native_web_search
        async def checked_authorize(signal):
            allowed = await authorize(signal)
            checks.append(allowed)
            return allowed
        live.session._config.before_native_web_search = checked_authorize
        live.session.on_event(lambda event: retries.append(event) if event.get("type") == "retry" else None)
        try:
            side.send(identity, "research")
            if permission == "ask":
                await asyncio.wait_for(waiting.wait(), 3)
                assert requests == []
                side.resolve(identity, live.approval["approvalId"], True)
            await asyncio.wait_for(live.task, 5)
            assert len(requests) == core_retries + 1, "SDK must not retry inside a Core attempt"
            assert len(retries) == core_retries
            assert checks == ([True, False] if core_retries else [True])
            assert len(set(approvals)) == int(permission == "ask")
            assert requests[0][0]["tools"].count({"type": "web_search"}) == 1
            assert "<native_web_search>" in requests[0][0]["instructions"]
            assert all(retry_count == "0" for _body, _mode, retry_count in requests)
            for body, mode, _retry_count in requests[1:]:
                assert mode == "plan"
                assert not any(tool.get("type") == "web_search" or tool.get("name") == "web_search" for tool in body.get("tools", []))
                assert "<native_web_search>" not in body.get("instructions", "")
            assert live.status == ("settled" if core_retries else "failed"), live.error
        finally:
            await side.close()
            await parent.aclose()
            await asyncio.gather(*(client.aclose() for client in clients))
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["503", "429", "timeout"])
def test_deepseek_provider_never_enables_sdk_retries_from_stream_options(mock_http, failure):
    async def scenario():
        requests = []
        def handle(request):
            requests.append(json.loads(request.content))
            return failure_response(request, failure)
        clients = mock_http(handle)
        event_stream = stream(
            DEEPSEEK_MODELS["deepseek-flash"],
            Context(messages=[UserMessage(content="research")]),
            {"api_key": "local-test-key", "web_search": True, "max_retries": 9},
        )
        try:
            events = [event async for event in event_stream]
            assert events[-1]["type"] == "error"
            assert (await event_stream.result()).stop_reason == "error"
            assert len(requests) == 1, "Stream options must not reopen unauthorized SDK retries"
        finally:
            await event_stream.aclose()
            await asyncio.gather(*(client.aclose() for client in clients))
    asyncio.run(scenario())
