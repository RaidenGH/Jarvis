"""Always-listening wake-word loop.

Owns a microphone input stream on a worker thread. In ``armed`` it scores every
frame against the wake-word model; on a hit it moves to ``capturing`` and
buffers audio until the endpointing VAD reports trailing silence, then hands the
utterance to the async layer via ``on_utterance``. While a turn is being
transcribed/answered/spoken the loop sits in ``paused`` so Jarvis cannot wake
itself.

The audio-thread plumbing is deliberately thin; the state machine lives in
``feed``, which tests drive synchronously with synthetic frames.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)

# openWakeWord's native frame: 80 ms @ 16 kHz.
FRAME_MS = 80

EventFn = Callable[[dict], None]
UtteranceFn = Callable[[np.ndarray], None]


class WakeListener:
    def __init__(
        self,
        wake,
        vad,
        sample_rate: int = 16_000,
        frame_ms: int = FRAME_MS,
        silence_seconds: float = 0.8,
        max_seconds: float = 15.0,
        min_speech_seconds: float = 0.2,
        on_event: EventFn | None = None,
        on_utterance: UtteranceFn | None = None,
        stream_factory: Callable | None = None,
    ):
        self._wake = wake
        self._vad = vad
        self._sample_rate = sample_rate
        self._frame_samples = int(sample_rate * frame_ms / 1000)
        self._silence_frames = max(1, int(silence_seconds * 1000 / frame_ms))
        self._max_frames = max(1, int(max_seconds * 1000 / frame_ms))
        self._min_speech_frames = max(1, int(min_speech_seconds * 1000 / frame_ms))
        self._on_event = on_event
        self._on_utterance = on_utterance
        self._stream_factory = stream_factory

        self._state = "idle"  # idle | armed | capturing | paused
        self._buffer: list[np.ndarray] = []
        self._silence_run = 0
        self._speech_frames = 0

        self._running = False
        self._thread: threading.Thread | None = None
        self._frames: queue.Queue = queue.Queue()
        self._stream = None

    # --- introspection ---
    @property
    def state(self) -> str:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def frame_samples(self) -> int:
        return self._frame_samples

    # --- lifecycle ---
    def start(self) -> bool:
        """Open the mic and begin listening. False if already running or no mic."""
        if self._running:
            return False
        factory = self._stream_factory or _default_stream_factory
        try:
            self._stream = factory(self._on_audio, self._sample_rate, self._frame_samples)
            self._stream.start()
        except Exception as exc:  # noqa: BLE001 - no mic / no PortAudio
            logger.warning("Wake listener mic start failed: %s", exc)
            self._stream = None
            return False
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="wake-listener", daemon=True
        )
        self._thread.start()
        self.arm()
        return True

    def arm(self) -> None:
        """Enter the armed state. Used by ``start`` and by tests that drive
        ``feed`` directly without an audio stream."""
        self._reset_turn()
        self._set_state("armed")

    def stop(self) -> None:
        if not self._running:
            self._set_state("idle")
            return
        self._running = False
        self._frames.put(None)  # unblock the worker
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001 - best effort teardown
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._reset_turn()
        self._set_state("idle")

    def pause(self) -> None:
        """Stop scoring while a reply is being produced or spoken."""
        if self._state == "armed":
            self._set_state("paused", emit=False)

    def resume(self) -> None:
        """Re-arm after a turn, clearing stale audio buffers."""
        if self._state == "paused":
            self._wake.reset()
            self._vad.reset()
            self._set_state("armed")

    # --- audio plumbing ---
    def _on_audio(self, indata, _frames, _time, _status) -> None:
        # PortAudio thread: hand the mono frame to the worker, never block.
        self._frames.put_nowait(indata[:, 0].copy())

    def _run(self) -> None:
        while self._running:
            frame = self._frames.get()
            if frame is None:
                break
            self.feed(frame)

    # --- state machine (also driven directly by tests) ---
    def feed(self, frame: np.ndarray) -> None:
        """Process one mono float32 frame; drives the state machine."""
        if self._state in ("idle", "paused"):
            return
        pcm = np.clip(frame, -1.0, 1.0).astype(np.float32)
        pcm16 = (pcm * 32767).astype(np.int16)

        if self._state == "armed":
            if self._wake.score(pcm16) < self._wake.threshold:
                return
            self._emit({"type": "wake_detected", "name": getattr(self._wake, "name", "")})
            self._begin_capture()
            return

        # capturing: buffer audio and watch for trailing silence.
        self._buffer.append(pcm.copy())
        if self._vad.is_speech(pcm16):
            self._silence_run = 0
            self._speech_frames += 1
        else:
            self._silence_run += 1

        enough_speech = self._speech_frames >= self._min_speech_frames
        if enough_speech and self._silence_run >= self._silence_frames:
            self._finish_capture()
        elif len(self._buffer) >= self._max_frames:
            self._finish_capture()

    def _begin_capture(self) -> None:
        self._reset_turn()
        self._wake.reset()  # don't re-trigger on the same phrase
        self._vad.reset()
        self._set_state("capturing")

    def _finish_capture(self) -> None:
        frames = self._buffer
        had_speech = self._speech_frames >= self._min_speech_frames
        self._reset_turn()
        # Sit out the coming turn so Jarvis can't wake on its own voice.
        self._set_state("paused", emit=False)
        if had_speech and frames:
            audio = np.concatenate(frames)
            if self._on_utterance is not None:
                self._on_utterance(audio)
        else:
            # Nothing intelligible followed the wake word: just re-arm.
            self.resume()

    def _reset_turn(self) -> None:
        self._buffer = []
        self._silence_run = 0
        self._speech_frames = 0

    def _set_state(self, state: str, *, emit: bool = True) -> None:
        self._state = state
        if emit:
            self._emit({"type": "listen_state", "state": state})

    def _emit(self, event: dict) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001 - a bad callback must not kill the loop
            logger.exception("wake listener event callback failed")


def _default_stream_factory(callback, sample_rate: int, block: int):
    import sounddevice as sd

    return sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        blocksize=block,
        callback=callback,
    )
