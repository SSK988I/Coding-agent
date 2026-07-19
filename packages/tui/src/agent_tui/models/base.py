"""Base chat model interfaces.

Defines the abstract interface for chat models and message types.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Literal, Optional


class Role(str, Enum):
    """Message role in a conversation."""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass
class Message:
    """A single message in a conversation.

    Matches the message format used by OpenAI/Anthropic APIs.
    """
    role: Literal["system", "user", "assistant"]
    content: str
    timestamp: float = field(default_factory=lambda: __import__("time").time())

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str) -> "Message":
        return cls(role="assistant", content=content)

    @classmethod
    def system(cls, content: str) -> "Message":
        return cls(role="system", content=content)


@dataclass
class StreamEvent:
    """A single event in a streaming response.

    Streaming events use these variants:
    - text_delta: incremental text content
    - done: stream completed, with final message
    - error: stream failed
    """
    type: Literal["text_delta", "text_end", "done", "error"]
    content: str = ""
    error: Optional[str] = None


class BaseChatModel(ABC):
    """Abstract interface for chat models.

    Implementations can wrap real LLM APIs (OpenAI, Anthropic, local models)
    or provide mock/fallback behavior.

    Providers implement asynchronous streaming and optional cleanup.
    """

    @abstractmethod
    async def generate(self, messages: list[Message]) -> str:
        """Generate a complete response for the given conversation.

        Args:
            messages: The conversation history.

        Returns:
            The model's complete response text.
        """
        ...

    @abstractmethod
    async def generate_stream(
        self, messages: list[Message]
    ) -> AsyncIterator[StreamEvent]:
        """Generate a streaming response.

        Args:
            messages: The conversation history.

        Yields:
            StreamEvent objects as content is generated.
        """
        ...

    async def close(self) -> None:
        """Clean up any resources (connections, sessions, etc.)."""
        pass
