"""Voice-activity detection for endpointing a captured utterance.

Two engines behind one tiny interface:

* ``EnergyVAD`` — RMS threshold, zero extra dependencies. The default: robust
  enough to find the end of a spoken command after a wake word.
* ``SileroVAD`` — openWakeWord's bundled Silero VAD (ONNX), higher quality.

Both accept int16 mono 16 kHz frames, matching openWakeWord's ``predict``.
"""

from __future__ import annotations

import numpy as np


class EnergyVAD:
    def __init__(self, threshold: float = 0.012):
        self._threshold = threshold

    def is_speech(self, frame: np.ndarray) -> bool:
        if frame is None or len(frame) == 0:
            return False
        rms = float(np.sqrt(np.mean((frame.astype(np.float32) / 32768.0) ** 2)))
        return rms >= self._threshold

    def reset(self) -> None:  # stateless
        pass


class SileroVAD:
    """Silero VAD from openWakeWord, buffered into its 30 ms (480-sample) chunks."""

    def __init__(self, threshold: float = 0.5, frame_size: int = 480):
        self._threshold = threshold
        self._frame_size = frame_size
        self._vad = None  # lazy: importing needs onnxruntime
        self._buffer = np.zeros(0, dtype=np.int16)

    def _load(self):
        if self._vad is None:
            from openwakeword.vad import VAD

            self._vad = VAD()
        return self._vad

    def is_speech(self, frame: np.ndarray) -> bool:
        if frame is None or len(frame) == 0:
            return False
        vad = self._load()
        buf = np.concatenate([self._buffer, frame.astype(np.int16)])
        n = len(buf) // self._frame_size
        speech = False
        for i in range(n):
            chunk = buf[i * self._frame_size : (i + 1) * self._frame_size]
            if float(vad.predict(chunk, frame_size=self._frame_size)) >= self._threshold:
                speech = True
        self._buffer = buf[n * self._frame_size :]
        return speech

    def reset(self) -> None:
        self._buffer = np.zeros(0, dtype=np.int16)
        if self._vad is not None:
            self._vad.reset_states()


def build_vad(
    engine: str, energy_threshold: float = 0.012, silero_threshold: float = 0.5
):
    """Return the endpointing VAD for the configured ``vad_engine``."""
    if engine == "silero":
        return SileroVAD(threshold=silero_threshold)
    if engine == "energy":
        return EnergyVAD(threshold=energy_threshold)
    raise ValueError(f"Unknown vad_engine: {engine!r}")
