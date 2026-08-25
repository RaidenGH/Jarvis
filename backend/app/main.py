"""Jarvis backend entrypoint (Phase 0).

Endpoints:
    GET  /health            liveness + configured provider/model
    POST /chat              one-shot REST chat (easy to curl)
    WS   /ws/{session_id}   streaming chat for the Flutter shell
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware

from .config import Settings, get_settings
from .llm import LLMClient, build_client
from .session import SYSTEM_PROMPT, SessionStore


@dataclass
class State:
    settings: Settings = field(default_factory=get_settings)
    sessions: SessionStore = field(default_factory=SessionStore)
    llm: LLMClient | None = None


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.settings = get_settings()
    state.sessions = SessionStore(max_messages=state.settings.max_history_messages)
    state.llm = build_client(state.settings)  # fails fast on bad config
    yield


app = FastAPI(title="Jarvis Backend", version="0.1.0", lifespan=lifespan)

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
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """Non-streaming one-shot chat. Handy for curl smoke tests."""
    reply = "".join([tok async for tok in _respond(req.session_id, req.text)])
    return ChatResponse(reply=reply)


@app.websocket("/ws/{session_id}")
async def ws_chat(ws: WebSocket, session_id: str) -> None:
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "reset":
                state.sessions.reset(session_id)
                await ws.send_json({"type": "reset_done"})
                continue
            if msg.get("type") != "user_message" or not str(msg.get("text", "")).strip():
                await ws.send_json({"type": "error", "message": "expected user_message"})
                continue
            text = str(msg["text"]).strip()
            async for token in _respond(session_id, text):
                await ws.send_json({"type": "token", "text": token})
            await ws.send_json({"type": "done"})
    except WebSocketDisconnect:
        pass


async def _respond(session_id: str, text: str):
    """Append the user turn, stream the assistant reply, persist history."""
    sessions = state.sessions
    sessions.add(session_id, "user", text)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *sessions.history(session_id)]
    reply_parts: list[str] = []
    assert state.llm is not None  # set by lifespan
    try:
        async for token in state.llm.stream_chat(messages):
            reply_parts.append(token)
            yield token
    except Exception as exc:  # noqa: BLE001 - surface a clean error to clients
        raise HTTPException(status_code=502, detail=f"LLM backend error: {exc}") from exc
    sessions.add(session_id, "assistant", "".join(reply_parts))
