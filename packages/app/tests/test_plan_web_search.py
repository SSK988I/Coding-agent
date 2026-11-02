"""Plan permits approved public research, not shell or delegated network access."""
from __future__ import annotations

import asyncio

import pytest
from agent_core import AgentContext, BeforeToolCallContext
from agent_llm import AssistantMessage, AssistantMessageEventStream, Context, TextContent, Tool, ToolCall
from agent_llm.providers.deepseek_models import DEEPSEEK_MODELS

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.plan_mode import PlanState
from coding_agent.core.retry import RetryPolicy
from coding_agent.search.tool import WebSearchTool
from coding_agent.search.types import SearchQuery, SearchResponse


class PublicBackend:
    name = "public-test"

    def __init__(self):
        self.queries = []

    async def search(self, query: SearchQuery, *, signal=None) -> SearchResponse:
        self.queries.append(query)
        return SearchResponse(query=query.query, backend=self.name, results=())

    async def aclose(self) -> None:
        pass

    def request_stop(self) -> None:
        pass


def tool_context(session, name, arguments):
    call = ToolCall(id="plan-call", name=name, arguments=arguments)
    return BeforeToolCallContext(
        assistant_message=AssistantMessage(content=[call]), tool_call=call,
        args=arguments, context=AgentContext(tools=session.tools),
    )


def test_builtin_search_is_observation_and_exposed_during_plan_drafting(tmp_path):
    session = AgentSession(AgentSessionConfig(
        cwd=str(tmp_path), model=DEEPSEEK_MODELS["deepseek-flash"],
        web_search_enabled=True, web_search_backend=PublicBackend(),
    ))
    try:
        assert WebSearchTool(PublicBackend()).plan_access == "observe"
        session.enter_plan_mode()
        assert session.web_search_available is True
        assert "web_search" in {tool.name for tool in session.tools}
        assert not {"write", "edit", "bash"} & {tool.name for tool in session.tools}
        assert session.collaboration_mode == "plan" and session.plan_state.phase == "drafting"
    finally:
        asyncio.run(session.aclose())


def test_toggling_search_in_plan_rebuilds_observation_tools_and_blocks_disabled_calls(tmp_path):
    async def scenario():
        session = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=DEEPSEEK_MODELS["deepseek-flash"],
            web_search_enabled=False, permission_mode="full_access",
        ))
        try:
            session.enter_plan_mode()
            assert session.web_search_available is False
            await session.set_web_search_enabled(True)
            assert session.web_search_available is True
            assert "web_search" in {tool.name for tool in session.tools}
            context = tool_context(session, "web_search", {"query": "public landmark"})
            assert await session._before_tool_call(context, asyncio.Event()) is None
            await session.set_web_search_enabled(False)
            assert session.web_search_available is False
            assert "web_search" not in {tool.name for tool in session.tools}
            context = tool_context(session, "web_search", {"query": "public landmark"})
            decision = await session._before_tool_call(context, asyncio.Event())
            assert decision and decision.block
            await session.set_web_search_enabled(True)
            assert session.web_search_available is True
            assert "web_search" in {tool.name for tool in session.tools}
            assert session.collaboration_mode == "plan" and session.plan_state.phase == "drafting"
        finally:
            await session.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("permission,blocked", [("ask", True), ("auto", False), ("full_access", False)])
def test_plan_function_search_retains_normal_permission_gate(tmp_path, permission, blocked):
    async def scenario():
        session = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=DEEPSEEK_MODELS["deepseek-flash"],
            web_search_enabled=True, permission_mode=permission,
        ))
        try:
            session.enter_plan_mode()
            decision = await session._before_tool_call(
                tool_context(session, "web_search", {"query": "public landmark"}), asyncio.Event(),
            )
            assert bool(decision and decision.block) is blocked
            if blocked:
                assert decision.code == "PERMISSION_APPROVAL_REQUIRED"
        finally:
            await session.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("revocation,code", [
    ("ready", "WEB_SEARCH_UNAVAILABLE"), ("disabled", "WEB_SEARCH_UNAVAILABLE"),
    ("aborted", "WEB_SEARCH_ABORTED"), (None, None),
])
def test_plan_function_search_rechecks_capability_after_approval_await(tmp_path, revocation, code):
    async def scenario():
        approvals, backend = [], PublicBackend()

        async def authorize(context, signal):
            approvals.append(context.tool_call.id)
            assert session.web_search_available is True
            await asyncio.sleep(0)
            if revocation == "ready":
                session._plan_state = PlanState(mode="plan", phase="ready")
                session._refresh_collaboration_runtime()
            elif revocation == "disabled":
                await session.set_web_search_enabled(False)
            elif revocation == "aborted":
                signal.set()
            return None

        session = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=DEEPSEEK_MODELS["deepseek-flash"],
            web_search_enabled=True, web_search_backend=backend,
            permission_mode="ask", before_tool_call=authorize,
        ))
        try:
            session.enter_plan_mode()
            tool = next(tool for tool in session.tools if tool.name == "web_search")
            # Retain the original call and tool surface while the dialog yields.
            context = tool_context(session, "web_search", {"query": "public landmark"})
            signal = asyncio.Event()
            decision = await session._before_tool_call(context, signal)
            assert approvals == ["plan-call"]
            if code is not None:
                assert decision and decision.block and decision.code == code
            else:
                assert decision is None
                await tool.execute(context.tool_call.id, context.args, signal=signal)
            assert backend.queries == ([] if revocation else [SearchQuery("public landmark")])
        finally:
            await session.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("name,args", [
    ("bash", {"command": "pytest -q"}), ("bash", {"command": "pnpm build"}),
    ("write", {"path": "output.txt", "content": "changed"}),
    ("edit", {"path": "output.txt", "oldText": "a", "newText": "b"}),
])
def test_plan_research_does_not_open_shell_or_mutation(tmp_path, name, args):
    async def scenario():
        session = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=DEEPSEEK_MODELS["deepseek-flash"],
            web_search_enabled=True, permission_mode="full_access",
        ))
        try:
            session.enter_plan_mode()
            decision = await session._before_tool_call(tool_context(session, name, args), asyncio.Event())
            assert decision and decision.block
        finally:
            await session.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["awaiting_answer", "ready", "uncertain", "recovery_error"])
