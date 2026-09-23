# Jarvis

Local Jarvis that can work offline and help you on your devices.

**Status: Phase 3** — text chat, a push-to-talk voice loop, and an
always-listening mode: tap the ear, say "Hey Jarvis", and speak without
touching anything. Offline STT via faster-whisper, offline TTS via Kokoro-82M,
wake word + VAD via openWakeWord, brain via local Ollama — with a working
**tool-calling agent loop**, a terminal front-end (`jarvis_cli.py`), and the
first piece of the **permission layer**: every tool carries a risk tier and
anything above read-only has to be approved by you before it runs. See
`docs/PLAN.md` for the full roadmap.

```
app/       Flutter Windows shell (chat UI, talks to backend over WebSocket)
jarvis_cli.py   Terminal front-end to the same agent loop (Phase 2.5)
backend/   FastAPI service (session memory + LLM + speech pipeline)
backend/app/agent.py   agent loop: LLM → decide → call tool → respond
backend/app/tools/     allow-listed tools, each tagged with a risk tier
                      (system_stats, read_file — both read-only, so both run
                      without prompting; base.py holds the tier→policy table)
backend/app/speech/   STT (faster-whisper), TTS (Kokoro-onnx), mic (sounddevice),
                      wake word (openWakeWord), VAD endpointing
docs/      Development plan
```

On first voice use the backend downloads its speech models (~900 MB total):
faster-whisper `small` into the HuggingFace cache, and the Kokoro ONNX model
into `backend/.models/` (gitignored). Always-listening pulls a few extra MB of
openWakeWord weights (the pretrained model plus its shared feature/VAD models)
into openWakeWord's own package directory on first use.

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
aloud through your speakers. Or tap the ear icon to always-listen and just say
"Hey Jarvis".

### CLI (Phase 2.5) — watch the brain call tools

With the backend running, open a third terminal:

```bash
backend/.venv/Scripts/python jarvis_cli.py     # needs websockets, which the venv has
```

Ask e.g. "what are my system stats?" or "read app/lib/main.dart" and you'll
see the tool call (`⚙ system_stats({})`), its result, then the answer.
If you ask for something above the read-only tier, the CLI stops mid-turn and
asks you to approve it (`⚠ set_volume({...})`) — `y/N` for the one-tap tiers,
or an exact retype of the challenge phrase for destructive ones. Answer no and
you'll see `✋ set_volume did not run`. `/tools` now lists each tool with its
risk tier.

Commands inside the chat: `/tools`, `/reset`, `/quit`. `--new` clears the
session's history on startup. Conversation history is persisted by the
backend to `.jarvis_history.json`, so both CLI and Flutter chats survive a
backend restart (`JARVIS_HISTORY_PATH=` disables it).

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
| `JARVIS_WAKE_ENABLED` | `true` | Enable always-listening wake-word mode |
| `JARVIS_WAKE_WORD` | `hey_jarvis` | openWakeWord pretrained model name |
| `JARVIS_WAKE_THRESHOLD` | `0.5` | Wake sensitivity (lower = triggers more easily) |
| `JARVIS_WAKE_VAD_THRESHOLD` | `0.5` | Silero VAD gate on detections (0 disables) |
| `JARVIS_WAKE_SILENCE_SECONDS` | `0.8` | Trailing silence that ends a spoken turn |
| `JARVIS_VAD_ENGINE` | `energy` | Endpointing VAD: `energy` or `silero` |
| `JARVIS_HISTORY_PATH` | `.jarvis_history.json` | Where sessions persist between runs (empty disables) |
| `JARVIS_TOOL_ROOT` | repo root | Sandbox root for `read_file` — nothing outside is reachable |
| `JARVIS_CONFIRM_TIMEOUT_SECONDS` | `120` | How long to wait for an approval before treating it as a refusal |

## API surface

- `GET /health` — liveness + configured provider/model + tool list
- `POST /chat` `{text, session_id}` → one-shot reply (easy curl testing)
- `WS /ws/{session_id}` — send `{"type":"user_message","text":"..."}`, receive
  `{"type":"token","text":...}` frames then `{"type":"done"}`;
  send `{"type":"reset"}` to clear the session
- **Agent loop:** every text turn may call allow-listed tools; before the
  answer the backend interleaves `{"type":"tool_call","name","arguments"}` and
  `{"type":"tool_result","name","result"}` frames (clients that don't know
  them just ignore them)
- **Permissions (risk tiers):** each tool has a tier — `read-only`,
  `reversible-write`, `destructive`, `external-facing` — and `GET /health`
  reports them as `tool_risks`. Read-only tools just run. Anything above that
  triggers `{"type":"confirm_request","name","arguments","risk","mode",
  "challenge"}`; the client answers `{"type":"confirm_response","approved":
  bool,"challenge":"..."}`. `mode` is `tap` (approve/deny) or `typed`, where
  `approved` is only honored together with an exact echo of `challenge` — so a
  stray "yes" can't satisfy a destructive action. If the answer is no (or
  nothing comes back within the timeout) the call never runs and the client
  gets `{"type":"tool_denied","name","reason"}`. `external-facing` tools are
  disabled outright and are never even asked about. Over REST `/chat` there is
  nobody to ask, so every gated tool is declined automatically
- **Push-to-talk:** send `{"type":"ptt_start"}`, then `{"type":"ptt_stop"}`.
  The backend records the mic, streams `{"type":"transcript","text":...}`,
  replies via the normal token stream, then plays the spoken reply
  (`tts_start`/`tts_done` frames) through the speakers
- **Always-listening:** send `{"type":"listen_start"}` to arm the wake word and
  `{"type":"listen_stop"}` to disarm. The backend reports
  `{"type":"listen_state","state":"armed"|"capturing"|"idle"}`, streams
  `{"type":"wake_detected","name":"hey_jarvis"}` on a detection, captures until
  you stop talking, then runs the same transcript → token → TTS → `done` flow and
  re-arms. Push-to-talk and wake listening share the mic and are mutually
  exclusive — starting one stops the other

## Tests

```bash
cd backend && .venv/Scripts/pytest
```

Tests stub the LLM — they never need Ollama running.
