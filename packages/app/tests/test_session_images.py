from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest

from agent_core import AgentContext, BeforeToolCallContext, SessionManager
from agent_llm import AssistantMessage, Context, ImageContent, TextContent, ToolCall, UserMessage
from agent_llm.event_stream import AssistantMessageEventStream, lazy_stream
from agent_llm.image_utils import ImageInputError
from agent_llm.providers.deepseek_models import DEEPSEEK_MODELS
from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.permissions import is_approval_safe_shell_command, requires_approval
from coding_agent.core.retry import RetryPolicy


def image_content() -> ImageContent:
    output = BytesIO()
    Image.new("RGB", (3, 2), "blue").save(output, format="PNG")
    return ImageContent(data=base64.b64encode(output.getvalue()).decode(), mime_type="image/png")


def make_session(tmp_path, **options) -> AgentSession:
    return AgentSession(AgentSessionConfig(
        cwd=str(tmp_path), model=DEEPSEEK_MODELS["deepseek-flash"],
        session_manager=SessionManager.create(cwd=str(tmp_path), in_memory=True),
        retry_policy=RetryPolicy(max_retries=1, initial_delay=0, max_delay=0),
        **options,
    ))


def image_tool_context(path: str, session: AgentSession) -> BeforeToolCallContext:
    call = ToolCall(id="image-call", name="view_image", arguments={"path": path})
    return BeforeToolCallContext(assistant_message=AssistantMessage(content=[call]),
                                 tool_call=call, args=call.arguments,
                                 context=AgentContext(tools=session.tools))


def finished_stream(message: AssistantMessage) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()
    stream.push({"type": "start", "partial": message})
    stream.push({"type": "done", "reason": message.stop_reason, "message": message})
    stream.end(message)
    return stream


def test_image_tool_follows_current_model_and_plan_mode(tmp_path) -> None:
    async def scenario():
        session = make_session(tmp_path)
        try:
            assert "view_image" in {tool.name for tool in session.tools}
            session.enter_plan_mode()
            assert "view_image" in {tool.name for tool in session.tools}
            session.set_model(DEEPSEEK_MODELS["deepseek-v4-pro"])
            assert "view_image" not in {tool.name for tool in session.tools}
            result = await session._before_tool_call(image_tool_context("image.png", session), asyncio.Event())
            assert result and result.block and result.code == "IMAGE_INPUT_UNSUPPORTED"
            session.set_model(DEEPSEEK_MODELS["deepseek-flash"])
            assert "view_image" in {tool.name for tool in session.tools}
        finally:
            await session.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("path,approval", [
    ("image.png", False), ("nested/image.png", False), ("../outside.png", True),
    (".git/image.png", True), (".ssh/image.png", True), (".env/image.png", True),
])
def test_image_path_approval(tmp_path, path, approval) -> None:
    assert requires_approval("ask", "view_image", {"path": path}, tmp_path,
                             is_approval_safe_shell_command) is approval
    assert not requires_approval("full_access", "view_image", {"path": path}, tmp_path,
                                is_approval_safe_shell_command)


def test_plan_cannot_bypass_external_image_gate_with_full_access(tmp_path) -> None:
    async def scenario():
        approvals = []
        async def approve(context, signal):
            approvals.append(context)
        session = make_session(tmp_path, permission_mode="full_access", before_tool_call=approve)
        try:
            session.enter_plan_mode()
            result = await session._before_tool_call(image_tool_context("../outside.png", session), asyncio.Event())
            assert result and result.block and result.code == "PLAN_IMAGE_PATH_BLOCKED"
            assert not approvals
            assert await session._before_tool_call(image_tool_context("inside.png", session), asyncio.Event()) is None
            assert len(approvals) == 1
        finally:
            await session.aclose()
    asyncio.run(scenario())


def test_noninteractive_external_image_requires_explicit_permission(tmp_path) -> None:
    async def scenario():
        session = make_session(tmp_path)
        try:
            result = await session._before_tool_call(image_tool_context("../outside.png", session), asyncio.Event())
            assert result and result.block and result.code == "PERMISSION_APPROVAL_REQUIRED"
        finally:
            await session.aclose()
    asyncio.run(scenario())


def test_invalid_images_fail_before_user_message_is_persisted(tmp_path) -> None:
    async def scenario():
        session = make_session(tmp_path)
        try:
            before = list(session.session_manager.get_branch())
            with pytest.raises(ImageInputError):
                await session.prompt(UserMessage(content=[ImageContent(data="broken", mime_type="image/png")]))
            assert session.session_manager.get_branch() == before
            assert not session.agent.state.messages
            assert not session.is_processing
        finally:
            await session.aclose()
    asyncio.run(scenario())


