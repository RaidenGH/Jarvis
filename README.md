# Jarvis

Local Jarvis that can work offline and help you on your devices.

**Status: Phase 3** — text chat, a push-to-talk voice loop, and an
always-listening mode: flip the dock to **Wake**, say "Hey Jarvis", and speak
without touching anything. Offline STT via faster-whisper, offline TTS via Kokoro-82M,
wake word + VAD via openWakeWord, brain via local Ollama — with a working
**tool-calling agent loop** with real device tools, a terminal front-end
(`jarvis_cli.py`), and the **permission layer**: every tool carries a risk tier,
anything above read-only has to be approved by you before it runs, and every
call is written to an append-only audit log. See `docs/PLAN.md` for the full
roadmap.

```
app/       Flutter Windows shell: the glass HUD from `New UI.html` — a state
           orb, a transcript drawer, inline tool activity and the approval
           dialog — talking to the backend over WebSocket.
           app/lib/hud.dart      backdrop + the radial-bar orb (idle /
                                 listening / thinking / speaking / offline)
           app/lib/theme.dart    every colour, radius and text style
           app/lib/transcript.dart  frame-to-view-model rules
           app/lib/palette.dart     command-palette ranking
           app/lib/reconnect.dart   reconnect backoff schedule
           app/lib/health.dart      parsed GET /health
           palette/reconnect/health/transcript are Flutter-free and unit
           tested: `cd app && flutter test`
           fonts: Space Grotesk + JetBrains Mono, both SIL OFL, bundled in
           `app/assets/fonts/`
jarvis_cli.py   Terminal front-end to the same agent loop (Phase 2.5)
backend/   FastAPI service (session memory + LLM + speech pipeline)
backend/app/agent.py   agent loop: LLM → decide → call tool → respond
backend/app/tools/     allow-listed tools, each tagged with a risk tier.
                      Read-only: system_stats, list_dir, read_file,
                      search_files, grep_files, update_plan.
                      Confirm-first: write_file, edit_file, run_checks,
                      open_app, change_volume — run_checks runs only the
                      project's own test/lint commands, from a fixed menu.
                      base.py holds the tier→policy table
backend/app/audit.py   append-only JSONL record of every tool call
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
3. **Ollama** — https://ollama.com/download, then pull an instruct model with
   good tool-calling support:
   ```bash
   ollama pull qwen2.5:7b   # 7B+ holds the agent loop; see ollama.com/library
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

Hold the mic (or hold the space bar) and speak — Jarvis replies in text and,
for voice, aloud through your speakers. Switch the dock toggle from **Hold**
to **Wake** to always-listen and just say "Hey Jarvis".

**Typing:** the mock has no visible message box, so the composer *is* the
command palette — start typing anywhere (or press `Ctrl+K`) and the first row
becomes `Ask Jarvis: <what you typed>`; `Enter` sends it.

| Shortcut | Does |
|---|---|
| hold `Space` | push-to-talk (when you aren't typing) |
| any letter | opens the composer with that keystroke |
| `Ctrl+K` | commands + suggested prompts |
| `Ctrl+,` | settings: backend, speech pipeline, tool risk tiers |
| `Esc` | close an overlay, or collapse the transcript |

A dead backend no longer needs a manual reconnect: the shell retries on an
exponential backoff (2s → 32s) and tells you when the next attempt is.

### CLI — an agent that works on the repo, not just a chat

With the backend running, open a third terminal:

```bash
backend/.venv/Scripts/python jarvis_cli.py     # needs websockets, which the venv has
```

Give it a job in plain language and it works the way you would: list

directories to get oriented, grep for the symbol, read what it needs, write its
plan down, make the edits, run the project's checks, and then tell you what
changed — including what it could *not* verify.

```bash
backend/.venv/Scripts/python jarvis_cli.py "find why the transcript test fails and fix it"
backend/.venv/Scripts/python jarvis_cli.py "add a --dry-run flag to the volume tool"
```

A positional task runs once and exits (non-zero if the backend reported an
error); omit it for an interactive session.

| Flag | Does |
|---|---|
| `--model NAME` | run on another local model, e.g. `qwen2.5:7b` |
| `--yes` | pre-approve the tap tiers (edits + checks), so a multi-file change runs unattended |
| `--allow TIER` | pre-approve one named tier, repeatable |
| `--verbose` | full tool output instead of one-line summaries |
| `--new` | start from an empty conversation |

**Approvals.** Read-only tools run silently. Anything that writes or runs code
stops and shows you what it is about to do — an edit arrives as a diff — then
waits: `y` for once, `n` to refuse, `a` to allow that tier for the rest of the
run. Destructive tiers never get the `a` shortcut; they make you retype a
challenge phrase, which is what stops a stray `y` from approving a delete. A
refusal is reported as `✋ edit_file did not run`, and the model is told not to
work around it.

**Commands:** `/tools` (each tool with its tier), `/plan` (the agent's todo
list), `/audit` (recent tool calls from the audit log), `/model [name]`,
`/verbose`, `/reset`, `/help`, `/quit`.

History is persisted by the backend to `.jarvis_history.json`, so a session
survives a restart (`JARVIS_HISTORY_PATH=` disables it).

**Use a model that can hold a loop.** Tool-calling degrades badly below ~7B: a
small model will describe an edit instead of making it, or announce a
"pytest passed" it never ran. The CLI names the model in its banner, warns when
it looks too small for agent work, and prints `no tools ran — nothing on disk
changed` at the end of any turn that called nothing, so a fabricated result is
obvious instead of convincing.

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
| `JARVIS_LLM_MODEL` | `qwen2.5:7b` | Local model name |
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
| `JARVIS_APP_ALLOWLIST` | *(empty)* | Extra apps `open_app` may launch, as `name=target` pairs |
| `JARVIS_AUDIT_PATH` | `.jarvis_audit.jsonl` | Where tool calls are logged (empty disables) |

## API surface

- `GET /health` — liveness + configured provider/model + tools and their
  risk tiers + where the audit log lives
- `GET /audit?limit=20` — the most recent tool calls, oldest first, including
  the ones that were declined or are disabled
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

## Troubleshooting

**Voice works but transcription runs on the CPU.** `JARVIS_WHISPER_DEVICE=auto`
(the default) builds a CUDA model when one looks available, probes it with a
quarter second of silence, and falls back to CPU int8 if that probe fails. On
Windows the usual cause is `cublas64_12.dll` / cuDNN not being on the search
path next to `ctranslate2` — which only surfaces on the first *encode*, never
at construction. You'll see
`Whisper on auto failed its first encode … falling back to the CPU` in the
backend log; results are identical, just slower. Install the CUDA 12 runtime
libraries to keep the GPU, or pin `JARVIS_WHISPER_DEVICE=cpu` to skip the probe.

## Tests

```bash
cd backend && .venv/Scripts/pytest
```

Tests stub the LLM — they never need Ollama running.
