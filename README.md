# Jarvis

Local Jarvis that can work offline and help you on your devices.

**Status: Phase 0** — text chat from a Flutter Windows shell through a local
Python backend, answered by a local LLM (Ollama). See `docs/PLAN.md` for the
full roadmap.

```
app/       Flutter Windows shell (chat UI, talks to backend over WebSocket)
backend/   FastAPI service (session memory + LLM orchestration)
docs/      Development plan
```

## Prerequisites

1. **Python 3.12+** (developed on 3.14)
2. **Flutter SDK** with Windows desktop support (`flutter doctor`)
3. **Ollama** — https://ollama.com/download, then pull a small instruct model:
   ```bash
   ollama pull qwen3:4b   # or any model with good tool-calling support; check ollama.com/library
   ```

## Run it

Terminal 1 — backend:

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"     # PowerShell: use .venv\Scripts\
.venv/Scripts/uvicorn app.main:app --port 8000
```

Terminal 2 — Flutter shell (first time only, generate the Windows platform folder):

```bash
cd app
flutter create --platforms=windows .
flutter pub get
flutter run -d windows
```

Type in the app → streamed reply from the local model.

## Configuration (env vars, prefix `JARVIS_`)

| Variable | Default | Purpose |
|---|---|---|
| `JARVIS_LLM_PROVIDER` | `ollama` | Brain selection (cloud comes in Phase 4) |
| `JARVIS_OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint |
| `JARVIS_LLM_MODEL` | `qwen3:4b` | Local model name |
| `JARVIS_PORT` | `8000` | Backend port |

## API surface

- `GET /health` — liveness + configured provider/model
- `POST /chat` `{text, session_id}` → one-shot reply (easy curl testing)
- `WS /ws/{session_id}` — send `{"type":"user_message","text":"..."}`, receive
  `{"type":"token","text":...}` frames then `{"type":"done"}`;
  send `{"type":"reset"}` to clear the session

## Tests

```bash
cd backend && .venv/Scripts/pytest
```

Tests stub the LLM — they never need Ollama running.
