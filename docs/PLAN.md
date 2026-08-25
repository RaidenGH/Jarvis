# Personal AI Assistant ("Jarvis-Style") — Development Plan

**Prepared for:** a solo developer with Python, Flutter/Dart, and Windows 11 experience
**Scope:** always-available voice + text assistant with deep, permissioned control of a Windows PC, working both online (cloud LLM) and offline (local LLM)

---

## 1. Project Scope & Goals

### 1.1 Vision statement
A single assistant, reachable by voice or text, that (a) reasons like a capable general-purpose LLM, (b) can see and act on your Windows 11 machine within limits you define, and (c) keeps working — in a degraded but real way — with no internet connection.

### 1.2 Core capability pillars

| Pillar | v1 definition of "done" |
|---|---|
| **Conversational core** | Natural back-and-forth by voice or typed text, with short-term memory of the current session |
| **Voice I/O** | Wake-word or push-to-talk capture → transcription → spoken reply, under ~2–3s perceived latency locally |
| **Device control** | A defined, growing library of *specific* actions (open an app, adjust volume, read system stats, search a folder, create a reminder) — not open-ended shell access |
| **Online/offline duality** | Same assistant, same tool library, running against either a cloud LLM (best quality, needs internet) or a local LLM (private, always available, lower quality) |
| **Safety & consent** | Every action is classified by risk tier; anything beyond read-only requires a visible confirmation step; every action is logged |

### 1.3 Explicit non-goals for v1 (scope guardrails)

Being explicit about what you're *not* building first is what keeps a project like this from stalling out. Recommended non-goals until later phases:

