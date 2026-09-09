"""Microphone capture and speaker playback via sounddevice.

Push-to-talk semantics: `start()` opens an input stream that appends frames
to a buffer; `stop()` returns the captured mono float32 waveform. Playback
is plain blocking `sd.play` + `sd.wait`.
"""

from __future__ import annotations

import threading

import numpy as np


class MicRecorder:
    def __init__(self, sample_rate: int = 16_000, max_seconds: float = 30.0):
        import sounddevice as sd  # keep import local: needs PortAudio

        self._sd = sd
        self._sample_rate = sample_rate
        self._max_frames = int(max_seconds * sample_rate)
        self._frames: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        """Open the input stream; safe to call once while idle."""
        if self.is_recording:
            return
        self._frames = []

        def _callback(indata, _frames, _time, _status) -> None:
            # Called on PortAudio's audio thread; just buffer and enforce cap.
            with self._lock:
                remaining = self._max_frames - sum(f.size for f in self._frames)
                if remaining > 0:
                    self._frames.append(indata[:remaining].copy())

        self._stream = self._sd.InputStream(
            samplerate=self._sample_rate, channels=1, dtype="float32", callback=_callback
        )
        self._stream.start()

    def stop(self) -> np.ndarray:
        """Stop capturing and return everything recorded so far."""
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()
        with self._lock:
            frames, self._frames = self._frames, []
        if not frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(frames)


class SpeakerPlayer:
    def play(self, samples: np.ndarray, sample_rate: int) -> None:
        """Block until the waveform has finished playing."""
        import sounddevice as sd

        if samples is None or len(samples) == 0:
            return
        sd.play(samples, sample_rate)
        sd.wait()