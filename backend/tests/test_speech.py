"""Phase 1 voice flow tests. Speech components are fakes — no mic, no
speakers, no model downloads; the LLM is stubbed as in test_chat.py.
"""

import pytest
import numpy as np
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm.base import LLMClient
from app.main import app, state
from app.speech.service import SpeechService


class StubLLM(LLMClient):
    async def stream_chat(self, messages):
        last = messages[-1]["content"]
        for token in ("echo:", " ", last):
            yield token


class FakeSTT:
    def __init__(self, text="hello jarvis"):
        self.text = text

    def transcribe(self, audio, sample_rate=None):
        return self.text


class FakeTTS:
    def __init__(self):
        self.synthesized = []

    def synthesize(self, text, voice=None, speed=None):
        self.synthesized.append(text)
        return np.zeros(24_000, dtype=np.float32), 24_000


class FakeRecorder:
    def __init__(self, audio=None):
        self.recording = False
        self.audio = (
            audio if audio is not None else np.zeros(16_000, dtype=np.float32)
        )

    @property
    def is_recording(self):
        return self.recording

    def start(self):
        self.recording = True

    def stop(self):
        self.recording = False
        return self.audio


class FakePlayer:
    def __init__(self):
        self.played = []

    def play(self, samples, sample_rate):
        self.played.append((samples, sample_rate))


@pytest.fixture
def voice_client():
    state.settings = Settings()
    state.sessions = __import__(
        "app.session", fromlist=["SessionStore"]
    ).SessionStore()
    speech = SpeechService(
        state.settings,
        stt=FakeSTT(),
        tts=FakeTTS(),
        recorder=FakeRecorder(),
        player=FakePlayer(),
    )
    with TestClient(app) as c:
        state.llm = StubLLM()
        state.speech = speech
        yield c


def test_ptt_full_loop(voice_client):
    with voice_client.websocket_connect("/ws/v1") as ws:
        ws.send_json({"type": "ptt_start"})
        assert ws.receive_json() == {"type": "ptt_state", "state": "recording"}

        ws.send_json({"type": "ptt_stop"})
        events = []
        while True:
            event = ws.receive_json()
            events.append(event)
            if event["type"] == "done":
                break

    types = [e["type"] for e in events]
    assert "transcript" in types
    assert "tts_start" in types
    assert "tts_done" in types
    transcript = next(e for e in events if e["type"] == "transcript")
    assert transcript["text"] == "hello jarvis"
    # Reply tokens stream through the same path as text chat.
    tokens = [e["text"] for e in events if e["type"] == "token"]
    assert "".join(tokens) == "echo: hello jarvis"


def test_ptt_requires_start_before_stop(voice_client):
    with voice_client.websocket_connect("/ws/v2") as ws:
        ws.send_json({"type": "ptt_stop"})
        event = ws.receive_json()
        assert event["type"] == "error"
        assert "not recording" in event["message"]


def test_ptt_rejects_double_start(voice_client):
    with voice_client.websocket_connect("/ws/v3") as ws:
        ws.send_json({"type": "ptt_start"})
        assert ws.receive_json()["type"] == "ptt_state"
        ws.send_json({"type": "ptt_start"})
        event = ws.receive_json()
        assert event["type"] == "error"
        assert "already recording" in event["message"]


def test_ptt_silence_skips_llm_and_tts(voice_client):
    # Silence -> empty transcript -> no LLM call, no audio, straight to idle.
    silence = SpeechService(
        state.settings,
        stt=FakeSTT(text="   "),
        tts=FakeTTS(),
        recorder=FakeRecorder(),
        player=FakePlayer(),
    )
    state.speech = silence
    with voice_client.websocket_connect("/ws/v4") as ws:
        ws.send_json({"type": "ptt_start"})
        assert ws.receive_json()["type"] == "ptt_state"
        ws.send_json({"type": "ptt_stop"})
        # First the turn announces it's transcribing, then silence -> idle.
        assert ws.receive_json() == {"type": "ptt_state", "state": "transcribing"}
        assert ws.receive_json() == {"type": "ptt_state", "state": "idle"}


def test_speech_service_components_are_lazy():
    """Constructing SpeechService must not import heavy libs or models."""
    svc = SpeechService(Settings())
    assert svc._stt is None and svc._tts is None
    assert svc._recorder is None and svc._player is None