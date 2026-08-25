"""LLM package: provider selection lives here."""

from ..config import Settings
from .base import LLMClient
from .ollama import OllamaClient

__all__ = ["LLMClient", "OllamaClient", "build_client"]


def build_client(settings: Settings) -> LLMClient:
    """Return the brain for the configured provider.

    Phase 4 will add the cloud path (e.g. Anthropic) behind the same interface.
    """
    if settings.llm_provider == "ollama":
        return OllamaClient(
            base_url=settings.ollama_base_url,
            model=settings.llm_model,
            timeout=settings.request_timeout_seconds,
        )
    raise ValueError(f"Unknown llm_provider: {settings.llm_provider!r}")
