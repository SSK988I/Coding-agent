"""Unknown provider usage must not look like complete cumulative accounting."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from agent_llm import AssistantMessage, Context, Model, ModelCost, TextContent, Usage, UserMessage
from agent_llm.api.openai_completions import stream

from coding_agent.core.usage_metrics import project_usage_metrics


def _model():
    return Model(
        id="compatible-test", provider="compatible-test", api="openai-completions",
        base_url="https://offline.invalid/v1", context_window=10_000, cost=ModelCost(0, 0, 0, 0),
    )


def _known_response():
    return AssistantMessage(
        model="compatible-test", provider="compatible-test", content=[TextContent(text="known")],
        usage=Usage(input=100, cache_read=900, output=20, cache_read_reported=True),
    )


def _metrics(messages):
    return project_usage_metrics(
        messages=messages, entries=[SimpleNamespace(type="message", message=message) for message in messages],
        model=_model(), system_prompt="", tools=[],
    )


def test_compatible_stream_without_usage_invalidates_cumulative_cache_rate(monkeypatch):
    monkeypatch.delenv("CODING_AGENT_STREAM_DEBUG", raising=False)
    requests, clients = [], []
    initialize = httpx.AsyncClient.__init__

    def response(request):
        assert request.method == "POST" and request.url.host == "offline.invalid"
        requests.append(json.loads(request.content))
        chunk = {
            "id": "local-completion", "object": "chat.completion.chunk", "created": 0,
            "model": "compatible-test",
            "choices": [{"index": 0, "delta": {"content": "answer"}, "finish_reason": "stop"}],
        }
        if len(requests) == 1:
            chunk["usage"] = {
                "prompt_tokens": 1000, "completion_tokens": 20, "total_tokens": 1020,
                "prompt_tokens_details": {"cached_tokens": 900},
            }
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
        )

    def initialize_with_transport(client, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(response)
        kwargs["trust_env"] = False
        initialize(client, *args, **kwargs)
        clients.append(client)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", initialize_with_transport)

    async def scenario():
        model = _model()
        context = Context(messages=[UserMessage(content="question")])
        try:
            first_stream = stream(model, context, {"api_key": "local-test-key"})
            first = await first_stream.result()
            await first_stream.aclose()
            assert first.stop_reason == "stop", first.error_message
            assert first.usage.cache_read_reported is True
            assert _metrics([first]).cache_hit_rate == 90
            assert requests[0]["stream_options"] == {"include_usage": True}

            model.compat = {"supports_usage_in_streaming": False}
            second_stream = stream(model, context, {"api_key": "local-test-key"})
            second = await second_stream.result()
            await second_stream.aclose()
            assert second.stop_reason == "stop", second.error_message
            assert second.content == [TextContent(text="answer")]
            assert second.usage == Usage()
            assert "stream_options" not in requests[1]

            metrics = _metrics([first, second])
            assert metrics.input_tokens == 1000  # Known usage remains useful as a partial count.
            assert metrics.cache_hit_rate is None
        finally:
            await asyncio.gather(*(client.aclose() for client in clients))

    asyncio.run(scenario())


@pytest.mark.parametrize("stop", ["stop", "length", "tool_use"])
@pytest.mark.parametrize("reported", [None, False])
def test_successful_zero_or_absent_usage_is_incomplete(stop, reported):
    second = AssistantMessage(stop_reason=stop, usage=Usage(cache_read_reported=reported))
    assert _metrics([_known_response(), second]).cache_hit_rate is None
    absent = SimpleNamespace(role="assistant", content=[], stop_reason=stop, usage=None)
    assert _metrics([_known_response(), absent]).cache_hit_rate is None


@pytest.mark.parametrize("stop", ["error", "aborted"])
@pytest.mark.parametrize("reported", [None, False])
def test_unsuccessful_unmetered_attempt_does_not_discard_known_cache_rate(stop, reported):
    second = AssistantMessage(stop_reason=stop, usage=Usage(cache_read_reported=reported))
    assert _metrics([_known_response(), second]).cache_hit_rate == 90
    absent = SimpleNamespace(role="assistant", content=[], stop_reason=stop, usage=None)
    assert _metrics([_known_response(), absent]).cache_hit_rate == 90


def test_explicit_zero_cache_and_zero_input_remain_distinct_from_missing_usage():
    measured_zero = AssistantMessage(usage=Usage(input=1000, cache_read_reported=True))
    assert _metrics([measured_zero]).cache_hit_rate == 0
    assert _metrics([_known_response(), measured_zero]).cache_hit_rate == 45
    zero_input = AssistantMessage(usage=Usage(cache_read_reported=True))
    assert _metrics([_known_response(), zero_input]).cache_hit_rate == 90


def test_legacy_positive_cache_counts_remain_valid_but_partial_failures_are_not_complete():
    legacy = AssistantMessage(usage=Usage(input=100, cache_read=900))
    assert _metrics([legacy]).cache_hit_rate == 90
    partial_failure = AssistantMessage(stop_reason="error", usage=Usage(input=100, cache_read_reported=False))
    assert _metrics([_known_response(), partial_failure]).cache_hit_rate is None
