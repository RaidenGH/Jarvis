"""Ollama client using its OpenAI-compatible endpoint (localhost:11434/v1).

Kept deliberately thin: plain httpx + SSE parsing, no SDK dependency, so
swapping in a cloud provider later is a config change plus one new class.
"""

import json
from typing import Any, AsyncIterator

import httpx

from ..session import Message
from .base import LLMChunk, LLMClient, LLMReply, ToolCall


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

    async def stream_complete(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> AsyncIterator[LLMChunk]:
        """Stream one turn, including tool calls.

        Ollama's /v1 endpoint streams OpenAI-shaped deltas: `content` (and
        `reasoning` for thinking models like qwen3) arrive as text fragments,
        while `tool_calls` arrive as fragments keyed by index that only make
        sense once the argument JSON is complete. So text is yielded the moment
        it exists and the calls are yielded assembled at the end.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        pending: dict[int, dict] = {}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream("POST", self._url, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[len("data: ") :]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0].get("delta") or {}
                    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                        continue  # tolerate a malformed frame mid-stream

                    reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                    if reasoning:
                        yield LLMChunk(reasoning=reasoning)
                    text = delta.get("content")
                    if text:
                        yield LLMChunk(text=text)
                    for raw in delta.get("tool_calls") or []:
                        _accumulate(pending, raw)

        yield LLMChunk(calls=_assemble(pending))

    async def complete(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> LLMReply:
        """One non-streaming turn, with tool calling when tools are offered.

        Ollama's /v1 endpoint accepts the OpenAI `tools` shape and returns
        `tool_calls` on the message — assembled here into an LLMReply.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(self._url, json=payload)
            resp.raise_for_status()
            message = resp.json()["choices"][0]["message"]

        calls = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            calls.append(
                ToolCall(
                    name=str(fn.get("name", "")),
                    arguments=_parse_arguments(fn.get("arguments")),
                    id=raw.get("id"),
                )
            )
        return LLMReply(content=message.get("content") or "", tool_calls=calls)


def _accumulate(pending: dict[int, dict], raw: Any) -> None:
    """Fold one streamed tool-call fragment into the pending call for its slot."""
    if not isinstance(raw, dict):
        return
    try:
        index = int(raw.get("index") or 0)
    except (TypeError, ValueError):
        index = 0
    slot = pending.setdefault(index, {"id": None, "name": "", "arguments": ""})
    if raw.get("id"):
        slot["id"] = raw["id"]
    function = raw.get("function") or {}
    if isinstance(function.get("name"), str):
        slot["name"] += function["name"]
    if isinstance(function.get("arguments"), str):
        slot["arguments"] += function["arguments"]


def _assemble(pending: dict[int, dict]) -> list[ToolCall]:
    """Turn accumulated fragments into tool calls, in the order they started."""
    calls = []
    for index in sorted(pending):
        slot = pending[index]
        name = slot["name"].strip()
        if not name:
            continue  # a fragment without a name is noise
        calls.append(
            ToolCall(
                name=name,
                arguments=_parse_arguments(slot["arguments"]),
                id=slot["id"],
            )
        )
    return calls


def _parse_arguments(raw: Any) -> dict:
    """Ollama/OpenAI send arguments as a JSON string; tolerate dicts too."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
