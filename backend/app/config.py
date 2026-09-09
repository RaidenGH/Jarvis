"""Central configuration for the Jarvis backend.

All settings are environment-driven with sane defaults so the service
runs out of the box against a local Ollama install.
"""

from functools import lru_cache

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
