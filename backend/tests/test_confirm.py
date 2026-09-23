"""Phase 3 permission tests: risk tiers, confirmation, and denial paths.

The gate is exercised with fake tools registered at each tier, so nothing here
depends on a device-control tool existing yet.
"""

import contextlib

import pytest
from fastapi.testclient import TestClient

from app.agent import ConfirmationDecision, run_turn
from app.config import Settings
from app.llm.base import LLMClient, LLMReply, ToolCall
from app.main import app, state
from app.session import SessionStore
from app.tools import TOOLS, Tool, _BY_NAME
from app.tools.base import (
    DESTRUCTIVE,
    EXTERNAL_FACING,
    READ_ONLY,
    REVERSIBLE_WRITE,
    policy_for,
)


def _tool(name: str, risk: str, ran: list | None = None) -> Tool:
    def handler(_args: dict) -> dict:
        if ran is not None:
            ran.append(name)
        return {"ran": name}

    return Tool(
        name=name,
        description=f"test tool {name}",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=handler,
        risk=risk,
    )


@contextlib.contextmanager
def registered(tool: Tool):
    """Temporarily add a tool to the allow-list the way a real one is added."""
    TOOLS.append(tool)
    _BY_NAME[tool.name] = tool
    try:
        yield tool
    finally:
        TOOLS.remove(tool)
        _BY_NAME.pop(tool.name, None)


