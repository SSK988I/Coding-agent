from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import json
import sys
from types import SimpleNamespace

import pytest

from agent_llm import (
    AssistantMessage,
    Context,
    ImageContent,
    TextContent,
    ThinkingContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from agent_llm.api.deepseek_responses import build_params, convert_input, stream
from agent_llm.providers.deepseek_models import DEEPSEEK_MODELS


def test_build_params_exposes_native_search_and_responses_function_tools() -> None:
    model = DEEPSEEK_MODELS["deepseek-v4-pro"]
    context = Context(
        system_prompt="Be useful",
        messages=[UserMessage(content="latest Python release")],
        tools=[Tool(
            name="read",
            description="Read a file",
            parameters={"type": "object", "properties": {}},
        )],
    )
    params = build_params(model, context, {"api_key": "x", "web_search": True})

    assert params["model"] == "deepseek-v4-pro"
    assert params["instructions"] == "Be useful"
    assert params["reasoning"] == {"effort": "none"}
    assert params["tools"][0] == {
        "type": "function",
        "name": "read",
        "description": "Read a file",
        "parameters": {"type": "object", "properties": {}},
        "strict": False,
    }
    assert params["tools"][1] == {"type": "web_search"}


def test_flash_never_sends_ignored_native_search_and_preserves_function_search() -> None:
    params = build_params(DEEPSEEK_MODELS["deepseek-flash"], Context(
        tools=[Tool(name="web_search", description="Search", parameters={"type": "object"})],
    ), {"web_search": True})
    assert len(params["tools"]) == 1
    assert params["tools"][0]["type"] == "function"
    assert params["tools"][0]["name"] == "web_search"


@pytest.mark.parametrize(
    ("level", "effort"),
    [(None, "none"), ("minimal", "low"), ("low", "low"),
     ("medium", "high"), ("high", "high"), ("xhigh", "max")],
)
def test_v4_1_flash_requests_use_supported_reasoning_levels(level, effort) -> None:
    params = build_params(DEEPSEEK_MODELS["deepseek-flash"], Context(), {"reasoning": level})
    assert params["reasoning"] == {"effort": effort}


def test_v4_1_flash_image_input_uses_responses_content_parts() -> None:
    model = DEEPSEEK_MODELS["deepseek-flash"]
    assert "image" in model.input
    params = build_params(model, Context(messages=[UserMessage(content=[
        TextContent(text="Describe the image"),
        ImageContent(data="aW1hZ2U=", mime_type="image/png"),
    ])]))
    assert params["input"] == [{"role": "user", "content": [
        {"type": "input_text", "text": "Describe the image"},
        {"type": "input_image", "detail": "auto", "image_url": "data:image/png;base64,aW1hZ2U="},
    ]}]
    assert params["max_output_tokens"] == 393_216


def test_convert_input_replays_provider_items_and_function_results() -> None:
    assistant = AssistantMessage(
        content=[
            ThinkingContent(thinking="checked", thinking_signature="reason-1"),
            TextContent(text="I need the file"),
            ToolCall(id="call-1", name="read", arguments={"path": "README.md"}),
        ],
        provider_data={
            "response_items": [{
                "type": "reasoning",
                "id": "reason-1",
                "summary": [],
                "content": [{"type": "reasoning_text", "text": "checked"}],
                "status": "completed",
            }],
        },
    )
    items = convert_input(Context(messages=[
        UserMessage(content="read it"),
        assistant,
        ToolResultMessage(
            tool_call_id="call-1",
            tool_name="read",
            content=[TextContent(text="body")],
        ),
    ]))

    assert items[1]["type"] == "reasoning"
    assert items[2] == {"role": "assistant", "content": "I need the file"}
    assert items[3]["type"] == "function_call"
    assert items[4] == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": "body",
    }


@pytest.mark.parametrize("include_text", [False, True])
def test_tool_images_are_preserved_as_responses_content_parts(include_text: bool) -> None:
    content: list[TextContent | ImageContent] = [TextContent(text="local screenshot")] if include_text else []
    content.append(ImageContent(data="aW1hZ2U=", mime_type="image/png"))
    items = convert_input(Context(messages=[ToolResultMessage(
        tool_call_id="view-1", tool_name="view_image", content=content,
    )]))
    output = []
    if include_text:
        output.append({"type": "input_text", "text": "local screenshot"})
    output.append({
        "type": "input_image", "detail": "auto", "image_url": "data:image/png;base64,aW1hZ2U=",
    })
    assert items == [{"type": "function_call_output", "call_id": "view-1", "output": output}]


def test_empty_tool_result_keeps_legacy_text_fallback() -> None:
    items = convert_input(Context(messages=[ToolResultMessage(
        tool_call_id="empty-1", tool_name="read", content=[],
    )]))
    assert items == [{"type": "function_call_output", "call_id": "empty-1", "output": "(no output)"}]


