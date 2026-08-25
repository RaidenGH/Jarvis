"""LLM client abstraction.

The same agent loop must work against a local model (Ollama) today and a
cloud provider (Phase 4) later, so everything talks to this interface.
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator

from ..session import Message


class LLMClient(ABC):
    @abstractmethod
    def stream_chat(self, messages: list[Message]) -> AsyncIterator[str]:
        """Yield assistant reply tokens as they are generated."""
        raise NotImplementedError