def test_history_image_and_text_only_model_fail_without_silently_discarding_image(tmp_path) -> None:
    async def scenario():
        session = make_session(tmp_path)
        try:
            previous = UserMessage(content=[image_content()])
            session.agent.state.messages = [previous]
            session.set_model(DEEPSEEK_MODELS["deepseek-v4-pro"])
            with pytest.raises(ImageInputError) as error:
                await session.prompt("Continue")
            assert error.value.code == "IMAGE_INPUT_UNSUPPORTED"
            assert session.agent.state.messages == [previous]
        finally:
            await session.aclose()
    asyncio.run(scenario())


def test_request_gate_is_rechecked_before_each_provider_attempt(tmp_path, monkeypatch) -> None:
    async def scenario():
        session = make_session(tmp_path)
        context = Context(messages=[UserMessage(content="test")])
        calls = []
        def start_provider(model, current_context, options):
            calls.append(current_context)
            current_context.messages.append(UserMessage(content=[ImageContent(data="invalid", mime_type="image/png")]))
            async def fail():
                raise RuntimeError("503 temporarily unavailable")
            return lazy_stream(model, fail)
        monkeypatch.setattr("agent_llm.compat.stream_simple", start_provider)
        try:
            result = await session._create_stream_fn()(session.model, context).result()
            assert result.stop_reason == "error"
            assert len(calls) == 1
            assert "Base64" in (result.error_message or "")
        finally:
            await session.aclose()
    asyncio.run(scenario())


def test_plan_image_tool_returns_pixels_and_preserves_readonly_mode(tmp_path) -> None:
    async def scenario():
        session = make_session(tmp_path)
        image = image_content()
        (tmp_path / "image.png").write_bytes(base64.b64decode(image.data))
        contexts = []
        def model_stream(model, context, options):
            contexts.append(context)
            if len(contexts) == 1:
                return finished_stream(AssistantMessage(content=[ToolCall(
                    id="call-image", name="view_image", arguments={"path": "image.png"},
                )], stop_reason="tool_use"))
            images = [block for message in context.messages if isinstance(message.content, list)
                      for block in message.content if isinstance(block, ImageContent)]
            assert len(images) == 1 and images[0].data == image.data
            return finished_stream(AssistantMessage(content=[TextContent(text="A blue image")]))
        session.agent.stream_fn = model_stream
        before_files = set(path.name for path in Path(tmp_path).iterdir())
        try:
            session.enter_plan_mode()
            await session.prompt("Inspect image.png without changing anything")
            assert len(contexts) == 2
            assert session.collaboration_mode == "plan"
            assert session.plan_state.phase == "drafting"
            assert session.plan_state.latest_revision is None
            assert set(path.name for path in Path(tmp_path).iterdir()) == before_files
        finally:
            await session.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("plan,no_tools", [(False, False), (True, False), (False, True)])
def test_image_grounding_guidance_is_transient_and_respects_tool_availability(tmp_path, monkeypatch, plan, no_tools) -> None:
    async def scenario():
        prompts = []

        def model_stream(_model, context, _options):
            prompts.append(context.system_prompt)
            return finished_stream(AssistantMessage(content=[TextContent(text="A solid blue image.")]))

        # Observe the real per-request wrapper; replacing agent.stream_fn would
        # bypass the transient guidance and authorization being tested.
        monkeypatch.setattr("agent_llm.compat.stream_simple", model_stream)
        session = make_session(tmp_path, web_search_enabled=True, no_tools=no_tools)
        try:
            if plan:
                session.enter_plan_mode()
            await session.prompt(UserMessage(content=[TextContent(text="Which fictional character is this?"), image_content()]))
            assert session.state.messages[-1].stop_reason == "stop"
            assert "<image_identification>" in prompts[0]
            can_search = not no_tools
            assert ("fictional character" in prompts[0]) is can_search
            assert ("public landmark" in prompts[0]) is can_search
            assert "Understand attached images directly" in prompts[0]
            assert ("Do not search merely because an image is present" in prompts[0]) is can_search
            assert "Do not add routine" in prompts[0]
            assert "not simply because no search was made" in prompts[0]
            assert "private text/OCR" in prompts[0]
            assert "Do not identify real people from their faces" in prompts[0]
            assert "Honor explicit requests for description, OCR" in prompts[0]
            assert ("When the user explicitly requests online verification" in prompts[0]) is can_search
            assert ("Cite the actual returned source URLs" in prompts[0]) is can_search
            assert ("Web search is unavailable for this request" in prompts[0]) is (not can_search)
            assert session.state.messages[-1].content == [TextContent(text="A solid blue image.")]
            assert "<image_identification>" not in session.agent.state.system_prompt
            await session.prompt("Continue describing without searching")
            assert session.state.messages[-1].stop_reason == "stop"
            assert len(prompts) == 2
            assert "<image_identification>" not in prompts[1], "A historical image must not force a new image-search task"
            if no_tools:
                assert "web_search" not in {tool.name for tool in session.tools}
        finally:
            await session.aclose()
    asyncio.run(scenario())
