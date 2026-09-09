"""Speech-to-text via faster-whisper (CTranslate2).

Wraps the model so the rest of the app never imports faster_whisper
directly and so tests can stub this class.
"""

from __future__ import annotations

from typing import Any

import numpy as np


class STTEngine:
    def __init__(
        self,
        model: str = "small",
        device: str = "auto",
        compute_type: str = "auto",
        sample_rate: int = 16000,
    ):
        # Heavy import kept inside __init__ so `import app.main` stays cheap
        # and tests never pay for it.
        from faster_whisper import WhisperModel

        self._sample_rate = sample_rate
        self._device = device
        try:
            self._model = WhisperModel(model, device=device, compute_type=compute_type)
        except (ImportError, OSError, RuntimeError):
            # CUDA requested/auto-detected but cuDNN/DLLs missing -> fall back.
            self._model = WhisperModel(model, device="cpu", compute_type="int8")

    @property
    def device(self) -> str:
        return self._device

    def transcribe(self, audio: np.ndarray, sample_rate: int | None = None) -> str:
        """Transcribe a mono float32 waveform; returns cleaned text."""
        if audio is None or len(audio) == 0:
            return ""
        sr = sample_rate or self._sample_rate
        segments, _info = self._model.transcribe(audio, beam_size=5)
        return " ".join(seg.text.strip() for seg in segments).strip()