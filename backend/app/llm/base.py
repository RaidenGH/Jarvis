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


@dataclass
class LLMChunk:
    """One piece of a streamed turn.

    Most chunks carry `text` (or `reasoning`, for models that narrate their
    thinking). Tool requests arrive assembled on the final chunk: their
    arguments stream in as JSON fragments that mean nothing until the last
    one lands, so they are never handed over half-parsed.
    """

    text: str = ""
    reasoning: str = ""
    calls: list[ToolCall] = field(default_factory=list)


class LLMClient(ABC):
    @abstractmethod
    def stream_chat(self, messages: list[Message]) -> AsyncIterator[str]:
        """Yield assistant reply tokens as they are generated."""
        raise NotImplementedError

    async def stream_complete(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> AsyncIterator[LLMChunk]:
        """Stream one turn as chunks, so a client can print as it arrives.

        The default buffers `complete()` into a single chunk — correct for
        stubs and for brains with no streaming of their own. Providers that
        can stream override this.
        """
        reply = await self.complete(messages, tools)
        yield LLMChunk(text=reply.content, calls=reply.tool_calls)

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