class OneToolLLM(LLMClient):
    """Asks for `name` on the first round, then answers."""

    def __init__(self, name: str, arguments: dict | None = None):
        self.name = name
        self.arguments = arguments or {}
        self.calls = 0
        self.final_messages: list = []

    async def stream_chat(self, messages):  # pragma: no cover - complete() overridden
        yield ""

    async def complete(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return LLMReply(
                tool_calls=[ToolCall(name=self.name, arguments=self.arguments, id="c1")]
            )
        self.final_messages = messages
        return LLMReply(content="done")


async def drain(gen) -> list[dict]:
    return [event async for event in gen]


def kinds(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


def _tool_error(messages: list) -> str:
    """The `error` the model was handed back for a denied tool call."""
    return [m for m in messages if m["role"] == "tool"][-1]["content"]


# --- policy table ---------------------------------------------------------


def test_unknown_risk_tier_falls_back_to_strictest():
    assert policy_for("something-new").tier == DESTRUCTIVE
    assert policy_for("something-new").confirmation == "typed"


def test_read_only_needs_no_confirmation():
    assert policy_for(READ_ONLY).confirmation == "none"
    assert policy_for(REVERSIBLE_WRITE).confirmation == "tap"
    assert policy_for(DESTRUCTIVE).confirmation == "typed"


def test_external_facing_is_disabled_by_default():
    assert policy_for(EXTERNAL_FACING).enabled is False
    assert policy_for(EXTERNAL_FACING).reason


# --- the gate -------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_only_tool_runs_without_asking():
    asked: list[dict] = []

    async def confirm(request: dict) -> ConfirmationDecision:
        asked.append(request)
        return ConfirmationDecision(approved=True)

    llm = OneToolLLM("system_stats")
    events = await drain(run_turn(llm, SessionStore(), "s1", "stats?", confirm=confirm))

    assert kinds(events) == ["tool_call", "tool_result", "token"]
    assert asked == []
    assert "cpu" in events[1]["result"]


@pytest.mark.asyncio
async def test_reversible_write_asks_then_runs_when_approved():
    asked: list[dict] = []
    ran: list[str] = []

    async def confirm(request: dict) -> ConfirmationDecision:
        asked.append(request)
        return ConfirmationDecision(approved=True)

    with registered(_tool("set_volume", REVERSIBLE_WRITE, ran)):
        llm = OneToolLLM("set_volume", {"level": 30})
        events = await drain(
            run_turn(llm, SessionStore(), "s2", "louder", confirm=confirm)
        )

    assert kinds(events) == ["tool_call", "tool_result", "token"]
    assert ran == ["set_volume"]
    assert events[1]["result"] == {"ran": "set_volume"}
    assert asked[0]["mode"] == "tap"
    assert asked[0]["risk"] == REVERSIBLE_WRITE
    assert asked[0]["challenge"] is None
    assert asked[0]["arguments"] == {"level": 30}


@pytest.mark.asyncio
async def test_denied_tool_does_not_run_and_model_is_told():
    ran: list[str] = []

    async def confirm(_request: dict) -> ConfirmationDecision:
        return ConfirmationDecision(approved=False)

    with registered(_tool("set_volume", REVERSIBLE_WRITE, ran)):
        llm = OneToolLLM("set_volume")
        events = await drain(
            run_turn(llm, SessionStore(), "s3", "louder", confirm=confirm)
        )

    assert kinds(events) == ["tool_call", "tool_denied", "token"]
    assert ran == []  # the handler never ran
    assert events[1]["name"] == "set_volume"
    assert "did not confirm" in events[1]["reason"]
    assert "did not confirm" in _tool_error(llm.final_messages)


@pytest.mark.asyncio
async def test_default_gate_declines_gated_tools():
    """No callback (REST /chat) means no human to ask, so nothing runs."""
    ran: list[str] = []
    with registered(_tool("set_volume", REVERSIBLE_WRITE, ran)):
        llm = OneToolLLM("set_volume")
        events = await drain(run_turn(llm, SessionStore(), "s4", "louder"))

    assert kinds(events) == ["tool_call", "tool_denied", "token"]
    assert ran == []


@pytest.mark.asyncio
async def test_typed_tier_rejects_a_wrong_challenge():
    ran: list[str] = []

    async def confirm(_request: dict) -> ConfirmationDecision:
        # Approves, but retypes the wrong phrase — must still be denied.
        return ConfirmationDecision(approved=True, challenge="yes do it")

    with registered(_tool("wipe_folder", DESTRUCTIVE, ran)):
        llm = OneToolLLM("wipe_folder")
        events = await drain(
            run_turn(llm, SessionStore(), "s5", "wipe it", confirm=confirm)
        )

    assert kinds(events) == ["tool_call", "tool_denied", "token"]
    assert ran == []


@pytest.mark.asyncio
async def test_typed_tier_accepts_the_exact_challenge():
    ran: list[str] = []
    asked: list[dict] = []

    async def confirm(request: dict) -> ConfirmationDecision:
        asked.append(request)
        return ConfirmationDecision(approved=True, challenge=request["challenge"])

    with registered(_tool("wipe_folder", DESTRUCTIVE, ran)):
        llm = OneToolLLM("wipe_folder")
        events = await drain(
            run_turn(llm, SessionStore(), "s6", "wipe it", confirm=confirm)
        )

    assert kinds(events) == ["tool_call", "tool_result", "token"]
    assert ran == ["wipe_folder"]
    assert asked[0]["mode"] == "typed"
    assert asked[0]["challenge"] == "wipe_folder"


@pytest.mark.asyncio
async def test_external_facing_tier_never_even_asks():
    asked: list[dict] = []
    ran: list[str] = []

    async def confirm(request: dict) -> ConfirmationDecision:
        asked.append(request)
        return ConfirmationDecision(approved=True, challenge=request.get("challenge"))

    with registered(_tool("send_email", EXTERNAL_FACING, ran)):
        llm = OneToolLLM("send_email")
        events = await drain(
            run_turn(llm, SessionStore(), "s7", "email bob", confirm=confirm)
        )

    assert kinds(events) == ["tool_call", "tool_denied", "token"]
    assert asked == [] and ran == []
    assert "disabled" in events[1]["reason"]


@pytest.mark.asyncio
async def test_unknown_tool_is_not_gated():
    """The hallucinated-name path stays a plain tool error, un-gated."""
    asked: list[dict] = []

    async def confirm(request: dict) -> ConfirmationDecision:
        asked.append(request)
        return ConfirmationDecision(approved=False)

    llm = OneToolLLM("launch_missiles")
    events = await drain(run_turn(llm, SessionStore(), "s8", "fire", confirm=confirm))

    assert kinds(events) == ["tool_call", "tool_result", "token"]
    assert asked == []
    assert "unknown tool" in events[1]["result"]["error"]


# --- over the wire --------------------------------------------------------


@pytest.fixture
def client():
    state.settings = Settings()
    state.sessions = SessionStore()
    with TestClient(app) as c:
        state.llm = OneToolLLM("system_stats")
        state.speech = None
        yield c


def _collect_until(ws, until="done"):
    events = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event["type"] == until:
            return events


def test_ws_confirmation_roundtrip_approves_and_runs(client):
    ran: list[str] = []
    with registered(_tool("set_volume", REVERSIBLE_WRITE, ran)):
        state.llm = OneToolLLM("set_volume", {"level": 10})
        with client.websocket_connect("/ws/c1") as ws:
            ws.send_json({"type": "user_message", "text": "turn it up"})
            assert ws.receive_json()["type"] == "tool_call"
            ask = ws.receive_json()
            assert ask["type"] == "confirm_request"
            assert ask["mode"] == "tap"
            ws.send_json({"type": "confirm_response", "approved": True})
            rest = _collect_until(ws)

    assert kinds(rest) == ["tool_result", "token", "done"]
    assert ran == ["set_volume"]


def test_ws_confirmation_denial_reports_tool_denied(client):
    ran: list[str] = []
    with registered(_tool("set_volume", REVERSIBLE_WRITE, ran)):
        state.llm = OneToolLLM("set_volume")
        with client.websocket_connect("/ws/c2") as ws:
            ws.send_json({"type": "user_message", "text": "turn it up"})
            assert ws.receive_json()["type"] == "tool_call"
            assert ws.receive_json()["type"] == "confirm_request"
            ws.send_json({"type": "confirm_response", "approved": False})
            rest = _collect_until(ws)

    assert kinds(rest) == ["tool_denied", "token", "done"]
    assert ran == []


def test_ws_typed_tier_requires_the_echoed_challenge(client):
    ran: list[str] = []
    with registered(_tool("wipe_folder", DESTRUCTIVE, ran)):
        state.llm = OneToolLLM("wipe_folder")
        with client.websocket_connect("/ws/c3") as ws:
            ws.send_json({"type": "user_message", "text": "wipe it"})
            assert ws.receive_json()["type"] == "tool_call"
            ask = ws.receive_json()
            assert ask["type"] == "confirm_request"
            assert ask["mode"] == "typed"
            assert ask["challenge"] == "wipe_folder"
            # Approved flag alone must not satisfy a typed tier.
            ws.send_json({"type": "confirm_response", "approved": True})
            rest = _collect_until(ws)

    assert kinds(rest) == ["tool_denied", "token", "done"]
    assert ran == []


def test_ws_typed_tier_accepts_the_echoed_challenge(client):
    ran: list[str] = []
    with registered(_tool("wipe_folder", DESTRUCTIVE, ran)):
        state.llm = OneToolLLM("wipe_folder")
        with client.websocket_connect("/ws/c4") as ws:
            ws.send_json({"type": "user_message", "text": "wipe it"})
            assert ws.receive_json()["type"] == "tool_call"
            ws.receive_json()  # confirm_request
            ws.send_json(
                {"type": "confirm_response", "approved": True, "challenge": "wipe_folder"}
            )
            rest = _collect_until(ws)

    assert kinds(rest) == ["tool_result", "token", "done"]
    assert ran == ["wipe_folder"]


def test_ws_unanswered_confirmation_times_out_as_denial(client):
    """An abandoned prompt must not park the turn forever."""
    ran: list[str] = []
    state.settings.confirm_timeout_seconds = 0.05
    with registered(_tool("set_volume", REVERSIBLE_WRITE, ran)):
        state.llm = OneToolLLM("set_volume")
        with client.websocket_connect("/ws/c5") as ws:
            ws.send_json({"type": "user_message", "text": "turn it up"})
            rest = _collect_until(ws)

    assert kinds(rest) == ["tool_call", "confirm_request", "tool_denied", "token", "done"]
    assert ran == []


def test_rest_chat_declines_gated_tool_and_still_answers(client):
    ran: list[str] = []
    with registered(_tool("set_volume", REVERSIBLE_WRITE, ran)):
        state.llm = OneToolLLM("set_volume")
        resp = client.post("/chat", json={"text": "turn it up", "session_id": "c6"})

    assert resp.status_code == 200
    assert resp.json()["reply"] == "done"
    assert ran == []


def test_health_reports_tool_risks(client):
    risks = client.get("/health").json()["tool_risks"]
    assert risks["system_stats"] == READ_ONLY
    assert risks["read_file"] == READ_ONLY
