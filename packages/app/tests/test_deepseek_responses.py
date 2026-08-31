from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

from agent_llm import (
    AssistantMessage,
    Context,
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
    model = DEEPSEEK_MODELS["deepseek-v4-flash"]
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
        model="deepseek-v4-flash",
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
            DEEPSEEK_MODELS["deepseek-v4-flash"],
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
    assert result.provider_data["response_items"][0]["type"] == "web_search_call"
    assert captured["tools"] == [{"type": "web_search"}]
