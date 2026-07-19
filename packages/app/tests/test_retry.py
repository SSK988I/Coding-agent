from __future__ import annotations

import asyncio

from agent_llm import AssistantMessage, TextContent
from agent_llm.event_stream import AssistantMessageEventStream

from coding_agent.core.retry import RetryPolicy, is_transient_error, retrying_stream


def _failed_stream(message: str) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()
    partial = AssistantMessage(content=[])
    error = AssistantMessage(stop_reason="error", error_message=message)
    stream.push({"type": "start", "partial": partial})
    stream.push({"type": "error", "reason": "error", "error": error})
    stream.end(error)
    return stream


def _successful_stream() -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()
    partial = AssistantMessage(content=[])
    final = AssistantMessage(content=[TextContent(text="ok")], stop_reason="stop")
    stream.push({"type": "start", "partial": partial})
    stream.push({"type": "done", "reason": "stop", "message": final})
    stream.end(final)
    return stream


def test_transient_error_classification_is_conservative():
    assert is_transient_error("HTTP 429 rate limit")
    assert is_transient_error("Connection reset by peer")
    assert not is_transient_error("401 invalid API key")
    assert not is_transient_error("400 invalid request")


def test_retrying_stream_retries_pre_content_transient_failure():
    async def scenario():
        attempts = 0
        retry_events = []

        def factory():
            nonlocal attempts
            attempts += 1
            return _failed_stream("HTTP 503 service unavailable") if attempts == 1 else _successful_stream()

        async def no_sleep(_delay: float):
            return None

        stream = retrying_stream(
            factory,
            RetryPolicy(max_retries=2, initial_delay=0),
            on_retry=lambda *args: retry_events.append(args),
            sleep=no_sleep,
        )
        events = [event async for event in stream]
        final = await stream.result()
        return attempts, retry_events, events, final

    attempts, retry_events, events, final = asyncio.run(scenario())
    assert attempts == 2
    assert len(retry_events) == 1
    assert [event["type"] for event in events] == ["start", "done"]
    assert final.stop_reason == "stop"


def test_retrying_stream_does_not_retry_auth_error():
    async def scenario():
        attempts = 0

        def factory():
            nonlocal attempts
            attempts += 1
            return _failed_stream("401 invalid API key")

        stream = retrying_stream(factory, RetryPolicy(max_retries=3, initial_delay=0))
        events = [event async for event in stream]
        return attempts, events, await stream.result()

    attempts, events, final = asyncio.run(scenario())
    assert attempts == 1
    assert [event["type"] for event in events] == ["start", "error"]
    assert final.stop_reason == "error"
