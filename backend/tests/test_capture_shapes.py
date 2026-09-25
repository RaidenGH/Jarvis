"""Waveform-shape regressions for the capture path.

sounddevice's callback delivers `(frames, channels)` blocks even with
`channels=1`. Both ends of the push-to-talk path used to pass that 2-D block
straight through, and faster-whisper read it as `(batch, samples)` — a
five-second utterance became ~76k one-sample clips and numpy was asked for a
68 GiB mel spectrogram, killing the turn. These tests pin the shape without
needing PortAudio or a model download.
"""

import threading

import numpy as np
import pytest

from app.speech.audio import MicRecorder
from app.speech.stt import STTEngine


def recorder_with(frames: list[np.ndarray]) -> MicRecorder:
    """A MicRecorder with no audio device, primed with buffered frames."""
    recorder = MicRecorder.__new__(MicRecorder)
    recorder._stream = None
    recorder._lock = threading.Lock()
    recorder._frames = frames
    recorder._max_frames = 16_000
    recorder._sample_rate = 16_000
    return recorder


class RecordingModel:
    """Stands in for WhisperModel, keeping whatever it was asked to decode."""

    def __init__(self):
        self.audio = None

    def transcribe(self, audio, beam_size=5):
        self.audio = audio
        return [], None


def test_stop_flattens_the_channel_axis():
    # What sounddevice actually hands the callback for a mono stream.
    block = np.arange(32, dtype=np.float32).reshape(32, 1)
    waveform = recorder_with([block, block]).stop()

    assert waveform.ndim == 1
    assert waveform.shape == (64,)
    assert waveform.dtype == np.float32
    assert waveform[0] == 0 and waveform[-1] == 31


def test_stop_returns_an_empty_waveform_when_nothing_was_captured():
    waveform = recorder_with([]).stop()

    assert waveform.ndim == 1
    assert waveform.size == 0


def test_stt_hands_whisper_a_mono_waveform():
    engine = STTEngine.__new__(STTEngine)
    engine._sample_rate = 16_000
    engine._model = RecordingModel()

    engine.transcribe(np.zeros((800, 1), dtype=np.float32))

    assert engine._model.audio.ndim == 1
    assert engine._model.audio.shape == (800,)


def test_stt_returns_nothing_for_silence_instead_of_decoding():
    engine = STTEngine.__new__(STTEngine)
    engine._sample_rate = 16_000
    engine._model = RecordingModel()

    assert engine.transcribe(np.zeros(0, dtype=np.float32)) == ""
    assert engine.transcribe(None) == ""
    assert engine._model.audio is None


@pytest.mark.parametrize("dtype", [np.float32, np.float64, np.int16])
def test_stt_accepts_whatever_numeric_dtype_arrives(dtype):
    engine = STTEngine.__new__(STTEngine)
    engine._sample_rate = 16_000
    engine._model = RecordingModel()

    engine.transcribe(np.zeros(1_600, dtype=dtype))

    assert engine._model.audio.dtype == np.float32
