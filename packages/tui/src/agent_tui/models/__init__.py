"""Chat Model abstractions.

Provides the base chat model interface and a mock implementation
for testing and development.
"""

from agent_tui.models.base import BaseChatModel, Message, Role, StreamEvent
from agent_tui.models.mock import MockChatModel

__all__ = ["BaseChatModel", "Message", "Role", "StreamEvent", "MockChatModel"]
