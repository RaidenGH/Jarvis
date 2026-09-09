# Jarvis

Local Jarvis that can work offline and help you on your devices.

**Status: Phase 1** — text chat + a push-to-talk voice loop from a Flutter
Windows shell through a local Python backend: hold the mic button, speak,
and Jarvis replies aloud through the speakers (offline STT via faster-whisper,
offline TTS via Kokoro-82M, brain via local Ollama). See `docs/PLAN.md` for
the full roadmap.

```
app/       Flutter Windows shell (chat UI, talks to backend over WebSocket)
backend/   FastAPI service (session memory + LLM + speech pipeline)
backend/app/speech/   STT (faster-whisper), TTS (Kokoro-onnx), mic/speakers (sounddevice)
docs/      Development plan
```

On first voice use the backend downloads its speech models (~900 MB total):
faster-whisper `small` into the HuggingFace cache, and the Kokoro ONNX model
into `backend/.models/` (gitignored).

## Prerequisites

1. **Python 3.12+** (developed on 3.14)
2. **Flutter SDK** with Windows desktop support (`flutter doctor`)
3. **Ollama** — https://ollama.com/download, then pull a small instruct model:
   ```bash
   ollama pull qwen3:4b   # or any model with good tool-calling support; check ollama.com/library
   ```

## Run it

0. **Ollama** (once per boot, or it autostarts):
   ```bash
   ollama serve
   ```

Terminal 1 — backend:

```bash
cd backend
.venv/Scripts/uvicorn app.main:app --port 8000
```

Terminal 2 — Flutter shell (already built — either works):

```bash
cd app
./build/windows/x64/runner/Debug/jarvis.exe   # quick: launch the built exe
# or: flutter run -d windows                   # dev mode with hot reload
```

Type a message, or hold 🎤 and speak — Jarvis replies in text and, for voice,
aloud through your speakers.

### First-time setup (only if `.venv` / `windows/` are missing)

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"     # PowerShell: use .venv\Scripts\

cd ../app
flutter create --platforms=windows .
flutter pub get
```

## Configuration (env vars, prefix `JARVIS_`)

| Variable | Default | Purpose |
|---|---|---|
| `JARVIS_LLM_PROVIDER` | `ollama` | Brain selection (cloud comes in Phase 4) |
| `JARVIS_OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint |
| `JARVIS_LLM_MODEL` | `qwen3:4b` | Local model name |
| `JARVIS_PORT` | `8000` | Backend port |
| `JARVIS_WHISPER_MODEL` | `small` | STT model size (`tiny`/`base`/`small`) |
| `JARVIS_WHISPER_DEVICE` | `auto` | `auto` (CUDA→CPU fallback), `cuda`, `cpu` |
| `JARVIS_TTS_VOICE` | `af_heart` | Kokoro voice id (e.g. `am_michael`) |
| `JARVIS_PTT_MAX_SECONDS` | `30` | Safety cap on a held recording |

## API surface

- `GET /health` — liveness + configured provider/model
- `POST /chat` `{text, session_id}` → one-shot reply (easy curl testing)
- `WS /ws/{session_id}` — send `{"type":"user_message","text":"..."}`, receive
  `{"type":"token","text":...}` frames then `{"type":"done"}`;
  send `{"type":"reset"}` to clear the session
- **Push-to-talk:** send `{"type":"ptt_start"}`, then `{"type":"ptt_stop"}`.
  The backend records the mic, streams `{"type":"transcript","text":...}`,
  replies via the normal token stream, then plays the spoken reply
  (`tts_start`/`tts_done` frames) through the speakers

## Tests

```bash
cd backend && .venv/Scripts/pytest
```

Tests stub the LLM — they never need Ollama running.
