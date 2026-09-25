"""Phase 3.5 agent-loop tests: streaming, plans, previews, context budget.

Everything here runs against stubs — no Ollama, no network, no disk writes
outside a tmp sandbox.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agent import _trim_tool_context
from app.config import Settings
from app.llm import build_client
from app.llm.base import LLMChunk, LLMClient, ToolCall
from app.main import app, state
from app.session import SessionStore


class ScriptedLLM(LLMClient):
    """Replays a fixed list of rounds, recording the context it was given.

    Each round is a list of chunks to stream back, so a test can shape a turn
    precisely: partial prose, a burst of tool calls, whatever is interesting.
    """

    def __init__(self, rounds: list[list[LLMChunk]]):
        self.script = list(rounds)
        self.seen: list[list[dict]] = []

    async def stream_chat(self, messages):  # pragma: no cover - unused
        yield ""

    async def stream_complete(self, messages, tools=None):
        self.seen.append([dict(message) for message in messages])
        for chunk in self.script.pop(0) if self.script else []:
            yield chunk


@pytest.fixture
def agent_client():
    state.settings = Settings()
    state.sessions = SessionStore()
    with TestClient(app) as c:
        state.speech = None
        yield c


def _collect_until(ws, until="done"):
    events = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event["type"] == until:
            return events


def _messages_in(messages, role):
    return [m for m in messages if m.get("role") == role]


# --- streaming ------------------------------------------------------------


def test_prose_streams_token_by_token(agent_client):
    state.llm = ScriptedLLM([[LLMChunk(text="Hel"), LLMChunk(text="lo"), LLMChunk(text="!")]])

    with agent_client.websocket_connect("/ws/s1") as ws:
        ws.send_json({"type": "user_message", "text": "hi"})
        events = _collect_until(ws)

    assert [e["text"] for e in events if e["type"] == "token"] == ["Hel", "lo", "!"]
    assert state.sessions.history("s1")[-1]["content"] == "Hello!"


def test_reasoning_streams_separately_from_the_answer(agent_client):
    state.llm = ScriptedLLM(
        [[LLMChunk(reasoning="weighing options"), LLMChunk(text="the answer")]]
    )

    with agent_client.websocket_connect("/ws/s2") as ws:
        ws.send_json({"type": "user_message", "text": "think"})
        events = _collect_until(ws)

    assert [e["text"] for e in events if e["type"] == "reasoning"] == ["weighing options"]
    assert [e["text"] for e in events if e["type"] == "token"] == ["the answer"]
    # Reasoning must not leak into the stored reply.
    assert state.sessions.history("s2")[-1]["content"] == "the answer"


def test_prose_then_tools_in_one_turn(agent_client):
    state.llm = ScriptedLLM(
        [
            [
                LLMChunk(text="let me look"),
                LLMChunk(calls=[ToolCall(name="system_stats", id="c1")]),
            ],
            [LLMChunk(text="done")],
        ]
    )

    with agent_client.websocket_connect("/ws/s3") as ws:
        ws.send_json({"type": "user_message", "text": "stats?"})
        events = _collect_until(ws)

    kinds = [e["type"] for e in events]
    assert kinds.index("token") < kinds.index("tool_call") < kinds.index("tool_result")
    assert "done" in [e.get("text") for e in events if e["type"] == "token"]


# --- plans ----------------------------------------------------------------


def test_update_plan_emits_a_plan_frame(agent_client):
    state.llm = ScriptedLLM(
        [
            [
                LLMChunk(
                    calls=[
                        ToolCall(
                            name="update_plan",
                            id="p1",
                            arguments={
                                "items": [
                                    {"task": "read the test", "status": "in_progress"},
                                    {"task": "fix it", "status": "pending"},
                                ]
                            },
                        )
                    ]
                )
            ],
            [LLMChunk(text="working on it")],
        ]
    )

    with agent_client.websocket_connect("/ws/s4") as ws:
        ws.send_json({"type": "user_message", "text": "fix the test"})
        events = _collect_until(ws)

    plan = [e for e in events if e["type"] == "plan"]
    assert len(plan) == 1
    assert plan[0]["items"][0]["task"] == "read the test"


# --- previews on the approval prompt --------------------------------------


def test_edit_confirmation_carries_a_diff_preview(agent_client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.tools.files.get_settings",
        lambda: SimpleNamespace(tool_root=str(tmp_path)),
    )
    (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
    state.llm = ScriptedLLM(
        [
            [
                LLMChunk(
                    calls=[
                        ToolCall(
                            name="edit_file",
                            id="e1",
                            arguments={
                                "path": "a.py",
                                "find": "value = 1",
                                "replace": "value = 2",
                            },
                        )
                    ]
                )
            ],
            [LLMChunk(text="changed it")],
        ]
    )

    with agent_client.websocket_connect("/ws/s5") as ws:
        ws.send_json({"type": "user_message", "text": "bump it"})
        events = _collect_until(ws, "confirm_request")
        request = events[-1]
        assert request["risk"] == "reversible-write"
        assert "-value = 1" in request["preview"]
        assert "+value = 2" in request["preview"]

        ws.send_json({"type": "confirm_response", "approved": False})
        rest = _collect_until(ws)

    assert any(e["type"] == "tool_denied" for e in rest)
    # Declined means untouched.
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "value = 1\n"


def test_read_only_tools_never_prompt(agent_client):
    state.llm = ScriptedLLM(
        [
            [LLMChunk(calls=[ToolCall(name="grep_files", id="g1", arguments={"pattern": "x"})])],
            [LLMChunk(text="nothing found")],
        ]
    )

    with agent_client.websocket_connect("/ws/s6") as ws:
        ws.send_json({"type": "user_message", "text": "find x"})
        events = _collect_until(ws)

    assert not any(e["type"] == "confirm_request" for e in events)
    assert any(e["type"] == "tool_result" for e in events)


# --- context budget -------------------------------------------------------


def _tool_messages(count: int, chars: int) -> list[dict]:
    return [
        {"role": "tool", "name": "read_file", "tool_call_id": f"t{i}", "content": "x" * chars}
        for i in range(count)
    ]


def test_trim_elides_the_oldest_results_and_keeps_recent_ones():
    messages = _tool_messages(10, 1_000)

    _trim_tool_context(messages, budget=2_000)

    elided = [m for m in messages if "elided" in m["content"]]
    kept = [m for m in messages if "elided" not in m["content"]]
    assert len(elided) == 4  # only the candidates older than the recent window
    assert len(kept) == 6
    # The stub names the tool, so the model can go get it again.
    assert "read_file" in elided[0]["content"]


def test_trim_leaves_a_turn_under_budget_alone():
    messages = _tool_messages(10, 10)

    _trim_tool_context(messages, budget=50_000)

    assert all(m["content"] == "x" * 10 for m in messages)


def test_trim_is_a_no_op_when_there_are_few_results():
    messages = _tool_messages(3, 10_000)

    _trim_tool_context(messages, budget=10)

    assert len(messages) == 3
    assert all(m["content"] == "x" * 10_000 for m in messages)


# --- per-run model override ----------------------------------------------


def test_build_client_honours_a_model_override():
    settings = Settings()

    assert build_client(settings, model="qwen2.5:7b")._model == "qwen2.5:7b"
    assert build_client(settings)._model == settings.llm_model
