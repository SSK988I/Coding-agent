"""Locate original edit calls and their durable results within one branch."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from agent_llm import AssistantMessage, ToolCall, ToolResultMessage
from agent_core.session.types import SessionEntry, SessionMessageEntry


@dataclass
class SessionEdit:
    entry_id: str
    assistant: AssistantMessage
    call: ToolCall
    saved: ToolResultMessage | None = None

    @property
    def unresolved(self) -> bool:
        return self.saved is None or self.saved.status == "uncertain"


def branch_edits(entries: list[SessionEntry]) -> list[SessionEdit]:
    """A provider call ID is scoped to one assistant entry, not to the whole chat."""
    edits: list[SessionEdit] = []
    pending: dict[str, SessionEdit] = {}
    for entry in entries:
        if not isinstance(entry, SessionMessageEntry) or entry.message is None:
            continue
        message = entry.message
        if isinstance(message, ToolResultMessage):
            operation = pending.pop(message.tool_call_id, None)
            if operation is not None and message.tool_name == "edit":
                operation.saved = message
            continue
        pending.clear()
        if isinstance(message, AssistantMessage):
            calls = [block for block in message.content if isinstance(block, ToolCall)]
            counts = Counter(call.id for call in calls)
            for call in calls:
                if call.name == "edit" and call.id and counts[call.id] == 1:
                    operation = SessionEdit(entry.id, message, call)
                    edits.append(operation)
                    pending[call.id] = operation
    return edits
