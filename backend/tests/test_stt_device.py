"""The `whisper_device = "auto"` contract: CUDA if it works, CPU otherwise.

The failure this pins down is nasty because construction *succeeds* on a
machine without cuBLAS: the model builds, then the first `transcribe()` dies
with `Library cublas64_12.dll is not found or cannot be loaded`. Anything that
only catches the constructor never falls back, and every voice turn fails.
"""

import numpy as np

from app.speech.stt import STTEngine


class FlakyEngine:
    """Encodes like CUDA without cuBLAS: fine until you ask it to work."""

    def __init__(self, fail: bool):
        self.fail = fail

    def transcribe(self, audio, beam_size=5):
        if self.fail:
            raise RuntimeError(
                "Library cublas64_12.dll is not found or cannot be loaded"
            )
        return [], None


class FakeEngine:
    def __init__(self, fail: bool):
        self.engine = FlakyEngine(fail)

    def transcribe(self, audio, beam_size=5):
        return self.engine.transcribe(audio, beam_size)


class FakeWhisperModel:
    """Stand-in for `faster_whisper.WhisperModel`.

    `failing` decides which devices produce a model that can't encode, and
    `raising` which devices fail at construction.
    """

    def __init__(self, failing=lambda device: device != "cpu", raising=lambda _d: False):
        self.failing = failing
        self.raising = raising
        self.built = []

    def __call__(self, model, device="auto", compute_type="auto"):
        self.built.append((model, device, compute_type))
        if self.raising(device):
            raise RuntimeError(f"{device} unavailable")
        return FakeEngine(fail=self.failing(device))

    @property
    def devices(self) -> list[str]:
        return [device for _model, device, _compute in self.built]


def engine_under_test(device: str = "auto") -> STTEngine:
    # Skip the real model download; `__init__` records the requested device
    # up front and `_load` corrects it if it has to fall back.
    engine = STTEngine.__new__(STTEngine)
    engine._sample_rate = 16_000
    engine._device = device
    return engine


def test_auto_keeps_a_model_that_can_encode():
    whisper = FakeWhisperModel(failing=lambda _device: False)
    engine = engine_under_test()

    engine._model = engine._load(whisper, "small", "auto", "auto")

    assert whisper.devices == ["auto"]
    assert engine.device == "auto"


def test_auto_falls_back_to_cpu_when_the_first_encode_fails():
    whisper = FakeWhisperModel()  # only the CPU build can encode
    engine = engine_under_test()

    engine._model = engine._load(whisper, "small", "auto", "auto")

    assert whisper.devices == ["auto", "cpu"]
    assert engine.device == "cpu"
    assert engine._encodes(engine._model) is True


def test_a_build_that_explodes_outright_still_falls_back():
    whisper = FakeWhisperModel(raising=lambda device: device == "cuda")
    engine = engine_under_test()

    engine._model = engine._load(whisper, "small", "cuda", "float16")

    assert whisper.devices == ["cuda", "cpu"]
    assert engine.device == "cpu"


def test_a_working_cpu_model_is_left_alone():
    whisper = FakeWhisperModel()
    engine = engine_under_test("cpu")

    engine._model = engine._load(whisper, "small", "cpu", "int8")

    assert whisper.devices == ["cpu"]
    assert engine.device == "cpu"
    assert engine.transcribe(np.zeros(800, dtype=np.float32)) == ""
