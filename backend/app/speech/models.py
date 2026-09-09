"""One-time downloads for the speech stack's model files.

faster-whisper pulls its own weights via huggingface_hub on first use
(cached under ~/.cache/huggingface). Kokoro's ONNX model + voices are not
on HuggingFace's Python-path though, so we fetch them from the kokoro-onnx
GitHub release into the configured model dir.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

# Same tag kokoro-onnx's own docs point at; files are ~325 MB and ~104 MB.
_KOKORO_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
_KOKORO_BASE_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
)


def ensure_kokoro_models(model_dir: str | Path) -> dict[str, Path]:
    """Download any missing Kokoro model files; returns their paths."""
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name in _KOKORO_FILES:
        target = model_dir / name
        if not target.exists() or target.stat().st_size == 0:
            url = f"{_KOKORO_BASE_URL}/{name}"
            print(f"Downloading {name} ({url}) …", file=sys.stderr)
            with httpx.stream("GET", url, follow_redirects=True, timeout=600) as resp:
                resp.raise_for_status()
                with open(target, "wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=1 << 20):
                        fh.write(chunk)
            print(f"Saved {target}", file=sys.stderr)
        paths[name] = target
    return paths