"""Phase 1 speech pipeline: push-to-talk -> STT -> LLM -> TTS -> playback.

All heavy libraries (faster_whisper, kokoro_onnx, sounddevice) are imported
lazily by the individual engines, so importing this package is cheap.
"""

from .service import SpeechService

__all__ = ["SpeechService"]