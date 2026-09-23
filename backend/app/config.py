"""Central configuration for the Jarvis backend.

All settings are environment-driven with sane defaults so the service
runs out of the box against a local Ollama install.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JARVIS_", env_file=".env", extra="ignore")

    # --- server ---
    host: str = "127.0.0.1"
    port: int = 8000

    # --- LLM (Phase 0: local Ollama via its OpenAI-compatible API) ---
    llm_provider: str = "ollama"  # "ollama" for now; cloud providers come in Phase 4
    ollama_base_url: str = "http://localhost:11434"
    # Don't hardcode a model choice into the plan; this is just the default.
    # Check `ollama.com/library` at build time and pick a small instruct model
    # with good tool-calling support, e.g. qwen3:4b / llama3.2:3b.
    llm_model: str = "qwen3:4b"
    request_timeout_seconds: float = 120.0

    # --- memory (v1 scope guardrail: session-scoped buffer only) ---
    max_history_messages: int = 24
    # Phase 2.5: persist sessions to this JSON file so CLI chats survive a
    # backend restart. Empty string disables persistence (tests use this).
    history_path: str = ".jarvis_history.json"

    # --- tools (Phase 2.5: read-only brain/tool loop) ---
    # Sandbox root for filesystem tools: everything outside is unreachable.
    # Default is the repo root, resolved from this file, not the cwd.
    tool_root: str = str(Path(__file__).resolve().parents[2])

    # --- permissions (Phase 3: risk tiers + confirmation gate) ---
    # How long to wait for a human to answer a confirm_request before treating
    # it as a refusal. Without this a client that never replies would park the
    # turn (and the mic) forever.
    confirm_timeout_seconds: float = 120.0

    # Extra apps `open_app` may launch, as comma-separated `name=target` pairs
    # (e.g. "steam=steam,code=code"). Added to a built-in list; nothing off
    # the list can be opened.
    app_allowlist: str = ""

    # --- audit (Phase 3: append-only record of every tool call) ---
    # One JSON object per line. Empty string disables logging (tests use this).
    audit_path: str = ".jarvis_audit.jsonl"

    # --- speech (Phase 1: push-to-talk voice loop) ---
    # STT via faster-whisper. `device` may be "auto" (tries CUDA, falls back to
    # CPU), "cuda" (needs cuDNN/cuBLAS DLLs next to ctranslate2 on Windows),
    # or "cpu".
    whisper_model: str = "small"
    whisper_device: str = "auto"
    whisper_compute_type: str = "auto"  # auto | float16 | int8
    stt_sample_rate: int = 16000  # Whisper's native rate; recorder captures at this
    ptt_max_seconds: float = 30.0  # safety cap so a stuck button can't record forever

    # TTS via Kokoro-82M (kokoro-onnx). Voice names are `af_heart` (female) /
    # `am_michael` (male) style codes — see kokoro-onnx docs for the full list.
    tts_voice: str = "af_heart"
    tts_speed: float = 1.0
    tts_model_dir: str = ".models"  # where kokoro-v1.0.onnx + voices-v1.0.bin live

    # --- wake word / always-listening (Phase 2) ---
    # openWakeWord pretrained model name (e.g. `hey_jarvis`, `alexa`). Weights are
    # fetched into the package's resources dir on first use (a few MB).
    wake_enabled: bool = True
    wake_word: str = "hey_jarvis"
    wake_threshold: float = 0.5  # score in [0, 1]; openWakeWord's suggested default
    # openWakeWord's bundled Silero VAD gates wake detections (0 disables it).
    wake_vad_threshold: float = 0.5
    wake_frame_ms: int = 80  # openWakeWord's native frame: 1280 samples @ 16 kHz
    # After a wake hit, keep capturing until this much trailing silence.
    wake_silence_seconds: float = 0.8
    wake_max_seconds: float = 15.0  # cap a single spoken turn
    wake_min_speech_seconds: float = 0.2  # ignore wake hit if nothing follows
    # Endpointing VAD for the captured utterance: "energy" (no extra deps) or
    # "silero" (openWakeWord's bundled Silero VAD).
    vad_engine: str = "energy"
    vad_energy_threshold: float = 0.012  # RMS on float32 audio in [-1, 1]


@lru_cache
def get_settings() -> Settings:
    return Settings()
