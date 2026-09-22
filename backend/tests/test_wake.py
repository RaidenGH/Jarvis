"""Phase 2 always-listening tests.

The wake-word model and audio hardware are faked, so these run with no mic,
no model download, and no Ollama — mirroring the Phase 0/1 test style.
"""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm.base import LLMClient
from app.main import app, state
from app.session import SessionStore
from app.speech.listener import WakeListener
from app.speech.service import SpeechService
from app.speech.vad import EnergyVAD, build_vad


# --- fakes -----------------------------------------------------------------


class FakeWake:
    """Scores frames from a scripted list; resets are counted."""

    name = "hey_jarvis"
    threshold = 0.5

    def __init__(self, scores):
        self.scores = list(scores)
        self.calls = 0
        self.resets = 0

    def score(self, frame):
        i = self.calls
        self.calls += 1
        return self.scores[i] if i < len(self.scores) else 0.0

    def reset(self):
        self.resets += 1


class StubLLM(LLMClient):
    async def stream_chat(self, messages):
        for token in ("echo:", " ", messages[-1]["content"]):
            yield token


class FakeSTT:
    def transcribe(self, audio, sample_rate=None):
        return "hello jarvis"


class FakeTTS:
    def synthesize(self, text, voice=None, speed=None):
        return np.zeros(24_000, dtype=np.float32), 24_000


class FakeRecorder:
    @property
    def is_recording(self):
        return False

    def start(self):
        pass

    def stop(self):
        return np.zeros(16_000, dtype=np.float32)


class FakePlayer:
    def play(self, samples, sample_rate):
        pass


class FakeListener:
    """Stands in for WakeListener at the service/WS boundary."""

    def __init__(self):
        self.started = False
        self.resumed = 0
        self.on_event = None
        self.on_utterance = None

    def start(self):
        self.started = True
        if self.on_event:
            self.on_event({"type": "listen_state", "state": "armed"})
        return True

    def stop(self):
        self.started = False

    def resume(self):
        self.resumed += 1
        if self.on_event:
            self.on_event({"type": "listen_state", "state": "armed"})


# --- frames -----------------------------------------------------------------

SILENCE = np.zeros(1280, dtype=np.float32)
LOUD = np.full(1280, 0.5, dtype=np.float32)


# --- VAD --------------------------------------------------------------------


def test_energy_vad_distinguishes_speech_from_silence():
    vad = EnergyVAD(threshold=0.05)
    assert vad.is_speech((LOUD * 32767).astype(np.int16))
    assert not vad.is_speech((SILENCE * 32767).astype(np.int16))
    assert not vad.is_speech(np.zeros(0, dtype=np.int16))


def test_build_vad_rejects_unknown_engine():
    assert isinstance(build_vad("energy"), EnergyVAD)
    with pytest.raises(ValueError):
        build_vad("nope")


# --- listener state machine -------------------------------------------------


def test_listener_wake_then_utterance():
    events, utterances = [], []
    wake = FakeWake([0.9] + [0.0] * 100)
    listener = WakeListener(
        wake,
        EnergyVAD(threshold=0.05),
        silence_seconds=0.8,
        min_speech_seconds=0.2,
        on_event=events.append,
        on_utterance=utterances.append,
    )
    listener.arm()
    events.clear()  # drop the `armed` notice emitted by arm()

    listener.feed(SILENCE)  # frame 0 scores as the wake word
    assert events[0] == {"type": "wake_detected", "name": "hey_jarvis"}
    assert events[-1] == {"type": "listen_state", "state": "capturing"}
    assert listener.state == "capturing"

    for _ in range(3):
        listener.feed(LOUD)
    for _ in range(10):
        listener.feed(SILENCE)

    assert listener.state == "paused"  # sits out the coming turn
    assert len(utterances) == 1
    # 3 speech frames + 10 trailing-silence frames buffered.
    assert utterances[0].shape[0] == 13 * 1280

    listener.resume()
    assert listener.state == "armed"


