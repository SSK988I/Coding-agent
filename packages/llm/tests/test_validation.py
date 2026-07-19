"""Tests for JSON-schema tool argument validation and coercion."""
from __future__ import annotations

from agent_llm import Tool, ToolCall, validate_tool_arguments


def _tool(parameters: dict) -> Tool:
    return Tool(name="demo", description="test tool", parameters=parameters)


def test_numeric_strings_are_coerced_recursively_without_mutating_call():
    tool = _tool({
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"score": {"type": "number"}},
                },
            },
        },
        "required": ["count", "items"],
    })
    call = ToolCall(
        id="call-1",
        name="demo",
        arguments={"count": "3", "items": [{"score": "2.5"}]},
    )

    result = validate_tool_arguments(tool, call)

    assert result == {"count": 3, "items": [{"score": 2.5}]}
    assert call.arguments == {"count": "3", "items": [{"score": "2.5"}]}


def test_invalid_numeric_string_still_fails_validation():
    tool = _tool({
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
    })
    call = ToolCall(id="call-2", name="demo", arguments={"count": "3.5"})

    try:
        validate_tool_arguments(tool, call)
    except ValueError as exc:
        assert "期望 integer" in str(exc)
    else:
        raise AssertionError("expected invalid integer string to fail")
