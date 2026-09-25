"""LLM package: provider selection lives here."""

from ..config import Settings
from .base import LLMClient
from .ollama import OllamaClient

__all__ = ["LLMClient", "OllamaClient", "build_client"]


def build_client(settings: Settings, model: str | None = None) -> LLMClient:
    """Return the brain for the configured provider.

    `model` overrides the configured one for this client — it is how a client
    asks for a bigger brain on one run (the CLI's `--model`) without the
    backend's configuration changing under anyone else.

    Phase 4 will add the cloud path (e.g. Anthropic) behind the same interface.
    """
    if settings.llm_provider == "ollama":
        return OllamaClient(
            base_url=settings.ollama_base_url,
            model=model or settings.llm_model,
            timeout=settings.request_timeout_seconds,
        )
    raise ValueError(f"Unknown llm_provider: {settings.llm_provider!r}")
