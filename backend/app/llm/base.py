"""LLM client abstraction.

The same agent loop must work against a local model (Ollama) today and a
cloud provider (Phase 4) later, so everything talks to this interface.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator

from ..session import Message


@dataclass
class ToolCall:
    """One tool invocation requested by the model."""

    name: str
    arguments: dict = field(default_factory=dict)
    id: str | None = None


@dataclass
class LLMReply:
    """A full (non-streaming) model turn: prose, tool requests, or both."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClient(ABC):
    @abstractmethod
    def stream_chat(self, messages: list[Message]) -> AsyncIterator[str]:
        """Yield assistant reply tokens as they are generated."""
        raise NotImplementedError

    async def complete(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> LLMReply:
        """Return one full turn, including any tool calls the model requests.

        The default implementation collects `stream_chat` and ignores `tools`
        — good enough for stubs and for brains without tool support. Providers
        that can call tools override this. (The agent loop prefers this over
        streaming because tool calls must be read as a unit, not as fragments.)
        """
        parts: list[str] = []
        async for token in self.stream_chat(messages):
            parts.append(token)
        return LLMReply(content="".join(parts))
