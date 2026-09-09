"""Text-to-speech via Kokoro-82M (kokoro-onnx, pure onnxruntime).

Lazily loads the ~325 MB ONNX model on first synthesize so backend startup
stays instant. Model files are fetched by `ensure_kokoro_models`.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from .models import ensure_kokoro_models

logger = logging.getLogger(__name__)


class TTSEngine:
    def __init__(self, model_dir: str | Path, voice: str = "af_heart", speed: float = 1.0):
        self._model_dir = Path(model_dir)
        self._voice = voice
        self._speed = speed
        self._kokoro = None  # lazy

    def _load(self):
        if self._kokoro is None:
            from espeakng_loader import get_data_path, get_library_path
            from kokoro_onnx import Kokoro
            from kokoro_onnx.config import EspeakConfig

            paths = ensure_kokoro_models(self._model_dir)
            # espeak-ng (phonemizer backend) ships inside the espeakng-loader wheel.
            espeak_config = EspeakConfig(
                lib_path=get_library_path(), data_path=get_data_path()
            )
            logger.info("Loading Kokoro TTS model (one-time)…")
            self._kokoro = Kokoro(
                str(paths["kokoro-v1.0.onnx"]),
                str(paths["voices-v1.0.bin"]),
                espeak_config=espeak_config,
            )
        return self._kokoro

    def synthesize(
        self, text: str, voice: str | None = None, speed: float | None = None
    ) -> tuple[np.ndarray, int]:
        """Return (mono float32 samples, sample_rate) for the spoken text."""
        if not text or not text.strip():
            return np.zeros(0, dtype=np.float32), 24_000
        kokoro = self._load()
        samples, sample_rate = kokoro.create(
            text,
            voice=voice or self._voice,
            speed=speed or self._speed,
            lang="en-us",
        )
        return samples.astype(np.float32), sample_rate