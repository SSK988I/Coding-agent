"""Usage anchors must describe the configuration of the actual model request."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from agent_core import SessionManager
from agent_llm import AssistantMessage, Context, Model, ModelCost, TextContent, Usage, UserMessage
from agent_llm.event_stream import AssistantMessageEventStream
from agent_llm.providers.deepseek_models import DEEPSEEK_MODELS

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.retry import RetryPolicy
from coding_agent.memory.types import MemoryContext, MemoryIdentity


def _model(*, provider="test"):
    return Model(id="request-test", provider=provider, api="openai-completions",
                 base_url="https://model.invalid/v1", context_window=1_000_000,
                 cost=ModelCost(0, 0, 0, 0))


def _session(tmp_path, **overrides: Any):
    options: dict[str, Any] = dict(
        cwd=str(tmp_path), model=_model(), system_prompt="base instruction",
        tools=[], no_builtin_tools=True,
        session_manager=SessionManager.create(cwd=str(tmp_path), in_memory=True),
        retry_policy=RetryPolicy(max_retries=0),
    )
    options.update(overrides)
    return AgentSession(AgentSessionConfig(**options))


def _install_stream(monkeypatch, *, delayed=False, before_return=None, fail_first=False, hold_first=None):
    requests, messages = [], []

    def stream(model, context, options=None):
        index = len(messages)
        requests.append((copy.deepcopy(context), copy.deepcopy(options or {})))
        message = AssistantMessage(
            model=model.id, provider=model.provider, api=model.api,
            content=[TextContent(text=f"answer {index}")],
            usage=Usage(input=100 * (index + 1), cache_read=900, output=100,
                        cache_read_reported=True),
        )
        if fail_first and index == 0:
            message.stop_reason = "error"
            message.error_message = "HTTP 503 service unavailable"
        messages.append(message)
        result = AssistantMessageEventStream()

        def finish():
            result.push({"type": "start", "partial": message})
            if message.stop_reason == "error":
                result.push({"type": "error", "reason": "error", "error": message})
            else:
                result.push({"type": "done", "reason": "stop", "message": message})
            result.end(message)

        if before_return is not None:
            before_return(index)
        if delayed:
            async def produce():
                await asyncio.sleep(0)
                if index == 0 and hold_first is not None:
                    await hold_first.wait()
                finish()
            result.set_producer(asyncio.create_task(produce()))
        else:
            finish()
        return result

    monkeypatch.setattr("agent_llm.compat.stream_simple", stream)
    return requests, messages


@pytest.mark.parametrize("delayed", [False, True])
def test_actual_request_is_bound_before_message_end_stats(tmp_path, monkeypatch, delayed):
    async def scenario():
        _requests, messages = _install_stream(monkeypatch, delayed=delayed)
        session = _session(tmp_path)
        at_end = []
        session.on_event(lambda event: at_end.append(session.get_stats().metrics)
                         if event.get("type") == "message_end"
                         and getattr(event.get("message"), "role", None) == "assistant" else None)
        try:
            await session.prompt("question")
            assert getattr(messages[0], "_usage_context_fingerprint", None)
            assert at_end[-1].context_estimated is False
            assert at_end[-1].context_tokens == 1100
            assert session.get_stats().metrics.context_estimated is False
        finally:
            await session.aclose()
    asyncio.run(scenario())


def test_web_configuration_change_invalidates_another_preexisting_branch(tmp_path, monkeypatch):
    async def scenario():
        _install_stream(monkeypatch)
        backend = SimpleNamespace(aclose=AsyncMock())
        session = _session(tmp_path, no_builtin_tools=False, web_search_backend=backend)
        manager = session.session_manager
        try:
            await session.prompt("branch A")
            leaf_a = manager.leaf_id
            assert leaf_a is not None
            assert session.get_stats().metrics.context_tokens == 1100
            manager.branch(manager.get_branch()[0].id)
            session.agent.load_messages(manager.build_session_context().messages)
            session.refresh_plan_state_from_branch()
            await session.prompt("branch B")
            await session.set_web_search_enabled(True)
            assert session.get_stats().metrics.context_estimated is True

            manager.branch(leaf_a)
            session.agent.load_messages(manager.build_session_context().messages)
            session.refresh_plan_state_from_branch()
            assert session.get_stats().metrics.context_estimated is True

            await session.prompt("fresh request on A")
            assert session.get_stats().metrics.context_estimated is False
            assert session.get_stats().metrics.context_tokens == 1300
        finally:
            await session.aclose()
    asyncio.run(scenario())


def test_restored_jsonl_has_unknown_request_configuration_until_a_fresh_request(tmp_path, monkeypatch):
    async def scenario():
        _install_stream(monkeypatch)
        manager = SessionManager.create(cwd=str(tmp_path), sessions_dir=tmp_path / "sessions")
        session = _session(tmp_path, session_manager=manager)
        try:
            await session.prompt("persist this")
            assert session.get_stats().metrics.context_estimated is False
            manager.flush()
            assert manager.path is not None
            assert "_usage_context_fingerprint" not in manager.path.read_text(encoding="utf-8")
            restored = _session(tmp_path, session_manager=SessionManager.open(manager.path))
            try:
                assert restored.get_stats().metrics.cache_hit_rate == 90
                assert restored.get_stats().metrics.context_estimated is True
                await restored.prompt("fresh request after restore")
                assert restored.get_stats().metrics.context_estimated is False
                assert restored.get_stats().metrics.context_tokens == 1200
            finally:
                await restored.aclose()
        finally:
            await session.aclose()
    asyncio.run(scenario())


def test_transient_memory_binding_expires_when_the_prompt_is_cleaned_up(tmp_path, monkeypatch):
    async def scenario():
        requests, _messages = _install_stream(monkeypatch)
        memory = SimpleNamespace(
            enabled=True, before_task=AsyncMock(return_value=MemoryContext(
                records=[], prompt_block="<long_term_memory>temporary preference</long_term_memory>",
                estimated_tokens=10,
            )), after_task=AsyncMock(), request_stop=lambda: None,
            close=AsyncMock(),
        )
        session = _session(tmp_path, memory_service=memory, memory_identity=MemoryIdentity("u", "p"))
        at_end = []
        session.on_event(lambda event: at_end.append(session.get_stats().metrics)
                         if event.get("type") == "message_end"
                         and getattr(event.get("message"), "role", None) == "assistant" else None)
        try:
            await session.prompt("question", _extract_memory=False)
            assert "<long_term_memory>" in requests[0][0].system_prompt
            assert at_end[-1].context_estimated is False
            assert "<long_term_memory>" not in session.state.system_prompt
            assert session.get_stats().metrics.context_estimated is True
            await session.prompt("without memory", _retrieve_memory=False, _extract_memory=False)
            assert session.get_stats().metrics.context_estimated is False
            assert session.get_stats().metrics.context_tokens == 1200
        finally:
            await session.aclose()
    asyncio.run(scenario())


def test_tool_schema_mutation_requires_a_new_request_binding(tmp_path, monkeypatch):
    async def scenario():
        _install_stream(monkeypatch)
        tool = SimpleNamespace(name="inspect", description="Inspect data", parameters={
            "type": "object", "properties": {"path": {"type": "string"}},
        })
        session = _session(tmp_path, tools=[tool])
        try:
            await session.prompt("before schema edit")
            assert session.get_stats().metrics.context_estimated is False
            tool.parameters["properties"]["path"]["description"] = "new description"
            assert session.get_stats().metrics.context_estimated is True
            await session.prompt("after schema edit")
            assert session.get_stats().metrics.context_estimated is False
            assert session.get_stats().metrics.context_tokens == 1200
        finally:
            await session.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["tool", "compat"])
def test_request_binding_freezes_mutable_configuration_at_request_start(tmp_path, monkeypatch, changed):
    async def scenario():
        tool = SimpleNamespace(name="inspect", description="Inspect data", parameters={"type": "object"})
        model = _model()
        compat = {"supports_system_role": True}
        model.compat = compat

        def mutate_after_request_started(_index):
            if changed == "tool":
                tool.parameters["description"] = "changed during provider execution"
            else:
                compat["supports_system_role"] = False

        _install_stream(monkeypatch, delayed=True, before_return=mutate_after_request_started)
        session = _session(tmp_path, model=model, tools=[tool])
        try:
            await session.prompt("capture before mutable configuration changes")
            assert session.get_stats().metrics.context_estimated is True
        finally:
            await session.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("allowed", [False, True])
def test_native_search_request_binding_survives_shaping_but_not_revocation(tmp_path, monkeypatch, allowed):
    async def scenario():
        requests, _messages = _install_stream(monkeypatch)
        authorize = AsyncMock(return_value=allowed)
        session = _session(
            tmp_path, model=DEEPSEEK_MODELS["deepseek-v4-pro"], no_builtin_tools=False,
            web_search_enabled=True, web_search_backend=SimpleNamespace(aclose=AsyncMock()),
            before_native_web_search=authorize, permission_mode="ask",
        )
        try:
            await session.prompt("research")
            authorize.assert_awaited_once()
            assert session.native_web_search_enabled is True
            assert requests[0][1].get("web_search", False) is allowed
            assert ("<native_web_search>" in requests[0][0].system_prompt) is allowed
            assert all(tool.name != "web_search" for tool in requests[0][0].tools or [])
            assert session.get_stats().metrics.context_estimated is False
            await session.set_web_search_enabled(False)
            assert session.get_stats().metrics.context_estimated is True
            await session.prompt("offline")
            assert session.get_stats().metrics.context_estimated is False
            assert session.get_stats().metrics.context_tokens == 1200
        finally:
            await session.aclose()
    asyncio.run(scenario())


def test_retry_binds_each_attempt_to_its_own_policy_snapshot(tmp_path, monkeypatch):
    async def scenario():
        _requests, messages = _install_stream(monkeypatch, fail_first=True)
        session = _session(tmp_path, permission_mode="ask", retry_policy=RetryPolicy(
            max_retries=1, initial_delay=0, max_delay=0,
        ))

        def change_host_policy(event):
            if event.get("type") == "retry":
                session._config.permission_mode = "full_access"

        session.on_event(change_host_policy)
        try:
            await session.prompt("retry once")
            assert len(messages) == 2
            first = getattr(messages[0], "_usage_context_fingerprint", None)
            second = getattr(messages[1], "_usage_context_fingerprint", None)
            assert first and second and first != second
            assert session.get_stats().metrics.context_estimated is False
            assert session.get_stats().metrics.context_tokens == 1200
        finally:
            await session.aclose()
    asyncio.run(scenario())


def test_concurrent_auxiliary_stream_cannot_rebind_the_parent_response(tmp_path, monkeypatch):
    async def scenario():
        parent_started = asyncio.Event()
        allow_parent_terminal = asyncio.Event()
        _install_stream(
            monkeypatch, delayed=True, hold_first=allow_parent_terminal,
            before_return=lambda index: parent_started.set() if index == 0 else None,
        )
        session = _session(tmp_path)

        async def auxiliary():
            await parent_started.wait()
            stream = session.agent.stream_fn(session.model, Context(
                system_prompt="unrelated memory extraction prompt",
                messages=[UserMessage(content="auxiliary")],
            ), None)
            async for _event in stream:
                pass
            await stream.result()
            await stream.aclose()
            allow_parent_terminal.set()

        try:
            await asyncio.gather(session.prompt("parent question"), auxiliary())
            assert session.get_stats().metrics.context_estimated is False
            assert session.get_stats().metrics.context_tokens == 1100
        finally:
            await session.aclose()
    asyncio.run(scenario())
