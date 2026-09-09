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
from .stt import STTEngine
from .tts import TTSEngine

logger = logging.getLogger(__name__)


class SpeechService:
    def __init__(
        self,
        settings: Settings,
        stt: STTEngine | None = None,
        tts: TTSEngine | None = None,
        recorder: MicRecorder | None = None,
        player: SpeakerPlayer | None = None,
    ):
        self.settings = settings
        # Lazy defaults: engines constructed on first use so backend startup
        # (and tests) never touch models or audio hardware.
        self._stt = stt
        self._tts = tts
        self._recorder = recorder
        self._player = player

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