- No unrestricted shell/PowerShell execution from natural language — only pre-defined, parameterized actions.
- No always-on raw audio recording — only buffered audio around a wake event.
- No autonomous background actions (the assistant doesn't independently browse, message people, or run scheduled agentic tasks) until the permission/audit system in Phase 3 exists.
- No multi-user support initially — assume one user, one machine.
- No mobile app in the first build (your Flutter skills make this a very natural v2/v3 add-on, not a blocker for v1).

### 1.4 "Jarvis" framing, concretely

Think of the end state as three cooperating processes on your PC:

1. A **Flutter Windows app** — the face: chat/voice UI, tray icon, permission dialogs, settings.
2. A **Python backend service** — the brain and hands: speech pipeline, LLM orchestration, tool execution, running as a local background process the Flutter app talks to over `localhost`.
3. A **tool/action layer** exposed via [Model Context Protocol (MCP)](https://modelcontextprotocol.io) servers — the hands' fingers: discrete, named, schema-checked actions the brain is allowed to call, regardless of whether the brain is a cloud model or a local one.

---

## 2. Technical Architecture

### 2.1 Layered overview

```
┌─────────────────────────────────────────────────────────────┐
│  Flutter Windows Shell (Dart)                                │
│  chat/voice UI · tray icon · consent dialogs · settings      │
└───────────────────────────┬─────────────────────────────────┘
                            │ localhost REST/WebSocket
┌───────────────────────────▼─────────────────────────────────┐
│  Python Backend Service (FastAPI, runs as background process)│
│                                                              │
│  ┌───────────────┐   ┌────────────────┐   ┌──────────────┐  │
│  │ Voice Pipeline │   │ Brain/Reasoning│   │ Permission & │  │
│  │ wake→VAD→STT→  │◄─►│ local/cloud LLM│◄─►│ Audit Layer  │  │
│  │ TTS            │   │ + agent loop   │   │ (risk tiers, │  │
│  └───────────────┘   └───────┬────────┘   │ confirmations│  │
│                              │            │ logging)     │  │
│                      ┌───────▼────────┐   └──────────────┘  │
│                      │ MCP Tool Layer │                     │
│                      │ (device-control│                     │
│                      │  actions)      │                     │
│                      └───────┬────────┘                     │
└──────────────────────────────┼───────────────────────────────┘
                               │
                ┌──────────────┼──────────────┐
                ▼              ▼              ▼
         Windows APIs /    GUI automation   External APIs
         PowerShell        (pywinauto/UIA)  (weather, smart
         (scoped)          for legacy apps  home, calendar)
```

### 2.2 Voice pipeline

| Stage | Recommended tool | Why | Fallback / alternative |
|---|---|---|---|
| Wake word | **openWakeWord** (open source, self-trainable, MIT-style license) | Free, fully offline, runs comfortably on CPU | **Picovoice Porcupine** — better out-of-the-box false-accept rates, but commercial use beyond the free tier needs a paid license |
| Voice activity detection | **Silero VAD** or `webrtcvad` | Trims silence so you're not transcribing dead air; also used to detect "user stopped talking" | — |
| Speech-to-text (offline) | **faster-whisper** (CTranslate2) if you have an NVIDIA GPU; **whisper.cpp** if CPU-only | faster-whisper is ~4x faster than reference Whisper on GPU with INT8 quantization; whisper.cpp is a dependency-free CPU/edge path | Cloud STT (Azure/Google/OpenAI) as an "online mode" accuracy boost |
| Text-to-speech (offline) | **Kokoro-82M** (Apache-2.0, ~2–3GB VRAM or CPU, 54 voices) as the default; **Piper** where you need the lowest possible latency (near-instant first audio, more robotic tone) | Kokoro is the current sweet spot for natural-sounding local voices on modest hardware | Cloud TTS (ElevenLabs, Azure) for "online mode" expressiveness |
| Audio capture | `sounddevice` or `pyaudio` (Python) | Simple cross-platform mic capture, integrates directly with your STT pipeline | — |

**Latency budget note:** wake-word detection and VAD are essentially instant; STT and TTS are the real cost. On a mid-range GPU, small/base Whisper models plus Kokoro TTS should land you well under 2 seconds end-to-end for short utterances — plan your model-size choices around this budget rather than always reaching for the largest model.

### 2.3 Brain / reasoning layer

- **Orchestrator:** a Python `FastAPI` service is the natural fit given your background — it hosts a WebSocket endpoint for the Flutter app (streaming partial responses/audio) and internally runs an agent loop: transcribed input → decide (answer directly / call a tool / ask for clarification) → act → respond.
- **Online path:** the Claude API (or another frontier LLM API) via its native tool-use/function-calling support. This is your best-quality reasoning path when connected; check current models and tool-use docs at `docs.claude.com` since offerings change.
- **Offline path:** **Ollama** as the local runtime — it's the lowest-friction option for a solo developer (`ollama pull <model>`, then an OpenAI-compatible API at `localhost:11434`), and most agent/LLM libraries work with it by just changing the base URL. Pick a currently-available instruct model in Ollama's library with good tool-calling support (this list changes fast — check `ollama.com/library` at build time rather than hardcoding a model name into your plan).
  - Step down to raw **llama.cpp** later only if you need something Ollama's abstractions don't expose (custom quantization, embedding the engine directly in a shipped binary).
- **Mode arbitration:** detect connectivity, let the user override the mode manually, and define a policy for what always stays local (e.g., anything touching personal files, calendar, or the mic transcript) versus what may escalate to cloud (general knowledge questions) — this is a privacy decision as much as a technical one, and you should write it down explicitly rather than leaving it implicit in code.
- **Tool/function-calling schema:** every device action is a named function with a strict JSON schema — never a field where the model can put a free-form shell string. This single design choice does more for safety than almost anything else in the stack.
- **Standardizing tools via MCP:** expose your device-control actions as **MCP servers** (Python's `fastmcp` library is the fast path) rather than bespoke glue code per LLM provider. MCP is now a Linux-Foundation-governed open standard with native support across Anthropic, OpenAI, and Google's APIs, which means the same tool layer works whether the "brain" of the moment is your local Ollama model or a cloud model — you write the tool once.
- **Memory (v2+):** start with a simple in-memory/session conversation buffer. Add a persistent memory layer (SQLite for structured facts, a local vector store like Chroma or LanceDB for semantic recall) only once the core loop is solid — this is a classic scope trap if pulled in too early.

### 2.4 Device control layer

Order these strategies from most to least preferred — reach for the next one only when the previous one can't do the job:

1. **Structured APIs (preferred):** Windows APIs, PowerShell cmdlets called with fixed, allow-listed arguments, and app-specific APIs where they exist (e.g., a smart-home hub's REST API). Deterministic, auditable, hard to misuse.
2. **GUI automation (fallback for apps with no API):** `pywinauto` on top of the Windows UI Automation backend — this is the same approach Microsoft's own research agent (UFO) uses for controlling arbitrary Windows apps. More brittle across app updates and screen states; reserve for a whitelist of explicitly-supported apps and actions.
3. **Low-level system control:** `pywin32` / `ctypes` for volume, brightness, power state, window focus/placement. `keyboard`/`pyautogui` for simulated input only as a last resort, and only for whitelisted, low-risk actions.
4. **Companion mobile control (later phase):** since you already know Flutter, a small companion phone app (notifications relay, location, calendar) talking to the same MCP tool layer is a natural, low-cost extension once the desktop core is stable — don't start here.

### 2.5 Permissions / safety layer

- **Risk tiers** for every action: `read-only` → `reversible-write` → `destructive/irreversible` → `external-facing` (sends money, messages, or data outside your machine). Each tier maps to a UX requirement (silent / one-tap confirm / typed confirm / disabled by default).
- **Least privilege:** the backend service runs as a normal user, not admin. Anything genuinely requiring elevation triggers a real Windows UAC prompt rather than the service running elevated all the time.
- **Sandboxing / egress control:** tool execution should be scoped to specific folders (no arbitrary filesystem access) and, where possible, restricted to an allow-list of outbound hosts — this is the standard 2026 mitigation for prompt-injection-driven data exfiltration (see §4).
- **Untrusted content discipline:** anything the assistant reads that didn't come directly from you — a webpage, an email body, a file it opened — is *data*, never *instructions*. Tool calls should never be triggered directly by text found inside fetched content without passing back through your policy/confirmation layer.
- **Secrets management:** API keys and OAuth tokens go in Windows Credential Manager via the `keyring` Python package, not in a plaintext `.env` sitting next to the code.
- **Audit log:** every tool call — what was requested, by which reasoning path (local/cloud), what it did, and the result — written to a local, append-only log you can review.

---

## 3. Development Roadmap

Estimates assume a solo developer working part-time (roughly 8–12 hrs/week); adjust to your actual pace. Each phase ends with a concrete, demoable milestone rather than a vague "improve X."

| Phase | Focus | Target duration | Milestone |
|---|---|---|---|
| **0 — Foundations** | Repo structure, dev environment, Ollama installed with a first local model, minimal FastAPI backend, minimal Flutter Windows shell with a text chat box talking to the backend over `localhost` | 1–2 weeks | Type a message in the Flutter app, get a reply generated by the local LLM |
| **1 — MVP voice loop** | Push-to-talk (skip wake word for now), STT (faster-whisper/whisper.cpp), TTS (Kokoro/Piper) | 2–3 weeks | Press a button, speak, hear a spoken reply — a "walkie-talkie" assistant, no device actions yet |
| **2 — Always-listening mode** | openWakeWord + VAD integration, background service behavior, system tray via `tray_manager`/`window_manager` | 1–2 weeks | Say the wake word, it responds, the app lives minimized in the tray |
| **3 — First device tools + safety scaffolding** | Design the risk-tier model, build 3–5 initial low-risk MCP tools (open app, system stats, volume, reminders, search a whitelisted folder), build the confirmation UX, add the audit log | 3–4 weeks | "Open Chrome," "what's my CPU usage" work; anything beyond read-only shows a visible confirmation step |
| **4 — Online/offline hybrid brain** | Integrate the Claude API (or chosen cloud LLM) as the online tool-use path, connectivity detection, explicit local-vs-cloud policy | 2–3 weeks | The exact same assistant works with Wi-Fi off (lower quality, fully functional) and Wi-Fi on (higher quality) |
| **5 — Expanded device control** | GUI-automation tools (pywinauto) for apps without APIs, broader tool library (calendar, draft-only email with human-send confirmation, optional smart-home via Home Assistant), first pass at persistent memory | 3–5 weeks | A meaningfully larger action vocabulary, still fully audited and tiered |
| **6 — Hardening & packaging** | Red-team your own assistant (try prompt-injecting it via a malicious webpage/email it reads), tighten sandboxing, build a Windows installer (PyInstaller for the backend, Flutter Windows build, code signing), first-run permission wizard | 2–4 weeks | Something you could hand to a technical friend to install and use safely |
| **7+ — Stretch goals** | Flutter mobile companion, multiple wake words/personas, local fine-tuning/LoRA personalization, smart-home expansion, multi-user profiles | Ongoing | — |

**Sequencing principle:** voice before device control, and device control before online/offline duality. It's tempting to build the "coolest" pieces (wake word, cloud brain, wide device access) simultaneously, but each of those adds a dimension of debugging surface — building linearly means you always have a working, demoable thing.

---

## 4. Risk & Safety Considerations

An assistant with microphone access and device control has a materially different threat model from a chatbot. Treat these as first-class engineering requirements, not a checklist to revisit later.

### 4.1 Prompt injection is the top real-world risk in 2026

The [OWASP Top 10 for Agentic Applications](https://owasp.org/) ranks **Agent Goal Hijacking** as the #1 risk for exactly this class of system: an attacker plants instructions somewhere your assistant will read — a webpage, an email, a file — and the model follows them as if you'd said them. Documented 2026 incidents against agentic AI products have chained this into data exfiltration. Mitigations that matter most for a personal project:

- Never let content the assistant *reads* directly trigger a tool call — route it back through your confirmation/policy layer first.
- Restrict outbound network access from the tool-execution process to an allow-list.
- Keep destructive or external-facing actions behind explicit, hard-to-automate confirmation (typed confirmation or a PIN, not just "yes" in a voice reply an injected instruction could also produce).

### 4.2 Privacy and legal considerations of always-listening mic

- Wake-word systems buffer a rolling few seconds of audio locally and only act on a detected wake event — make sure your implementation actually discards that buffer rather than persisting raw audio.
- If anyone besides you is ever in the room, be aware that consent-to-record laws vary (one-party vs. two-party/all-party consent) by jurisdiction — this matters more once you're not the only person the mic might pick up.
- Give the assistant a visible, honest "listening" state (tray icon color, a persistent on-screen indicator) — don't rely on people trusting an invisible mic state.

### 4.3 Blast radius control

- Assume any single component (a bad model output, a compromised dependency, a malicious webpage) will eventually misbehave, and design so the damage it can do is bounded: scoped filesystem access, no standing admin rights, an allow-list of actions rather than an open-ended shell.
- Log everything. A system with device access that *can't* tell you what it did last Tuesday is not one you should trust with more access over time.

### 4.4 Supply-chain hygiene

You'll be pulling in a lot of actively-developed open-source components (wake-word models, STT/TTS engines, MCP servers, GUI-automation libraries). Pin versions, review before upgrading anything with device access, and keep an eye on the projects' security advisories — this is a normal part of running anything with real device control, not unique to AI assistants.

---

## 5. Resource Requirements

### 5.1 Hardware

| Tier | Setup | What it gets you |
|---|---|---|
| **Minimum (what you likely already have)** | Windows 11 PC, 16GB RAM, modern CPU, no dedicated GPU | Tiny/base Whisper models + a small quantized (Q4) 7–8B local LLM, tolerable but not snappy |
| **Recommended** | Add an NVIDIA GPU with 8–12GB+ VRAM (RTX 3060/4060/4070 class) | Comfortable faster-whisper STT + 7–13B local models with good headroom |
| **Enthusiast / larger local models** | 16–24GB+ VRAM (RTX 4080/4090, or a used RTX 3090 — a popular budget pick in 2026 for its 24GB) | Larger local models, more concurrent headroom, real-time transcription with bigger Whisper sizes |
| **Microphone** | A USB conference mic or a headset with built-in noise suppression | Meaningfully better STT accuracy than a laptop's built-in mic — this is an easy, cheap upgrade that pays off across the whole pipeline |

You don't need to buy anything to start Phase 0–2; the GPU tier only matters once you're pushing for lower STT/TTS latency or bigger local models.

### 5.2 Core library/tool cheat sheet

| Layer | Python | Dart/Flutter |
|---|---|---|
| Backend service | `fastapi`, `uvicorn`, `websockets` | — |
| Local LLM runtime | `ollama` (as a service, called via its OpenAI-compatible API) | — |
| Cloud LLM | Anthropic Python SDK (`anthropic`) for the online path | — |
| Wake word | `openwakeword` | — |
| VAD | `silero-vad` or `webrtcvad` | — |
| STT | `faster-whisper` (GPU) / `whisper.cpp` bindings (CPU) | — |
| TTS | `kokoro` + `soundfile`, or `piper-tts` | — |
| Audio I/O | `sounddevice` | — |
| Tool/agent layer | `fastmcp` (MCP server framework) | — |
| Windows automation | `pywinauto`, `pywin32`, `keyboard`/`pyautogui` (last resort) | — |
| Secrets | `keyring` | — |
| Packaging | `pyinstaller` (bundle the backend as an .exe sidecar) | Flutter Windows build tooling |
| Desktop UI shell | — | `window_manager`, `tray_manager`, a Fluent-UI-style package for a native Windows look |
| Backend↔UI transport | — | `web_socket_channel` or `http` for the localhost connection to FastAPI |

### 5.3 Optional ongoing costs

- Cloud LLM API usage for the "online" reasoning path — check current model lineup and pricing at `docs.claude.com` before budgeting, since both change over time.
- A Picovoice Porcupine license only if you outgrow openWakeWord's accuracy and want a commercial-grade wake word.
- Nothing else in this stack requires a subscription — the offline path is designed to be $0/month by default.

---

## 6. Skill Gaps to Close

Given your Python + Flutter/Dart + Windows 11 background, most of this plan sits inside skills you already have. The genuine gaps, roughly in the order you'll hit them:

1. **Real-time audio programming** — buffering, streaming, voice activity detection, and managing the "is the user still talking" state machine are a different discipline from typical request/response backend work. Budget real learning time here in Phase 1; it's the least Python-typical part of the stack.
2. **Agent/tool-use design patterns** — writing good JSON-schema tool definitions and an agent loop that reliably picks the right tool (or correctly does nothing) is a skill in itself, distinct from general LLM prompting. Expect Phase 3–4 to involve real iteration here.
3. **MCP server development** — a new-ish protocol (JSON-RPC 2.0 based); the concepts are simple but you'll be learning the spec's conventions as you go. `fastmcp`'s docs and examples are the fastest on-ramp.
4. **Windows UI Automation internals** — `pywinauto` is Python, but reasoning about UI Automation trees, control types, and app-specific quirks is closer to QA/RPA engineering than typical app development. Plan for this to be the most trial-and-error-heavy part of Phase 5.
5. **Applied security engineering** — sandboxing, least-privilege process design, and threat-modeling prompt injection aren't things most app developers do day-to-day. This is worth deliberately studying (the OWASP Agentic Top 10 is a good starting document) rather than improvising, given what's at stake once the assistant can act on your machine.
6. **Cross-process packaging** — bundling a Python backend as a sidecar to a Flutter Windows app (process lifecycle, IPC, a single installer, code signing) is a specific, slightly fiddly packaging problem you likely haven't hit before even if you know both ecosystems separately. Leave real time for it in Phase 6 rather than treating it as a final afternoon's work.

None of these are blockers — they're just the parts of this project that will take longer than your general programming experience would predict, so it's worth planning slack around them rather than being surprised mid-phase.
