"""Agent loop (Phase 2.5): transcribe intent → answer or call tools → respond.

One loop for every text turn — typed chat, REST, and voice all flow through
here, so the brain/tool behavior is identical no matter the front-end.

Tool specs use the OpenAI function-calling shape (Ollama's /v1 speaks it
natively; an Anthropic translation layer can consume it in Phase 4). The
reply is non-streaming: tool calls must be read as a unit, so we trade token
streaming for a correct loop and yield the final answer as one token event.
"""

import json
from typing import AsyncIterator

from .llm.base import LLMClient, LLMReply
from .session import SYSTEM_PROMPT, Message, SessionStore
from .tools import execute_tool, tool_specs

MAX_TOOL_ROUNDS = 5

LIMIT_REACHED = "I couldn't finish that — hit the tool-call limit."


async def run_turn(
    llm: LLMClient,
    sessions: SessionStore,
    session_id: str,
    text: str,
) -> AsyncIterator[dict]:
    """Run one user turn, yielding WS frames: tool_call / tool_result / token.

    Raises RuntimeError on provider failure (same contract the old
    _stream_llm had). History is persisted only when the turn completes, so
    a failed turn leaves no half-finished assistant message behind.
    """
    sessions.add(session_id, "user", text)
    messages: list[Message] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *sessions.history(session_id),
    ]
    specs = tool_specs()

    for _round in range(MAX_TOOL_ROUNDS):
        try:
            reply: LLMReply = await llm.complete(messages, tools=specs)
        except Exception as exc:  # noqa: BLE001 - surface a clean error to clients
            raise RuntimeError(str(exc)) from exc

        if not reply.tool_calls:
            sessions.add(session_id, "assistant", reply.content)
            yield {"type": "token", "text": reply.content}
            return

        messages.append(_assistant_call_message(reply))
        for call in reply.tool_calls:
            yield {
                "type": "tool_call",
                "id": call.id,
                "name": call.name,
                "arguments": call.arguments,
            }
            result = execute_tool(call.name, call.arguments)
            yield {"type": "tool_result", "name": call.name, "result": result}
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id or "",
                    "name": call.name,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )

    sessions.add(session_id, "assistant", LIMIT_REACHED)
    yield {"type": "token", "text": LIMIT_REACHED}


def _assistant_call_message(reply: LLMReply) -> Message:
    """OpenAI-shaped assistant turn that owns the tool_calls (needed for the
    tool results that follow to be accepted by the provider)."""
    return {
        "role": "assistant",
        "content": reply.content or "",
        "tool_calls": [
            {
                "id": call.id or "",
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments, ensure_ascii=False, default=str
                    ),
                },
            }
            for call in reply.tool_calls
        ],
    }