def test_image_request_budget_checks_the_complete_provider_payload(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(
        "agent_llm.api.deepseek_responses.validate_image_request_size", captured.append,
    )
    params = build_params(DEEPSEEK_MODELS["deepseek-flash"], Context(
        system_prompt="system instructions",
        messages=[ToolResultMessage(
            tool_call_id="image-1", tool_name="view_image",
            content=[ImageContent(data="aW1hZ2U=", mime_type="image/png")],
        )],
        tools=[Tool(name="view_image", description="View", parameters={"type": "object"})],
    ), {"reasoning": "high", "web_search": True})
    assert captured == [params]
    assert captured[0]["instructions"] == "system instructions"
    assert captured[0]["reasoning"] == {"effort": "high"}
    assert captured[0]["tools"][-1]["name"] == "view_image"
    assert not any(tool["type"] == "web_search" for tool in captured[0]["tools"])


def test_image_request_budget_rejection_propagates_before_sending(monkeypatch) -> None:
    def reject(_params):
        raise ValueError("Image request too large")

    monkeypatch.setattr("agent_llm.api.deepseek_responses.validate_image_request_size", reject)
    with pytest.raises(ValueError, match="Image request too large"):
        build_params(DEEPSEEK_MODELS["deepseek-flash"], Context(messages=[UserMessage(content=[
            ImageContent(data="aW1hZ2U=", mime_type="image/png"),
        ])]))


class _Dumpable(SimpleNamespace):
    def model_dump(self, *, exclude_none: bool = False):
        del exclude_none
        return dict(self.__dict__)


class _AsyncEvents:
    def __init__(self, events):
        self._events = iter(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def test_stream_maps_text_citations_usage_and_native_search_history(monkeypatch) -> None:
    annotation = SimpleNamespace(
        type="url_citation",
        title="Python Releases",
        url="https://python.org/downloads/",
    )
    message_item = SimpleNamespace(
        type="message",
        id="msg-1",
        content=[SimpleNamespace(
            type="output_text",
            text="Python 3.14 is current.",
            annotations=[annotation],
        )],
    )
    search_item = _Dumpable(
        type="web_search_call",
        id="ws-1",
        status="completed",
        action={"type": "search", "query": "latest Python release"},
    )
    usage = SimpleNamespace(
        input_tokens=12,
        input_tokens_details=SimpleNamespace(cached_tokens=2),
        output_tokens=8,
        output_tokens_details=SimpleNamespace(reasoning_tokens=1),
        total_tokens=20,
    )
    response = SimpleNamespace(
        id="resp-1",
        model="deepseek-v4-pro",
        usage=usage,
        output=[search_item, message_item],
    )
    events = _AsyncEvents([
        SimpleNamespace(
            type="response.output_text.delta",
            output_index=1,
            content_index=0,
            item_id="msg-1",
            delta="Python 3.14 is current.",
        ),
        SimpleNamespace(type="response.completed", response=response),
    ])
    captured = {}

    class FakeResponses:
        async def create(self, **params):
            captured.update(params)
            return events

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.responses = FakeResponses()

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=FakeAsyncOpenAI))

    async def scenario():
        event_stream = stream(
            DEEPSEEK_MODELS["deepseek-v4-pro"],
            Context(messages=[UserMessage(content="what is current?")]),
            {"api_key": "secret", "web_search": True},
        )
        seen = [event async for event in event_stream]
        return seen, await event_stream.result()

    seen, result = asyncio.run(scenario())
    assert seen[-1]["type"] == "done"
    assert isinstance(result.content[0], TextContent)
    assert "[Python Releases](https://python.org/downloads/)" in result.content[0].text
    assert result.response_id == "resp-1"
    assert result.usage.input == 10
    assert result.usage.cache_read == 2
    assert result.usage.cost.total == pytest.approx((10 * 1.32 + 2 * 0.044 + 8 * 3.96) / 1_000_000)
    assert result.provider_data["response_items"][0]["type"] == "web_search_call"
    assert captured["tools"] == [{"type": "web_search"}]
    assert captured["model"] == "deepseek-v4-pro"
    assert captured["client"]["max_retries"] == 0


def test_real_sdk_serializes_tool_image_pixels_in_responses_request(monkeypatch) -> None:
    """Exercise SDK JSON serialization, not just the provider conversion helper."""
    import httpx
    import openai
    from PIL import Image

    image_buffer = BytesIO()
    Image.new("RGB", (3, 2), "blue").save(image_buffer, format="PNG")
    image_data = base64.b64encode(image_buffer.getvalue()).decode("ascii")
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        event = {"type": "response.completed", "response": {
            "id": "response-vision", "object": "response", "created_at": 0,
            "model": "deepseek-flash", "status": "completed", "output": [{
                "type": "message", "id": "message-vision", "role": "assistant",
                "status": "completed", "content": [{
                    "type": "output_text", "text": "A blue rectangle.", "annotations": [],
                }],
            }], "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
        }}
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(event)}\n\n".encode(),
        )

    real_client = openai.AsyncOpenAI

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
            monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kwargs: real_client(
                **kwargs, http_client=http_client,
            ))
            event_stream = stream(DEEPSEEK_MODELS["deepseek-flash"], Context(messages=[
                UserMessage(content="View image.png"),
                AssistantMessage(content=[ToolCall(
                    id="view-1", name="view_image", arguments={"path": "image.png"},
                )]),
                ToolResultMessage(tool_call_id="view-1", tool_name="view_image", content=[
                    TextContent(text="Image: image.png"),
                    ImageContent(data=image_data, mime_type="image/png"),
                ]),
            ]), {"api_key": "mock-key"})
            events = [event async for event in event_stream]
            return events, await event_stream.result()

    events, result = asyncio.run(scenario())
    assert events[-1]["type"] == "done"
    assert isinstance(result.content[0], TextContent)
    assert result.content[0].text == "A blue rectangle."
    assert len(requests) == 1
    assert requests[0]["input"][-1] == {
        "type": "function_call_output", "call_id": "view-1", "output": [
            {"type": "input_text", "text": "Image: image.png"},
            {"type": "input_image", "detail": "auto", "image_url": f"data:image/png;base64,{image_data}"},
        ],
    }
