"""Phase 3 audit-log tests: what ran, what didn't, and where it's written."""

import json

import pytest
from conftest import make_tool, registered
from fastapi.testclient import TestClient

from app.agent import ConfirmationDecision, run_turn
from app.audit import AuditLog
from app.config import Settings
from app.llm.base import LLMClient, LLMReply, ToolCall
from app.main import app, state
from app.session import SessionStore
from app.tools.base import EXTERNAL_FACING, REVERSIBLE_WRITE


class OneToolLLM(LLMClient):
    """Asks for `name` on the first round, then answers."""

    def __init__(self, name: str, arguments: dict | None = None):
        self.name = name
        self.arguments = arguments or {}
        self.calls = 0

    async def stream_chat(self, messages):  # pragma: no cover - complete() overridden
        yield ""

    async def complete(self, messages, tools=None):
        self.calls += 1
        # Ask for the tool until we've been handed its result, so the stub
        # keeps working across several turns in one session.
        if messages[-1].get("role") != "tool":
            return LLMReply(
                tool_calls=[
                    ToolCall(name=self.name, arguments=self.arguments, id=f"c{self.calls}")
                ]
            )
        return LLMReply(content="done")


async def drain(gen) -> list[dict]:
    return [event async for event in gen]


# --- the log itself -------------------------------------------------------


def test_record_appends_one_json_object_per_line(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record(tool="system_stats", decision="ran")
    log.record(tool="open_app", decision="declined")

    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["tool"] for line in lines] == ["system_stats", "open_app"]
    assert all(json.loads(line)["ts"] for line in lines)


def test_tail_returns_the_newest_entries_oldest_first(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(5):
        log.record(tool=f"tool_{i}")

    assert [e["tool"] for e in log.tail(2)] == ["tool_3", "tool_4"]
    assert len(log.tail()) == 5
    assert log.tail(0) == []


def test_missing_log_reads_as_empty(tmp_path):
    assert AuditLog(tmp_path / "nope.jsonl").tail() == []
    assert AuditLog(tmp_path / "nope.jsonl").enabled is True


def test_disabled_log_is_a_no_op(tmp_path):
    log = AuditLog(None)
    log.record(tool="system_stats")

    assert log.enabled is False
    assert log.path is None
    assert log.tail() == []
    assert list(tmp_path.iterdir()) == []


def test_a_torn_line_does_not_hide_the_rest(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record(tool="system_stats")
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"tool": "interrup')  # e.g. killed mid-write
    log.record(tool="open_app")

    assert [e["tool"] for e in log.tail()] == ["system_stats", "open_app"]


def test_logging_never_raises_when_the_path_is_unusable(tmp_path):
    """A directory where the file should be: log silently, don't break."""
    log = AuditLog(tmp_path)  # a path that is a directory
    log.record(tool="system_stats")  # must not raise
    assert log.tail() == []


# --- recording from the agent loop ---------------------------------------


@pytest.mark.asyncio
async def test_successful_call_is_audited(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    await drain(
        run_turn(
            OneToolLLM("system_stats"),
            SessionStore(),
            "s1",
            "what are my stats?",
            audit=log,
            brain="ollama:qwen3:4b",
        )
    )

    entry = log.tail()[-1]
    assert entry["tool"] == "system_stats"
    assert entry["decision"] == "ran"
    assert entry["risk"] == "read-only"
    assert entry["brain"] == "ollama:qwen3:4b"
    assert entry["session_id"] == "s1"
    assert "cpu" in entry["result"]


@pytest.mark.asyncio
async def test_declined_call_is_audited_with_the_reason(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    ran: list[str] = []

    async def confirm(_request: dict) -> ConfirmationDecision:
        return ConfirmationDecision(approved=False)

    with registered(make_tool("set_volume", REVERSIBLE_WRITE, ran)):
        await drain(
            run_turn(
                OneToolLLM("set_volume", {"direction": "up"}),
                SessionStore(),
                "s2",
                "louder",
                confirm=confirm,
                audit=log,
            )
        )

    entry = log.tail()[-1]
    assert entry["decision"] == "declined"
    assert entry["risk"] == REVERSIBLE_WRITE
    assert entry["arguments"] == {"direction": "up"}
    assert "did not confirm" in entry["result"]["error"]
    assert ran == []


@pytest.mark.asyncio
async def test_disabled_call_is_audited_too(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    with registered(make_tool("send_email", EXTERNAL_FACING)):
        await drain(
            run_turn(OneToolLLM("send_email"), SessionStore(), "s3", "email bob", audit=log)
        )

    entry = log.tail()[-1]
    assert entry["decision"] == "disabled"
    assert entry["risk"] == EXTERNAL_FACING


# --- over the wire --------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    state.settings = Settings()
    state.sessions = SessionStore()
    with TestClient(app) as c:
        state.llm = OneToolLLM("system_stats")
        state.speech = None
        state.audit = AuditLog(tmp_path / "audit.jsonl")
        yield c


def test_turn_over_websocket_lands_in_the_audit_log(client, tmp_path):
    with client.websocket_connect("/ws/audit-1") as ws:
        ws.send_json({"type": "user_message", "text": "stats?"})
        while ws.receive_json()["type"] != "done":
            pass

    resp = client.get("/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert [e["tool"] for e in body["entries"]] == ["system_stats"]
    assert body["entries"][0]["session_id"] == "audit-1"
    # and it really is on disk, not just in memory
    assert "system_stats" in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


def test_audit_endpoint_honours_limit(client):
    with client.websocket_connect("/ws/audit-2") as ws:
        for _ in range(3):
            ws.send_json({"type": "user_message", "text": "stats?"})
            while ws.receive_json()["type"] != "done":
                pass

    assert len(client.get("/audit?limit=2").json()["entries"]) == 2


def test_audit_reports_when_logging_is_off(client):
    state.audit = None
    body = client.get("/audit").json()
    assert body == {"entries": [], "enabled": False}