def test_listener_wake_without_speech_rearms():
    events, utterances = [], []
    listener = WakeListener(
        FakeWake([0.9]),
        EnergyVAD(threshold=0.05),
        min_speech_seconds=0.2,
        max_seconds=0.4,  # 5 frames before the cap
        on_event=events.append,
        on_utterance=utterances.append,
    )
    listener.arm()
    listener.feed(SILENCE)  # wake hit
    for _ in range(5):
        listener.feed(SILENCE)  # nothing intelligible follows

    assert utterances == []
    assert listener.state == "armed"
    assert events[-1] == {"type": "listen_state", "state": "armed"}


def test_listener_max_duration_cuts_off_ongoing_speech():
    utterances = []
    listener = WakeListener(
        FakeWake([0.9]),
        EnergyVAD(threshold=0.05),
        min_speech_seconds=0.2,
        max_seconds=0.4,  # 5 frames
        on_utterance=utterances.append,
    )
    listener.arm()
    listener.feed(SILENCE)  # wake hit, consumed
    for _ in range(5):
        listener.feed(LOUD)  # never pauses long enough to endpoint

    assert len(utterances) == 1
    assert utterances[0].shape[0] == 5 * 1280


def test_listener_paused_ignores_frames():
    events, utterances = [], []
    wake = FakeWake([0.9] * 100)
    listener = WakeListener(
        wake,
        EnergyVAD(threshold=0.05),
        on_event=events.append,
        on_utterance=utterances.append,
    )
    listener.arm()
    listener.pause()
    events.clear()
    for _ in range(3):
        listener.feed(SILENCE)

    assert listener.state == "paused"
    assert wake.calls == 0  # never scored while paused
    assert events == [] and utterances == []


# --- service / WS integration ----------------------------------------------


@pytest.fixture
def listen_client():
    state.settings = Settings()
    state.sessions = SessionStore()
    fake = FakeListener()
    speech = SpeechService(
        state.settings,
        stt=FakeSTT(),
        tts=FakeTTS(),
        recorder=FakeRecorder(),
        player=FakePlayer(),
        listener=fake,
    )
    fake.on_event = speech._emit_event
    fake.on_utterance = speech._emit_utterance
    with TestClient(app) as c:
        state.llm = StubLLM()
        state.speech = speech
        yield c, speech, fake


def test_ws_listen_flow(listen_client):
    client, speech, fake = listen_client
    with client.websocket_connect("/ws/l1") as ws:
        ws.send_json({"type": "listen_start"})
        assert ws.receive_json() == {"type": "listen_state", "state": "armed"}
        assert speech.is_listening and fake.started

        speech._emit_event({"type": "wake_detected", "name": "hey_jarvis"})
        assert ws.receive_json() == {"type": "wake_detected", "name": "hey_jarvis"}

        speech._emit_utterance(np.zeros(16_000, dtype=np.float32))
        events = []
        while True:
            event = ws.receive_json()
            events.append(event)
            if event["type"] == "done":
                break
        types = [e["type"] for e in events]
        assert "transcript" in types and "tts_done" in types
        assert "".join(e["text"] for e in events if e["type"] == "token") == (
            "echo: hello jarvis"
        )
        # The listener re-arms after the turn.
        assert ws.receive_json() == {"type": "listen_state", "state": "armed"}
        assert fake.resumed == 1

        ws.send_json({"type": "listen_stop"})
        assert ws.receive_json() == {"type": "listen_state", "state": "idle"}
        assert not speech.is_listening


def test_ws_listen_start_rejected_when_disabled(listen_client):
    client, _speech, _fake = listen_client
    # settings is an lru_cached singleton, so restore it afterwards.
    state.settings.wake_enabled = False
    try:
        with client.websocket_connect("/ws/l2") as ws:
            ws.send_json({"type": "listen_start"})
            event = ws.receive_json()
            assert event["type"] == "error"
            assert "disabled" in event["message"]
    finally:
        state.settings.wake_enabled = True


def test_ws_ptt_start_stops_listening(listen_client):
    client, speech, fake = listen_client
    with client.websocket_connect("/ws/l3") as ws:
        ws.send_json({"type": "listen_start"})
        assert ws.receive_json() == {"type": "listen_state", "state": "armed"}
        ws.send_json({"type": "ptt_start"})
        assert ws.receive_json() == {"type": "listen_state", "state": "idle"}
        assert ws.receive_json() == {"type": "ptt_state", "state": "recording"}
        assert not speech.is_listening
