"""Jarvis backend entrypoint (Phase 0 + Phase 1 push-to-talk voice).

Endpoints:
    GET  /health            liveness + configured provider/model
    POST /chat              one-shot REST chat (easy to curl)
    WS   /ws/{session_id}   streaming chat + push-to-talk voice
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware

from .config import Settings, get_settings
from .llm import LLMClient, build_client
from .session import SYSTEM_PROMPT, SessionStore
from .speech import SpeechService


@dataclass
class State:
    settings: Settings = field(default_factory=get_settings)
    sessions: SessionStore = field(default_factory=SessionStore)
    llm: LLMClient | None = None
    speech: SpeechService | None = None


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.settings = get_settings()
    state.sessions = SessionStore(max_messages=state.settings.max_history_messages)
    state.llm = build_client(state.settings)  # fails fast on bad config
    state.speech = SpeechService(state.settings)  # cheap: engines are lazy
    yield


app = FastAPI(title="Jarvis Backend", version="0.2.0", lifespan=lifespan)

# Localhost-only dev convenience; tighten when packaging (Phase 6).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    text: str
    session_id: str = "default"


class ChatResponse(BaseModel):
    reply: str


@app.get("/health")
async def health() -> dict:
    s = state.settings
    return {
        "status": "ok",
        "provider": s.llm_provider,
        "model": s.llm_model,
        "ollama_base_url": s.ollama_base_url,
        "whisper_model": s.whisper_model,
        "tts_voice": s.tts_voice,
        "ptt": True,
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """Non-streaming one-shot chat. Handy for curl smoke tests."""
    try:
        reply = "".join([tok async for tok in _stream_reply(req.session_id, req.text)])
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"LLM backend error: {exc}") from exc
    return ChatResponse(reply=reply)


async def _stream_llm(messages: list[dict]):
    """Stream LLM tokens, translating provider failures to RuntimeError."""
    assert state.llm is not None  # set by lifespan
    try:
        async for token in state.llm.stream_chat(messages):
            yield token
    except Exception as exc:  # noqa: BLE001 - surface a clean error to clients
        raise RuntimeError(str(exc)) from exc


async def _stream_reply(session_id: str, text: str):
    """Record the user turn, stream the assistant reply, persist history."""
    state.sessions.add(session_id, "user", text)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *state.sessions.history(session_id)]
    reply_parts: list[str] = []
    async for token in _stream_llm(messages):
        reply_parts.append(token)
        yield token
    state.sessions.add(session_id, "assistant", "".join(reply_parts))


@app.websocket("/ws/{session_id}")
async def ws_chat(ws: WebSocket, session_id: str) -> None:
    await ws.accept()
    # Serializes voice turns: one mic + one speaker, no interleaving.
    voice_lock = asyncio.Lock()
    recording = False
    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")

            if mtype == "reset":
                state.sessions.reset(session_id)
                await ws.send_json({"type": "reset_done"})
                continue

            if mtype == "ptt_start":
                assert state.speech is not None
                if recording:
                    await ws.send_json({"type": "error", "message": "already recording"})
                    continue
                if voice_lock.locked():
                    await ws.send_json({"type": "error", "message": "voice busy"})
                    continue
                if not state.speech.start_recording():
                    await ws.send_json(
                        {"type": "error", "message": "could not start microphone"}
                    )
                    continue
                recording = True
                await ws.send_json({"type": "ptt_state", "state": "recording"})
                continue

            if mtype == "ptt_stop":
                assert state.speech is not None
                if not recording:
                    await ws.send_json({"type": "error", "message": "not recording"})
                    continue
                recording = False
                await ws.send_json({"type": "ptt_state", "state": "transcribing"})
                async with voice_lock:
                    audio = state.speech.stop_recording()
                    transcript = await state.speech.transcribe(audio)
                    if not transcript.strip():
                        await ws.send_json({"type": "ptt_state", "state": "idle"})
                        continue
                    await ws.send_json({"type": "transcript", "text": transcript})
                    try:
                        async for token in _stream_reply(session_id, transcript):
                            await ws.send_json({"type": "token", "text": token})
                    except RuntimeError as exc:
                        await ws.send_json({"type": "error", "message": str(exc)})
                        continue
                    await ws.send_json({"type": "tts_start"})
                    try:
                        reply = state.sessions.history(session_id)[-1]["content"]
                        await state.speech.speak(reply)
                    except Exception as exc:  # noqa: BLE001 - audio is best-effort
                        await ws.send_json(
                            {"type": "error", "message": f"audio failed: {exc}"}
                        )
                    await ws.send_json({"type": "tts_done"})
                await ws.send_json({"type": "done"})
                continue

            if mtype != "user_message" or not str(msg.get("text", "")).strip():
                await ws.send_json({"type": "error", "message": "expected user_message"})
                continue
            text = str(msg["text"]).strip()
            try:
                async for token in _stream_reply(session_id, text):
                    await ws.send_json({"type": "token", "text": token})
            except RuntimeError as exc:
                await ws.send_json({"type": "error", "message": str(exc)})
                continue
            await ws.send_json({"type": "done"})
    except WebSocketDisconnect:
        pass
    finally:
        if recording:
            state.speech.stop_recording()