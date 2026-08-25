"""Ollama client using its OpenAI-compatible endpoint (localhost:11434/v1).

Kept deliberately thin: plain httpx + SSE parsing, no SDK dependency, so
swapping in a cloud provider later is a config change plus one new class.
"""

import json
from typing import AsyncIterator

import httpx

from ..session import Message
from .base import LLMClient


class OllamaClient(LLMClient):
    def __init__(self, base_url: str, model: str, timeout: float = 120.0):
        self._url = base_url.rstrip("/") + "/v1/chat/completions"
        self._model = model
        self._timeout = timeout

    async def stream_chat(self, messages: list[Message]) -> AsyncIterator[str]:
        payload = {"model": self._model, "messages": messages, "stream": True}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream("POST", self._url, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[len("data: ") :]
                    if data.strip() == "[DONE]":
                        break
                    delta = json.loads(data)["choices"][0].get("delta", {})
                    token = delta.get("content")
                    if token:
                        yield token
