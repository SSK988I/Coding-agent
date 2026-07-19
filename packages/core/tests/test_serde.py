"""Round-trip tests for message serialization (serde.py).

Verifies that all Message/ContentBlock variants survive encode -> decode
without losing fields, including opaque provider signatures and Usage details.
"""
from __future__ import annotations

from agent_llm import (
    AssistantMessage,
    ImageContent,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)

from agent_core.session.serde import (
    content_block_to_dict,
    dict_to_content_block,
    dict_to_message,
    dict_to_usage,
    dicts_to_messages,
    message_to_dict,
    messages_to_dicts,
    usage_to_dict,
)


# ─── content blocks ───────────────────────────────────────────────────

def test_text_content_roundtrip():
    b = TextContent(text="hello", text_signature="sig-123")
    d = content_block_to_dict(b)
    assert d == {"type": "text", "text": "hello", "text_signature": "sig-123"}
    assert dict_to_content_block(d) == b


def test_thinking_content_roundtrip_keeps_signature():
    b = ThinkingContent(thinking="reasoning", thinking_signature="opaque", redacted=False)
    d = content_block_to_dict(b)
    assert dict_to_content_block(d) == b
    # signature must survive (providers require it for multi-turn reuse)
    assert dict_to_content_block(d).thinking_signature == "opaque"


def test_image_content_roundtrip():
    b = ImageContent(data="base64==", mime_type="image/png")
    assert dict_to_content_block(content_block_to_dict(b)) == b


def test_toolcall_roundtrip():
    b = ToolCall(id="tc1", name="read", arguments={"path": "/a", "n": 3}, thought_signature="t-sig")
    d = content_block_to_dict(b)
    out = dict_to_content_block(d)
    assert isinstance(out, ToolCall)
    assert out.id == "tc1" and out.name == "read"
    assert out.arguments == {"path": "/a", "n": 3}
    assert out.thought_signature == "t-sig"


def test_unknown_content_block_type_raises():
    try:
        dict_to_content_block({"type": "bogus"})
        assert False, "expected ValueError"
    except ValueError:
        pass


# ─── usage ─────────────────────────────────────────────────────────────

def test_usage_roundtrip():
    u = Usage(
        input=10, output=20, cache_read=5, cache_write=3, cache_write_1h=1,
        reasoning=8, total_tokens=30,
        cost=UsageCost(input=0.1, output=0.2, cache_read=0.05, cache_write=0.03, total=0.38),
    )
    d = usage_to_dict(u)
    out = dict_to_usage(d)
    assert out.input == 10 and out.output == 20 and out.total_tokens == 30
    assert out.cache_read == 5 and out.cache_write_1h == 1
    assert out.reasoning == 8
    assert out.cost.total == 0.38


def test_dict_to_usage_handles_none():
    out = dict_to_usage(None)
    assert out.input == 0 and out.total_tokens == 0


# ─── messages ──────────────────────────────────────────────────────────

def test_user_message_string_content_roundtrip():
    m = UserMessage(content="hi there")
    out = dict_to_message(message_to_dict(m))
    assert isinstance(out, UserMessage)
    assert out.content == "hi there"


def test_user_message_block_content_roundtrip():
    m = UserMessage(content=[TextContent(text="multi"), ImageContent(data="d", mime_type="image/png")])
    out = dict_to_message(message_to_dict(m))
    assert isinstance(out, UserMessage)
    assert isinstance(out.content, list)
    assert out.content[0].text == "multi"
    assert out.content[1].data == "d"


def test_assistant_message_full_roundtrip():
    m = AssistantMessage(
        content=[
            ThinkingContent(thinking="think", thinking_signature="s1"),
            TextContent(text="answer"),
            ToolCall(id="c1", name="read", arguments={"path": "/x"}),
        ],
        api="openai-completions",
        provider="deepseek",
        model="deepseek-v4-flash",
        response_model="deepseek-v4-flash-actual",
        response_id="resp-1",
        usage=Usage(input=1, output=2, total_tokens=3),
        stop_reason="tool_use",
        error_message=None,
    )
    d = message_to_dict(m)
    out = dict_to_message(d)
    assert isinstance(out, AssistantMessage)
    assert out.provider == "deepseek" and out.model == "deepseek-v4-flash"
    assert out.response_id == "resp-1" and out.stop_reason == "tool_use"
    assert out.usage.total_tokens == 3
    # content block order and types preserved
    assert isinstance(out.content[0], ThinkingContent)
    assert out.content[0].thinking_signature == "s1"
    assert isinstance(out.content[1], TextContent)
    assert isinstance(out.content[2], ToolCall)
    assert out.content[2].arguments == {"path": "/x"}


def test_tool_result_message_roundtrip():
    m = ToolResultMessage(
        tool_call_id="c1", tool_name="read",
        content=[TextContent(text="file body")],
        details={"lines": 10}, is_error=False,
    )
    out = dict_to_message(message_to_dict(m))
    assert isinstance(out, ToolResultMessage)
    assert out.tool_call_id == "c1" and out.tool_name == "read"
    assert out.content[0].text == "file body"
    assert out.details == {"lines": 10}
    assert out.is_error is False


# ─── list round-trip ──────────────────────────────────────────────────

def test_messages_list_roundtrip_preserves_order_and_types():
    msgs = [
        UserMessage(content="hello"),
        AssistantMessage(content=[TextContent(text="hi")]),
        ToolResultMessage(tool_call_id="c1", tool_name="bash", content=[TextContent(text="done")]),
        UserMessage(content="next"),
    ]
    out = dicts_to_messages(messages_to_dicts(msgs))
    assert len(out) == 4
    assert out[0].role == "user" and out[1].role == "assistant"
    assert out[2].role == "toolResult" and out[3].role == "user"


# ─── JSON safety (the dicts must be json.dumps-able) ───────────────────

def test_message_dicts_are_json_serializable():
    import json
    m = AssistantMessage(
        content=[ToolCall(id="c", name="write", arguments={"path": "/a", "data": "x"})],
        usage=Usage(input=1, total_tokens=1, cost=UsageCost(total=0.01)),
    )
    line = json.dumps(message_to_dict(m))
    assert json.loads(line)["role"] == "assistant"
