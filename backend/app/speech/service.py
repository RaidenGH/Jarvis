"""Speech service: owns the STT/TTS/audio components and runs the
push-to-talk turn. Components are constructor-injected so tests can stub
them without hardware or model downloads.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np

from ..config import Settings
from .audio import MicRecorder, SpeakerPlayer
from .listener import WakeListener
from .stt import STTEngine
from .tts import TTSEngine
from .vad import build_vad
from .wake import WakeWordEngine

logger = logging.getLogger(__name__)


class SpeechService:
    def __init__(
        self,
        settings: Settings,
        stt: STTEngine | None = None,
        tts: TTSEngine | None = None,
        recorder: MicRecorder | None = None,
        player: SpeakerPlayer | None = None,
        wake: WakeWordEngine | None = None,
        vad=None,
        listener: WakeListener | None = None,
    ):
        self.settings = settings
        # Lazy defaults: engines constructed on first use so backend startup
        # (and tests) never touch models or audio hardware.
        self._stt = stt
        self._tts = tts
        self._recorder = recorder
        self._player = player
        self._wake = wake
        self._vad = vad
        self._listener = listener
        # Always-listening plumbing, live only between start/stop_listening.
        self._event_queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # --- components (lazily built) ---
    @property
    def stt(self) -> STTEngine:
        if self._stt is None:
            self._stt = STTEngine(
                model=self.settings.whisper_model,
                device=self.settings.whisper_device,
                compute_type=self.settings.whisper_compute_type,
                sample_rate=self.settings.stt_sample_rate,
            )
        return self._stt

    @property
    def tts(self) -> TTSEngine:
        if self._tts is None:
            self._tts = TTSEngine(
                model_dir=self.settings.tts_model_dir,
                voice=self.settings.tts_voice,
                speed=self.settings.tts_speed,
            )
        return self._tts

    @property
    def recorder(self) -> MicRecorder:
        if self._recorder is None:
            self._recorder = MicRecorder(
                sample_rate=self.settings.stt_sample_rate,
                max_seconds=self.settings.ptt_max_seconds,
            )
        return self._recorder

    @property
    def player(self) -> SpeakerPlayer:
        if self._player is None:
            self._player = SpeakerPlayer()
        return self._player

    @property
    def wake(self) -> WakeWordEngine:
        if self._wake is None:
            self._wake = WakeWordEngine(
                model=self.settings.wake_word,
                threshold=self.settings.wake_threshold,
                vad_threshold=self.settings.wake_vad_threshold,
            )
        return self._wake

    @property
    def vad(self):
        if self._vad is None:
            self._vad = build_vad(
                self.settings.vad_engine,
                energy_threshold=self.settings.vad_energy_threshold,
            )
        return self._vad

    @property
    def listener(self) -> WakeListener:
        if self._listener is None:
            self._listener = WakeListener(
                self.wake,
                self.vad,
                sample_rate=self.settings.stt_sample_rate,
                frame_ms=self.settings.wake_frame_ms,
                silence_seconds=self.settings.wake_silence_seconds,
                max_seconds=self.settings.wake_max_seconds,
                min_speech_seconds=self.settings.wake_min_speech_seconds,
                on_event=self._emit_event,
                on_utterance=self._emit_utterance,
            )
        return self._listener

    # --- always-listening (Phase 2) ---
    @property
    def is_listening(self) -> bool:
        return self._event_queue is not None

    def start_listening(self) -> asyncio.Queue | None:
        """Begin wake-word listening. Returns an event queue, or None if the
        mic could not be opened (or listening is already active)."""
        if self.is_listening:
            return None
        queue: asyncio.Queue = asyncio.Queue()
        self._event_queue = queue
        self._loop = asyncio.get_running_loop()
        if not self.listener.start():
            self._event_queue = None
            self._loop = None
            return None
        return queue

    def stop_listening(self) -> None:
        if self._listener is not None:
            self._listener.stop()
        self._event_queue = None
        self._loop = None

    def resume_listening(self) -> None:
        """Re-arm after a spoken turn has been answered and played."""
        if self._listener is not None:
            self._listener.resume()

    def _emit_event(self, event: dict) -> None:
        # Called from the listener thread; hop back onto the event loop.
        queue, loop = self._event_queue, self._loop
        if queue is None or loop is None:
            return
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def _emit_utterance(self, audio: np.ndarray) -> None:
        self._emit_event({"type": "utterance", "audio": audio})

    # --- push-to-talk flow (run from the WS handler) ---
    def start_recording(self) -> bool:
        """Begin mic capture. Returns False if already recording."""
        recorder = self.recorder
        if recorder.is_recording:
            return False
        try:
            recorder.start()
        except Exception as exc:  # noqa: BLE001 - no mic / no PortAudio
            logger.warning("Mic start failed: %s", exc)
            return False
        return True

    def stop_recording(self) -> np.ndarray:
        return self.recorder.stop()

    async def transcribe(self, audio: np.ndarray) -> str:
        return await asyncio.to_thread(self.stt.transcribe, audio)

    async def speak(self, text: str) -> None:
        """Synthesize and play the reply aloud (blocking parts off-thread)."""
        if not text.strip():
            return
        samples, sample_rate = await asyncio.to_thread(self.tts.synthesize, text)
        await asyncio.to_thread(self.player.play, samples, sample_rate)