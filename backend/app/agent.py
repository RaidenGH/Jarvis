"""Agent loop (Phase 2.5, safety-gated in Phase 3): decide → maybe confirm → act.

One loop for every text turn — typed chat, REST, and voice all flow through
here, so the brain/tool behavior is identical no matter the front-end.

Phase 3 inserts the permission step between "the model asked for a tool" and
"the tool runs". A tool's *risk tier* maps to a `RiskPolicy` (plan §2.5), and
anything above read-only has to be approved by the human first:

    tool_call  →  [confirm_request]  →  tool_result   (approved)
                                     →  tool_denied   (declined / disabled)

Approval is delegated to a `confirm` callback rather than handled inline, so
the loop stays transport-agnostic: the WebSocket endpoint injects a callback
that round-trips to the client, while REST `/chat` gets the default that
declines everything (there is no human on that channel to ask).

Typed confirmations are verified *here*, not by the client: the callback only
reports what the human typed, and the loop decides whether it matches. A
client that just sends `approved: true` can't satisfy a typed tier.
"""

import json
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable

from .audit import AuditLog
from .llm.base import LLMClient, LLMReply, ToolCall
from .session import SYSTEM_PROMPT, Message, SessionStore
from .tools import Tool, execute_tool, get_tool, policy_for, tool_specs
from .tools.base import CONFIRM_NONE, CONFIRM_TYPED

MAX_TOOL_ROUNDS = 5

LIMIT_REACHED = "I couldn't finish that — hit the tool-call limit."


@dataclass
class ConfirmationDecision:
    """The human's answer to one `confirm_request`.

    `challenge` is only meaningful for typed tiers: it is whatever the human
    retyped, and the loop compares it to the phrase it issued.
    """

    approved: bool = False
    challenge: str | None = None


ConfirmFn = Callable[[dict], Awaitable[ConfirmationDecision]]


async def _decline_all(_request: dict) -> ConfirmationDecision:
    """Default gate: no human to ask, so nothing above read-only ever runs."""
    return ConfirmationDecision(approved=False)


def _challenge_for(name: str) -> str:
    """Phrase a human must retype for a typed tier — the tool's own name."""
    return name


def _approved(request: dict, decision: ConfirmationDecision | None) -> bool:
    if decision is None or not decision.approved:
        return False
    if request["mode"] == CONFIRM_TYPED:
        typed = (decision.challenge or "").strip()
        return typed == request["challenge"]
    return True


def _confirmation_request(call: ToolCall, tool: Tool) -> dict:
    policy = policy_for(tool.risk)
    return {
        "type": "confirm_request",
        "id": call.id,
        "name": call.name,
        "arguments": call.arguments,
        "risk": policy.tier,
        "mode": policy.confirmation,
        "challenge": (
            _challenge_for(call.name) if policy.confirmation == CONFIRM_TYPED else None
        ),
    }


def _tool_message(call: ToolCall, result: dict) -> Message:
    return {
        "role": "tool",
        "tool_call_id": call.id or "",
        "name": call.name,
        "content": json.dumps(result, ensure_ascii=False, default=str),
    }


def _audit(
    audit: AuditLog | None,
    brain: str,
    session_id: str,
    call: ToolCall,
    *,
    decision: str,
    risk: str,
    result: dict,
) -> None:
    """Record one tool call's outcome: what was asked, by which brain, and
    whether it ran (plan §2.5's audit log)."""
    if audit is None:
        return
    audit.record(
        session_id=session_id,
        brain=brain,
        tool=call.name,
        arguments=call.arguments,
        risk=risk,
        decision=decision,
        result=result,
    )


async def run_turn(
    llm: LLMClient,
    sessions: SessionStore,
    session_id: str,
    text: str,
    confirm: ConfirmFn | None = None,
    audit: AuditLog | None = None,
    brain: str = "",
) -> AsyncIterator[dict]:
    """Run one user turn, yielding WS frames: tool_call / tool_result / token.

    Gated tools also produce `confirm_request` (asks the human) and
    `tool_denied` (the action did not run). Every call is written to `audit`,
    including the ones that never ran. Raises RuntimeError on provider failure
    (same contract the old _stream_llm had). History is persisted only when the
    turn completes, so a failed turn leaves no half-finished assistant message
    behind.
    """
    confirm = confirm or _decline_all
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

            tool = get_tool(call.name)
            denial = await _gate(tool, call, confirm)
            if denial is not None:
                _audit(
                    audit,
                    brain,
                    session_id,
                    call,
                    decision=denial["decision"],
                    risk=denial["risk"],
                    result={"error": denial["reason"]},
                )
                yield {
                    "type": "tool_denied",
                    "id": call.id,
                    "name": call.name,
                    **denial,
                }
                messages.append(_tool_message(call, {"error": denial["reason"]}))
                continue

            result = execute_tool(call.name, call.arguments)
            _audit(
                audit,
                brain,
                session_id,
                call,
                decision="ran",
                risk=tool.risk if tool else "unknown",
                result=result,
            )
            yield {"type": "tool_result", "name": call.name, "result": result}
            messages.append(_tool_message(call, result))

    sessions.add(session_id, "assistant", LIMIT_REACHED)
    yield {"type": "token", "text": LIMIT_REACHED}


async def _gate(tool: Tool | None, call: ToolCall, confirm: ConfirmFn) -> dict | None:
    """Decide whether `call` may run.

    Returns None to let it through, or a `tool_denied` payload explaining why
    it didn't. Unknown tools are *not* gated — they have no tier to check and
    `execute_tool` already reports them as an error, which keeps the model's
    recovery path for a hallucinated name exactly as it was.
    """
    if tool is None:
        return None

    policy = policy_for(tool.risk)
    if not policy.enabled:
        return {
            "decision": "disabled",
            "risk": policy.tier,
            "reason": f"{call.name} is disabled: {policy.reason}",
        }

    if policy.confirmation == CONFIRM_NONE:
        return None

    request = _confirmation_request(call, tool)
    decision = await confirm(request)
    if _approved(request, decision):
        return None
    return {
        "decision": "declined",
        "risk": policy.tier,
        "mode": policy.confirmation,
        "reason": f"the user did not confirm {call.name}",
    }


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
