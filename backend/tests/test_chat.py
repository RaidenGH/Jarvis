"""Tests for Phase 0 backend. No Ollama required — the LLM is stubbed."""

import pytest
from fastapi.testclient import TestClient

from app.llm.base import LLMClient
from app.main import app, state


class StubLLM(LLMClient):
    """Echoes tokens deterministically so tests never touch the network."""

    async def stream_chat(self, messages):
        last = messages[-1]["content"]
        for token in ("echo:", " ", last):
            yield token


@pytest.fixture
def client():
    state.settings = __import__("app.config", fromlist=["Settings"]).Settings()
    state.sessions = __import__("app.session", fromlist=["SessionStore"]).SessionStore()
    # Enter the context first so the lifespan has run, THEN swap in the stub —
    # otherwise lifespan overwrites state.llm with the real Ollama client.
    with TestClient(app) as c:
        state.llm = StubLLM()
        yield c


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["provider"] == "ollama"


def test_chat_rest_roundtrip(client):
    resp = client.post("/chat", json={"text": "hello", "session_id": "t1"})
    assert resp.status_code == 200
    assert resp.json()["reply"] == "echo: hello"


def test_session_history_is_kept_and_capped():
    from app.session import SessionStore

    store = SessionStore(max_messages=4)
    for i in range(6):
        store.add("s", "user", f"msg {i}")
    history = store.history("s")
    assert len(history) == 4
    assert history[0]["content"] == "msg 2"

    store.reset("s")
    assert store.history("s") == []


def test_ws_streaming_flow(client):
    with client.websocket_connect("/ws/t2") as ws:
        ws.send_json({"type": "user_message", "text": "hi"})
        tokens = []
        while True:
            event = ws.receive_json()
            if event["type"] == "done":
                break
            tokens.append(event["text"])
        assert "".join(tokens) == "echo: hi"


def test_llm_error_surfaces_as_502(client):
    class BoomLLM(LLMClient):
        async def stream_chat(self, messages):
            raise RuntimeError("ollama down")
            yield  # pragma: no cover

    state.llm = BoomLLM()
    resp = client.post("/chat", json={"text": "x", "session_id": "t3"})
    assert resp.status_code == 502
