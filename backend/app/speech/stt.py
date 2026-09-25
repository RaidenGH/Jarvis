"""Speech-to-text via faster-whisper (CTranslate2).

Wraps the model so the rest of the app never imports faster_whisper
directly and so tests can stub this class.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


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
        self._model = self._load(WhisperModel, model, device, compute_type)

    def _load(self, whisper_model: Any, model: str, device: str, compute_type: str):
        """Build the model, but only keep it if it can actually encode.

        `device="auto"` is documented as CUDA-then-CPU, and construction alone
        can't honour that: ctranslate2 happily builds a CUDA model without
        cuBLAS/cuDNN present and only reaches for `cublas64_12.dll` on the
        first encode. So probe it here, once, and fall back for real.
        """
        try:
            engine = whisper_model(model, device=device, compute_type=compute_type)
        except (ImportError, OSError, RuntimeError):
            # CUDA requested/auto-detected but cuDNN/DLLs missing -> fall back.
            logger.warning("Whisper could not start on %s; using the CPU", device)
            return self._cpu_model(whisper_model, model)
        if self._encodes(engine):
            return engine
        logger.warning(
            "Whisper on %s failed its first encode (missing CUDA libraries?); "
            "falling back to the CPU",
            device,
        )
        return self._cpu_model(whisper_model, model)

    def _cpu_model(self, whisper_model: Any, model: str):
        self._device = "cpu"
        return whisper_model(model, device="cpu", compute_type="int8")

    def _encodes(self, engine: Any) -> bool:
        """Push a quarter second of silence through the model."""
        try:
            segments, _info = engine.transcribe(
                np.zeros(self._sample_rate // 4, dtype=np.float32), beam_size=1
            )
            list(segments)  # decoding is lazy; force it
            return True
        except Exception:  # noqa: BLE001 - any failure means "use the CPU"
            return False

    @property
    def device(self) -> str:
        return self._device

    def transcribe(self, audio: np.ndarray, sample_rate: int | None = None) -> str:
        """Transcribe a mono float32 waveform; returns cleaned text."""
        if audio is None:
            return ""
        # Normalise whatever the capture layer hands over. CTranslate2 treats a
        # 2-D input as `(batch, samples)`, so a stray `(samples, 1)` block turns
        # a five-second clip into 76k one-sample utterances (and a 68 GiB mel
        # spectrogram). One dimension, always.
        waveform = np.ascontiguousarray(
            np.asarray(audio, dtype=np.float32).reshape(-1)
        )
        if waveform.size == 0:
            return ""
        sr = sample_rate or self._sample_rate
        segments, _info = self._model.transcribe(waveform, beam_size=5)
        return " ".join(seg.text.strip() for seg in segments).strip()