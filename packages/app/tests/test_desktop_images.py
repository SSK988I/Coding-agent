"""Image attachments cross the real Runtime/session persistence boundary."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import random

import pytest
from agent_core import AgentToolResult, SessionManager
from agent_core.session.types import PlanRunEntry
from agent_core.tools.view_image import ViewImageTool
from agent_llm import (
    AssistantMessage, AssistantMessageEventStream, ImageContent, Model, ModelCost,
    TextContent, ToolCall, UserMessage,
)
from agent_llm.providers.deepseek_models import DEEPSEEK_MODELS
from PIL import Image

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.plan_mode import PlanQuestion, reduce_plan_state
from coding_agent.desktop.attachments import parse_image_attachments
from coding_agent.desktop.protocol import RpcError
from coding_agent.desktop.runtime import DesktopRuntime


def png_attachment(size=(2, 2), *, noisy=False):
    image = (
        Image.frombytes("RGB", size, random.Random(1).randbytes(size[0] * size[1] * 3))
        if noisy else Image.new("RGB", size, "white")
    )
    output = io.BytesIO()
    image.save(output, format="PNG")
    return {"data": base64.b64encode(output.getvalue()).decode("ascii"), "mime_type": "image/png"}


def make_runtime(tmp_path, *, input_modalities=None, tools=None, question_behavior="interactive", model=None):
    events = []
    runtime = DesktopRuntime(events.append)
    manager = SessionManager.create(cwd=str(tmp_path), sessions_dir=tmp_path / "sessions")
    session = AgentSession(AgentSessionConfig(
        cwd=str(tmp_path), session_manager=manager, tools=tools or [], question_behavior=question_behavior,
        model=model or Model(
            id="vision-test", provider="test", input=input_modalities or ["text", "image"],
            cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
        ),
    ))
    runtime._workspace = tmp_path
    runtime._session = session
    runtime._unsubscribe = session.on_event(runtime._on_session_event)
    return runtime, session, events


def successful_stream(*_):
    stream = AssistantMessageEventStream()
    stream.push({"type": "done", "reason": "stop", "message": AssistantMessage(content=[TextContent(text="seen")])})
    return stream


@pytest.mark.parametrize("text", ["What is in this image?", "", "  "])
def test_image_prompt_events_and_persistence_are_lossless(tmp_path, text):
    async def scenario():
        runtime, session, events = make_runtime(tmp_path)
        attachment = png_attachment()
        captured = []

        def stream(model, context, options):
            captured.append(context)
            return successful_stream()

        session.agent.stream_fn = stream
        try:
            result = await runtime.dispatch("run.start", {"text": text, "images": [attachment]})
            assert result["accepted"]
            await asyncio.wait_for(runtime._run_task, 5)
            assert len(captured) == 1
            user = captured[0].messages[0]
            assert isinstance(user, UserMessage)
            assert isinstance(user.content, list)
            assert user.content[-1] == ImageContent(**attachment)
            if text.strip():
                assert user.content[0] == TextContent(text=text)
            else:
                assert len(user.content) == 1
            started = next(event for event in events if event["event"]["type"] == "run.started")
            assert started["runId"] == result["runId"]
            assert started["event"]["payload"] == {"text": text, "images": [attachment]}
            json.dumps(started)  # The wire never receives dataclass objects.
            assert events[-1]["event"]["type"] == "run.completed"

            before = await runtime.dispatch("session.snapshot", {})
            assert before["messages"][0]["content"][-1] == {"type": "image", **attachment}
            path = session.session_manager.path
            assert path is not None and path.exists()
            restored = SessionManager.open(path).build_session_context()
            assert restored.messages[0] == session.state.messages[0]
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("entrypoint", ["execute", "answer"])
@pytest.mark.parametrize("outcome", [
    "completed", "lost_append", "flush_failed", "cancelled", "cancelled_lost_append", "failed", "failed_lost_append",
])
def test_plan_image_results_are_durable_before_completion(tmp_path, monkeypatch, entrypoint, outcome):
    import agent_core.session.session_manager as persistence

    async def scenario():
        image = tmp_path / "image.png"
        image.write_bytes(base64.b64decode(png_attachment()["data"]))
        runtime, session, events = make_runtime(
            tmp_path, tools=[ViewImageTool(str(tmp_path))], question_behavior="deferred",
            model=DEEPSEEK_MODELS["deepseek-flash"],
        )
        session.enter_plan_mode()
        setup_call = ToolCall(
            id="setup", name="submit_plan" if entrypoint == "execute" else "request_user_input",
            arguments=(
                {"title": "Inspect image", "markdown": "Inspect image.png."}
                if entrypoint == "execute" else {"questions": [{
                    "header": "Scope", "question": "Which image?", "options": [
                        {"label": "Local", "description": "Inspect local image"},
                        {"label": "Other", "description": "Inspect another image"},
                    ],
                }]}
            ),
        )

        def response(message):
            stream = AssistantMessageEventStream()
            stream.push({"type": "done", "reason": message.stop_reason, "message": message})
            return stream

        session.agent.stream_fn = lambda *_: response(AssistantMessage(content=[setup_call], stop_reason="tool_use"))
        await runtime.dispatch("run.start", {"text": "Plan an image inspection"})
        await asyncio.wait_for(runtime._run_task, 5)
        assert events[-1]["event"]["type"] == "run.completed"
        events.clear()
        calls = []

        def stream(*_):
            calls.append(True)
            if len(calls) == 1:
                return response(AssistantMessage(content=[ToolCall(
                    id="view", name="view_image", arguments={"path": "image.png"},
                )], stop_reason="tool_use"))
            if outcome.startswith("cancelled"):
                return response(AssistantMessage(content=[], stop_reason="aborted"))
            if outcome.startswith("failed"):
                return response(AssistantMessage(content=[], stop_reason="error", error_message="provider unavailable"))
            return successful_stream()

        append = persistence.append_entry_line
        flush = session.session_manager.flush
        rejected = []
        flush_errors = []
        lose_image = "lost_append" in outcome

        def reject_image(path, entry):
            message = getattr(entry, "message", None)
            if lose_image and not rejected and getattr(message, "role", None) == "toolResult" and any(
                isinstance(block, ImageContent) for block in message.content
            ):
                rejected.append(entry.id)
                raise OSError("image append interrupted")
            return append(path, entry)

        def fail_image_flush_once():
            if outcome == "flush_failed" and not flush_errors and any(
                isinstance(message.content, list)
                and any(isinstance(block, ImageContent) for block in message.content)
                for message in session.state.messages
            ):
                flush_errors.append(True)
                raise OSError("image flush interrupted")
            return flush()

        session.agent.stream_fn = stream
        monkeypatch.setattr(persistence, "append_entry_line", reject_image)
        monkeypatch.setattr(session.session_manager, "flush", fail_image_flush_once)
        try:
            if entrypoint == "execute":
                plan = session.plan_state.latest_revision
                params = {"planId": plan.plan_id, "revision": plan.revision, "digest": plan.digest}
            else:
                params = {"questionId": session.plan_state.pending_question.question_id, "answer": "Local"}
            accepted = await runtime.dispatch(f"plan.{entrypoint}", params)
            await asyncio.wait_for(runtime._run_task, 5)
            assert len(rejected) == int(lose_image)
            assert len(flush_errors) == int(outcome == "flush_failed")
            assert len(calls) == 2
            session.session_manager.flush()  # Later successful writes cannot restore the removed entry.
            reopened = SessionManager.open(session.session_manager.path)
            assert any(
                getattr(entry, "message", None) is not None
                and isinstance(entry.message.content, list)
                and any(isinstance(block, ImageContent) for block in entry.message.content)
                for entry in reopened.entries
            ) is (not lose_image)
            expected = "completed" if outcome == "completed" else "cancelled" if outcome.startswith("cancelled") else "failed"
            assert events[-1]["event"]["type"] == f"run.{expected}"
            if expected == "failed" and outcome != "failed":
                assert events[-1]["event"]["payload"]["code"] == "IMAGE_PERSISTENCE_FAILED"
            assert events[-1]["runId"] == accepted["runId"]
            assert any(event["event"]["type"] == "run.completed" for event in events) is (expected == "completed")
            assert runtime._run_task is None and runtime._run_id is None
            if entrypoint == "execute":
                runs = [entry for entry in reopened.entries if isinstance(entry, PlanRunEntry)]
                status = "aborted" if expected == "cancelled" else expected
                assert [entry.status for entry in runs] == ["started", status]
                assert all(
                    (entry.plan_id, entry.revision, entry.digest, entry.run_id)
                    == (plan.plan_id, plan.revision, plan.digest, accepted["runId"])
                    for entry in runs
                )
                phase = "settled" if expected == "completed" else status
                assert session.plan_state.phase == phase
                assert reduce_plan_state(reopened.get_branch()).phase == phase

            monkeypatch.setattr(persistence, "append_entry_line", append)
            monkeypatch.setattr(session.session_manager, "flush", lambda: (_ for _ in ()).throw(
                OSError("a text-only next run must not flush old image state"),
            ))
            session.agent.stream_fn = successful_stream
            await runtime.dispatch("run.start", {"text": "Next text-only request"})
            await asyncio.wait_for(runtime._run_task, 5)
            assert events[-1]["event"]["type"] == "run.completed"
        finally:
            monkeypatch.setattr(persistence, "append_entry_line", append)
            monkeypatch.setattr(session.session_manager, "flush", flush)
            await runtime.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("raw", [None, "image.png", {}, True, 1, [None], ["image.png"], [{"path": "secret.png"}],
    [{"data": 123, "mime_type": "image/png"}], [{"data": "AA==", "mime_type": True}],
    [{"data": "!!!!", "mime_type": "image/png"}], [{"data": "data:image/png;base64,AA==", "mime_type": "image/png"}],
    [{"data": "AA==", "mime_type": "image/png"}],
])
def test_images_reject_untrusted_payload_shapes_and_content(tmp_path, raw):
    async def scenario():
        runtime, _session, events = make_runtime(tmp_path)
        try:
            with pytest.raises(RpcError) as error:
                await runtime.dispatch("run.start", {"text": "look", "images": raw})
            assert error.value.code == "INVALID_IMAGES"
            assert runtime._run_task is None and runtime._run_id is None
            assert not events
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_images_reject_mime_mismatch_and_image_limits():
    attachment = png_attachment()
    for raw in (
        [{**attachment, "mime_type": "image/jpeg"}],
        [{**attachment, "mime_type": "text/plain"}],
        [attachment] * 5,
        [png_attachment((8193, 1))],
        [png_attachment((4097, 4096))],
        [png_attachment((1400, 1400), noisy=True)],
    ):
        with pytest.raises(RpcError) as error:
            parse_image_attachments(raw)
        assert error.value.code == "INVALID_IMAGES"


def test_images_enforce_twelve_mib_aggregate_limit():
    attachment = png_attachment((1200, 1200), noisy=True)
    assert 4 * 1024 * 1024 < len(base64.b64decode(attachment["data"])) < 5 * 1024 * 1024
    with pytest.raises(RpcError, match="12 MiB") as error:
        parse_image_attachments([attachment] * 3)
    assert error.value.code == "INVALID_IMAGES"


def test_non_visual_model_rejects_attachments_before_start(tmp_path):
    async def scenario():
        runtime, session, events = make_runtime(tmp_path, input_modalities=["text"])
        session.agent.stream_fn = lambda *_: pytest.fail("Image must never reach a text-only provider")
        try:
            with pytest.raises(RpcError) as error:
                await runtime.dispatch("run.start", {"text": "look", "images": [png_attachment()]})
            assert error.value.code == "IMAGE_INPUT_UNSUPPORTED"
            assert runtime._run_task is None and not events
            assert not session.state.messages
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("text_only", [False, True])
def test_history_image_budget_and_model_gate_reject_before_run_started(tmp_path, text_only):
    async def scenario():
        runtime, session, events = make_runtime(tmp_path)
        image = ImageContent(**png_attachment())
        session.agent.state.messages.append(UserMessage(content=[image] * (1 if text_only else 24)))
        if text_only:
            session.model.input = ["text"]
        before = list(session.state.messages)
        try:
            with pytest.raises(RpcError) as error:
                await runtime.dispatch("run.start", {
                    "text": "next", "images": [] if text_only else [png_attachment()],
                })
            assert error.value.code == ("IMAGE_INPUT_UNSUPPORTED" if text_only else "IMAGE_REQUEST_TOO_LARGE")
            assert runtime._run_task is None and runtime._run_id is None
            assert session.state.messages == before
            assert not events
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["uncertain", "recovery_error", "awaiting_answer"])
def test_image_messages_cannot_bypass_plan_state_gates(tmp_path, phase):
    async def scenario():
        runtime, session, events = make_runtime(tmp_path)
        session._plan_state.mode = "plan"
        session._plan_state.phase = phase
        if phase == "awaiting_answer":
            session._plan_state.pending_question = PlanQuestion("question", "Header", "Choose", ())
        session.agent.stream_fn = lambda *_: pytest.fail("Plan gate must reject before provider startup")
        try:
            await runtime.dispatch("run.start", {"text": "look", "images": [png_attachment()]})
            await asyncio.wait_for(runtime._run_task, 5)
            assert events[-1]["event"]["type"] == "run.failed"
            assert not session.state.messages
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_image_messages_cannot_send_slash_commands_or_start_over_active_run(tmp_path):
    async def scenario():
        runtime, _session, events = make_runtime(tmp_path)
        try:
            with pytest.raises(RpcError) as slash:
                await runtime.dispatch("run.start", {"text": "/new", "images": [png_attachment()]})
            assert slash.value.code == "SLASH_COMMAND"
            runtime._run_task = asyncio.create_task(asyncio.Event().wait())
            with pytest.raises(RpcError) as active:
                await runtime.dispatch("run.start", {"text": "look", "images": [png_attachment()]})
            assert active.value.code == "RUN_IN_PROGRESS"
            assert not events
        finally:
            if runtime._run_task is not None:
                task = runtime._run_task
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                runtime._run_task = None
            await runtime.dispose()
    asyncio.run(scenario())


def test_text_only_runs_remain_compatible_and_blank_payload_is_rejected(tmp_path):
    async def scenario():
        runtime, session, events = make_runtime(tmp_path, input_modalities=["text"])
        session.agent.stream_fn = successful_stream
        try:
            for invalid in ({"text": ""}, {"text": "  ", "images": []}, {"images": [png_attachment()]}, {"text": False}):
                with pytest.raises(RpcError) as error:
                    await runtime.dispatch("run.start", invalid)
                assert error.value.code == "INVALID_PARAMS"
            await runtime.dispatch("run.start", {"text": "hello"})
            await asyncio.wait_for(runtime._run_task, 5)
            assert session.state.messages[0].content == "hello"
            assert events[0]["event"] == {"type": "run.started", "payload": {"text": "hello", "images": []}}
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_model_capabilities_are_authoritative_in_rpc_and_events(tmp_path, monkeypatch):
    async def scenario():
        runtime, session, events = make_runtime(tmp_path)
        visual = session.model
        text_only = Model(
            id="text-test", provider="test", input=["text"],
            cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
        )
        monkeypatch.setattr(runtime, "_available_models", lambda: [visual, text_only])
        try:
            listed = await runtime.dispatch("model.list", {})
            assert listed[0]["input"] == ["text", "image"]
            assert listed[1]["input"] == ["text"]
            selected = await runtime.dispatch("model.select", {"modelId": "text-test", "provider": "test"})
            assert selected["input"] == ["text"]
            assert events[-1]["event"] == {"type": "model.changed", "payload": selected}
            assert runtime._workspace_payload()["model"]["input"] == ["text"]
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
def test_image_run_failure_or_cancellation_does_not_leak_into_next_run(tmp_path, monkeypatch, outcome):
    async def scenario():
        runtime, session, events = make_runtime(tmp_path)
        seen = []

        async def interrupted(message):
            seen.append(message)
            if outcome == "cancelled":
                raise asyncio.CancelledError
            raise RuntimeError("provider unavailable")

        monkeypatch.setattr(session, "prompt", interrupted)
        try:
            await runtime.dispatch("run.start", {"text": "look", "images": [png_attachment()]})
            await asyncio.wait_for(runtime._run_task, 5)
            assert events[-1]["event"]["type"] == f"run.{outcome}"
            assert runtime._run_task is None and runtime._run_id is None
            assert isinstance(seen[0], UserMessage)
            await runtime.dispatch("run.start", {"text": "next"})
            await asyncio.wait_for(runtime._run_task, 5)
            assert seen[-1] == "next"
            started = [event for event in events if event["event"]["type"] == "run.started"]
            assert started[-1]["event"]["payload"]["images"] == []
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_image_persistence_failure_never_reports_completed_and_can_be_flushed_after_recovery(tmp_path, monkeypatch):
    import agent_core.session.session_manager as persistence

    async def scenario():
        runtime, session, events = make_runtime(tmp_path)
        session.agent.stream_fn = successful_stream
        attachment = png_attachment()
        append = persistence.append_entry_line

        def disk_full(*_):
            raise OSError("disk full")

        monkeypatch.setattr(persistence, "append_entry_line", disk_full)
        try:
            result = await runtime.dispatch("run.start", {"text": "look", "images": [attachment]})
            await asyncio.wait_for(runtime._run_task, 5)
            assert events[-1]["event"]["type"] == "run.failed"
            assert events[-1]["event"]["payload"]["code"] == "IMAGE_PERSISTENCE_FAILED"
            assert "保存" in events[-1]["event"]["payload"]["message"]
            assert "disk full" in events[-1]["event"]["payload"]["message"]
            assert not any(event["event"]["type"] == "run.completed" for event in events)
            assert runtime._run_task is None and runtime._run_id is None
            assert events[-1]["runId"] == result["runId"]
            assert session.state.messages[0].content[-1] == ImageContent(**attachment)
            path = session.session_manager.path
            assert path is not None and path.exists()
            assert len(path.read_text(encoding="utf-8").splitlines()) == 1

            monkeypatch.setattr(persistence, "append_entry_line", append)
            session.session_manager.flush()
            restored = SessionManager.open(path).build_session_context()
            assert restored.messages[0].content[-1] == ImageContent(**attachment)
            saved = path.read_text(encoding="utf-8")
            session.session_manager.flush()
            assert path.read_text(encoding="utf-8") == saved  # No duplicate images when retried.
        finally:
            monkeypatch.setattr(persistence, "append_entry_line", append)
            await runtime.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("with_image", [False, True])
def test_only_image_runs_force_flush_before_completion(tmp_path, monkeypatch, with_image):
    async def scenario():
        runtime, session, events = make_runtime(tmp_path)
        session.agent.stream_fn = successful_stream
        flush = session.session_manager.flush
        flushed = []

        def record_flush():
            assert not any(event["event"]["type"] == "run.completed" for event in events)
            flush()
            flushed.append(True)

        monkeypatch.setattr(session.session_manager, "flush", record_flush)
        try:
            await runtime.dispatch("run.start", {
                "text": "look", "images": [png_attachment()] if with_image else [],
            })
            await asyncio.wait_for(runtime._run_task, 5)
            assert len(flushed) == int(with_image)
            assert events[-1]["event"]["type"] == "run.completed"
        finally:
            monkeypatch.setattr(session.session_manager, "flush", flush)
            await runtime.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("source", ["user", "toolResult"])
@pytest.mark.parametrize("reject_image", [False, True])
def test_image_entry_consistency_even_when_later_appends_and_flush_succeed(tmp_path, monkeypatch, source, reject_image):
    import agent_core.session.session_manager as persistence

    async def scenario():
        attachment = png_attachment()
        append = persistence.append_entry_line
        rejected, model_calls = [], []

        class ImageTool:
            name = "image"
            label = "Image"
            description = "Return a test image"
            parameters = {"type": "object", "properties": {}}
            effect = "read"
            plan_access = "observe"

            async def execute(self, *_):
                return AgentToolResult(content=[ImageContent(**attachment)])

        runtime, session, events = make_runtime(tmp_path, tools=[ImageTool()] if source == "toolResult" else [])
        previous = UserMessage(content="Previous saved message")
        session.agent.state.messages.append(previous)
        session.session_manager.append_message(previous)
        session.session_manager.flush()

        def stream(*_):
            model_calls.append(True)
            if source == "toolResult" and len(model_calls) == 1:
                result = AssistantMessageEventStream()
                result.push({"type": "done", "reason": "tool_use", "message": AssistantMessage(
                    content=[ToolCall(id="image-call", name="image", arguments={})], stop_reason="tool_use",
                )})
                return result
            return successful_stream()

        def fail_only_image(path, entry):
            message = getattr(entry, "message", None)
            content = getattr(message, "content", None)
            if (
                reject_image and getattr(message, "role", None) == source
                and isinstance(content, list)
                and any(isinstance(block, ImageContent) for block in content)
            ):
                rejected.append(entry.id)
                raise OSError("disk full while writing image")
            return append(path, entry)

        session.agent.stream_fn = stream
        monkeypatch.setattr(persistence, "append_entry_line", fail_only_image)
        try:
            await runtime.dispatch("run.start", {
                "text": "look", "images": [attachment] if source == "user" else [],
            })
            await asyncio.wait_for(runtime._run_task, 5)
            assert len(rejected) == int(reject_image)
            assert len(model_calls) == (1 if source == "user" else 2)  # Never resend the model request.
            assert events[-1]["event"]["type"] == ("run.failed" if reject_image else "run.completed")
            if reject_image:
                assert events[-1]["event"]["payload"]["code"] == "IMAGE_PERSISTENCE_FAILED"
                assert not any(event["event"]["type"] == "run.completed" for event in events)
            assert runtime._run_task is None and runtime._run_id is None
            session.session_manager.flush()  # No pending write error remains to detect.
            path = session.session_manager.path
            assert path is not None
            persisted = SessionManager.open(path).build_session_context().messages
            assert persisted[0].content == previous.content
            assert any(
                isinstance(message.content, list)
                and any(isinstance(block, ImageContent) for block in message.content)
                for message in persisted
            ) is (not reject_image)
        finally:
            monkeypatch.setattr(persistence, "append_entry_line", append)
            await runtime.dispose()
    asyncio.run(scenario())
