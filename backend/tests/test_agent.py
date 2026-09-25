"""Phase 2.5 agent-loop tests: tool calling over WS/REST. No Ollama needed."""

import pytest
from fastapi.testclient import TestClient

from app.llm.base import LLMClient, LLMReply, ToolCall
from app.main import app, state
from app.session import SessionStore


class ToolStubLLM(LLMClient):
    """Requests a tool on the first turn, answers on the second."""

    def __init__(self, first_call: ToolCall | None = None, answer: str = "42 cores"):
        self.calls = 0
        self.first_call = first_call or ToolCall(name="system_stats", id="call-1")
        self.answer = answer

    async def stream_chat(self, messages):  # pragma: no cover - complete() overridden
        yield self.answer

    async def complete(self, messages, tools=None):
        self.calls += 1
        assert tools and tools[0]["function"]["name"] == "system_stats"
        if self.calls == 1:
            return LLMReply(tool_calls=[self.first_call])
        return LLMReply(content=self.answer)


class PlainStubLLM(LLMClient):
    """No tool support: proves the base-class complete() fallback works."""

    async def stream_chat(self, messages):
        last = messages[-1]["content"]
        for token in ("echo:", " ", last):
            yield token


@pytest.fixture
def agent_client():
    state.settings = __import__("app.config", fromlist=["Settings"]).Settings()
    state.sessions = SessionStore()
    with TestClient(app) as c:
        state.llm = ToolStubLLM()
        state.speech = None
        yield c


def _collect_until(ws, until="done"):
    events = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event["type"] == until:
            return events


def test_ws_tool_call_roundtrip(agent_client):
    with agent_client.websocket_connect("/ws/a1") as ws:
        ws.send_json({"type": "user_message", "text": "how many cores?"})
        events = _collect_until(ws)

    kinds = [e["type"] for e in events]
    assert kinds[0] == "tool_call"
    assert kinds[1] == "tool_result"
    assert kinds[2] == "token"
    assert kinds[3] == "done"

    call = events[0]
    assert call["name"] == "system_stats"
    assert call["arguments"] == {}
    assert "cpu" in events[1]["result"]
    assert "".join(e["text"] for e in events if e["type"] == "token") == "42 cores"

    history = state.sessions.history("a1")
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[-1]["content"] == "42 cores"


def test_rest_chat_folds_tools_into_reply(agent_client):
    resp = agent_client.post(
        "/chat", json={"text": "stats?", "session_id": "a2"}
    )
    assert resp.status_code == 200
    assert resp.json()["reply"] == "42 cores"


def test_unknown_tool_reports_error_and_loop_continues(agent_client):
    state.llm = ToolStubLLM(
        first_call=ToolCall(name="launch_missiles", arguments={"t": 1}, id="call-x")
    )
    with agent_client.websocket_connect("/ws/a3") as ws:
        ws.send_json({"type": "user_message", "text": "fire away"})
        events = _collect_until(ws)

    result = events[1]
    assert result["type"] == "tool_result"
    assert "unknown tool" in result["result"]["error"]
    assert events[2]["type"] == "token"  # the loop recovered and answered
    assert events[-1]["type"] == "done"


def test_tool_call_limit_stops_runaway_loops(agent_client):
    class AlwaysCalls(ToolStubLLM):
        async def complete(self, messages, tools=None):
            self.calls += 1
            return LLMReply(
                tool_calls=[ToolCall(name="system_stats", id=f"c{self.calls}")]
            )

    state.llm = AlwaysCalls()
    with agent_client.websocket_connect("/ws/a4") as ws:
        ws.send_json({"type": "user_message", "text": "loop"})
        events = _collect_until(ws)

    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_calls) == state.settings.max_tool_rounds
    assert "limit" in state.sessions.history("a4")[-1]["content"]


def test_base_complete_collects_stream_chat():
    import asyncio

    reply = asyncio.run(PlainStubLLM().complete([{"role": "user", "content": "hi"}]))
    assert reply == LLMReply(content="echo: hi")
    assert reply.tool_calls == []


def test_provider_failure_surfaces_as_502(agent_client):
    class BoomLLM(LLMClient):
        async def stream_chat(self, messages):
            raise RuntimeError("ollama down")
            yield  # pragma: no cover

    state.llm = BoomLLM()
    resp = agent_client.post("/chat", json={"text": "x", "session_id": "a5"})
    assert resp.status_code == 502
    # The failed turn records the user message but no half-written reply.
    assert [m["role"] for m in state.sessions.history("a5")] == ["user"]


# --- persistence ----------------------------------------------------------


def test_history_persists_across_store_instances(tmp_path):
    path = tmp_path / "history.json"
    first = SessionStore(path=path)
    first.add("cli", "user", "hi")
    first.add("cli", "assistant", "hello")

    second = SessionStore(path=path)
    assert second.history("cli") == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]

    second.reset("cli")
    assert SessionStore(path=path).history("cli") == []


def test_corrupt_history_file_is_ignored(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("{not json", encoding="utf-8")
    store = SessionStore(path=path)
    assert store.history("anything") == []
