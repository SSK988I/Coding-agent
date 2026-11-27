"""Bind edit recovery to the original assistant entry on the active branch."""
from __future__ import annotations

from agent_core.session.edit_calls import SessionEdit
from agent_core.types import AgentToolResult
from agent_llm import TextContent, ToolCall, ToolResultMessage
from agent_llm.types import ToolResultStatus


def recovery_error(code: str, text: str, *, status: ToolResultStatus = "failed") -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text=text)], details={"code": code}, status=status)


def as_message(operation: SessionEdit, result: AgentToolResult) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=operation.call.id, tool_name="edit", content=result.content,
        details={**(result.details or {}), "assistantEntryId": operation.entry_id},
        status=result.status, is_error=result.status != "completed",
        timestamp=operation.assistant.timestamp,
    )


def project_results(messages: list, replacements: dict[int, dict[str, ToolResultMessage]]) -> list:
    """Put synthetic results beside their source assistant without changing durable JSONL."""
    if not replacements:
        return messages
    projected = []
    index = 0
    while index < len(messages):
        source = messages[index]
        projected.append(source)
        index += 1
        recovered = replacements.get(id(source))
        if not recovered:
            continue
        available = {}
        while index < len(messages) and isinstance(messages[index], ToolResultMessage):
            item = messages[index]
            available[item.tool_call_id] = item
            index += 1
        available.update(recovered)
        for call in source.content:
            if isinstance(call, ToolCall) and call.id in available:
                projected.append(available.pop(call.id))
        projected.extend(available.values())
    return projected
