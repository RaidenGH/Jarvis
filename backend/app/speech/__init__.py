"""Speech pipeline: push-to-talk and always-listening voice loops.

Phases: PTT -> STT -> LLM -> TTS -> playback (Phase 1), plus wake-word +
VAD always-listening capture (Phase 2). All heavy libraries (faster_whisper,
kokoro_onnx, openwakeword, sounddevice) are imported lazily by the individual
engines, so importing this package is cheap.
"""

from .listener import WakeListener
from .service import SpeechService
from .vad import EnergyVAD, SileroVAD, build_vad
from .wake import WakeWordEngine

__all__ = [
    "SpeechService",
    "WakeListener",
    "WakeWordEngine",
    "EnergyVAD",
    "SileroVAD",
    "build_vad",
]