"""Wake-word detection via openWakeWord.

Wraps the pretrained model so the rest of the app never imports openwakeword
directly and so tests can stub this class. openWakeWord expects 16-bit 16 kHz
PCM frames — ideally multiples of 80 ms (1280 samples).

The package weights (a few MB, plus the shared feature models and a Silero VAD
model) are downloaded into openWakeWord's own resources dir on first use.
"""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)


class WakeWordEngine:
    def __init__(
        self,
        model: str = "hey_jarvis",
        threshold: float = 0.5,
        vad_threshold: float = 0.5,
        inference_framework: str = "onnx",
    ):
        self._name = model
        self._threshold = threshold
        self._vad_threshold = vad_threshold
        self._framework = inference_framework
        self._model = None  # lazy: importing/downloading is costly

    @property
    def name(self) -> str:
        return self._name

    @property
    def threshold(self) -> float:
        return self._threshold

    def _load(self):
        if self._model is None:
            from openwakeword.model import Model
            from openwakeword.utils import download_models

            # Idempotent: skips files that already exist.
            download_models(model_names=[self._name])
            logger.info("Loading openWakeWord model %r (first use)…", self._name)
            self._model = Model(
                wakeword_models=[self._name],
                inference_framework=self._framework,
                vad_threshold=self._vad_threshold,
            )
        return self._model

    def score(self, frame: np.ndarray) -> float:
        """Return the wake-word confidence in [0, 1] for one int16 PCM frame."""
        if frame is None or len(frame) == 0:
            return 0.0
        predictions = self._load().predict(frame)
        if self._name in predictions:
            return float(predictions[self._name])
        return float(max(predictions.values(), default=0.0))

    def reset(self) -> None:
        """Clear internal feature buffers (called after a detection)."""
        if self._model is not None:
            self._model.reset()