@pytest.mark.parametrize("model_id", ["deepseek-flash", "deepseek-v4-pro"])
def test_non_drafting_plan_phases_cannot_reuse_search(tmp_path, phase, model_id):
    async def scenario():
        session = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=DEEPSEEK_MODELS[model_id], web_search_enabled=True,
            permission_mode="full_access",
        ))
        try:
            session._plan_state = PlanState(mode="plan", phase=phase)
            session._refresh_collaboration_runtime()
            assert session.web_search_available is False
            assert session.native_web_search_enabled is False
            assert "web_search" not in {tool.name for tool in session.tools}
            decision = await session._before_tool_call(
                tool_context(session, "web_search", {"query": "public landmark"}), asyncio.Event(),
            )
            assert decision and decision.block
        finally:
            await session.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("allowed", [False, True])
def test_plan_pro_native_search_waits_for_approval_and_preserves_plan(tmp_path, monkeypatch, allowed):
    async def scenario():
        approvals, requests = [], []

        async def authorize(_signal):
            approvals.append(allowed)
            return allowed

        def provider(_model, context, options):
            requests.append((context, options or {}))
            stream = AssistantMessageEventStream()
            stream.push({"type": "done", "reason": "stop", "message": AssistantMessage(content=[TextContent(text="ok")])})
            return stream

        monkeypatch.setattr("agent_llm.compat.stream_simple", provider)
        session = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=DEEPSEEK_MODELS["deepseek-v4-pro"],
            web_search_enabled=True, permission_mode="ask", before_native_web_search=authorize,
            retry_policy=RetryPolicy(max_retries=0),
        ))
        try:
            session.enter_plan_mode()
            stream = session._create_stream_fn()(session.model, Context(tools=[Tool(name="web_search")]))
            async for _event in stream:
                pass
            assert approvals == [allowed]
            context, options = requests[0]
            assert bool(options.get("web_search")) is allowed
            assert not any(tool.name == "web_search" for tool in context.tools or [])
            assert session.collaboration_mode == "plan" and session.plan_state.phase == "drafting"
            assert session.plan_state.latest_revision is None
        finally:
            await session.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("revocation", ["ready", "disabled", "recovery_error"])
def test_plan_native_search_rechecks_capability_after_approval_await(tmp_path, monkeypatch, revocation):
    async def scenario():
        approvals, requests = [], []

        async def authorize(_signal):
            approvals.append(True)
            await asyncio.sleep(0)
            if revocation == "disabled":
                await session.set_web_search_enabled(False)
            else:
                session._plan_state = PlanState(mode="plan", phase=revocation)
                session._refresh_collaboration_runtime()
            return True

        def provider(_model, context, options):
            requests.append((context, options or {}))
            stream = AssistantMessageEventStream()
            stream.push({"type": "done", "reason": "stop", "message": AssistantMessage(content=[TextContent(text="ok")])})
            return stream

        monkeypatch.setattr("agent_llm.compat.stream_simple", provider)
        session = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), model=DEEPSEEK_MODELS["deepseek-v4-pro"],
            web_search_enabled=True, permission_mode="ask", before_native_web_search=authorize,
            retry_policy=RetryPolicy(max_retries=0),
        ))
        try:
            session.enter_plan_mode()
            stream = session._create_stream_fn()(session.model, Context(tools=[Tool(name="web_search")]))
            async for _event in stream:
                pass
            assert approvals == [True]
            context, options = requests[0]
            assert session.web_search_available is False
            assert not options.get("web_search")
            assert "<native_web_search>" not in (context.system_prompt or "")
            assert "web_search" not in {tool.name for tool in context.tools or []}
        finally:
            await session.aclose()
    asyncio.run(scenario())


def test_readonly_subagent_does_not_inherit_parent_plan_search(tmp_path, monkeypatch):
    requests = []

    def provider(_model, context, options):
        requests.append((context, options or {}))
        stream = AssistantMessageEventStream()
        stream.push({"type": "done", "reason": "stop", "message": AssistantMessage(content=[TextContent(text="ok")])})
        return stream

    monkeypatch.setattr("agent_llm.compat.stream_simple", provider)
    session = AgentSession(AgentSessionConfig(
        cwd=str(tmp_path), model=DEEPSEEK_MODELS["deepseek-v4-pro"],
        web_search_enabled=True, permission_mode="full_access",
    ))
    try:
        session.enter_plan_mode()
        assert session.native_web_search_enabled is True
        child = session._make_readonly_subagent()
        assert "web_search" not in {tool.name for tool in child.state.tools}
        assert not {"bash", "edit", "write", "subagent_spawn"} & {tool.name for tool in child.state.tools}
        async def request():
            context = Context(tools=[Tool(name=tool.name) for tool in child.state.tools])
            await child.stream_fn(session.model, context, {"web_search": True}).result()

        asyncio.run(request())
        context, options = requests[0]
        assert not options.get("web_search")
        assert "<native_web_search>" not in (context.system_prompt or "")
        assert "web_search" not in {tool.name for tool in context.tools or []}
    finally:
        asyncio.run(session.aclose())
