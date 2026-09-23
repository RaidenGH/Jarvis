"""Jarvis backend entrypoint (chat + push-to-talk + always-listening voice).

Endpoints:
    GET  /health            liveness + configured provider/model
    POST /chat              one-shot REST chat (easy to curl)
    WS   /ws/{session_id}   streaming chat + push-to-talk + wake-word voice

Phase 3: the WS loop can now service `confirm_response` frames *while* an agent
turn is running. That needs both sides of the conversation active at once, so
a turn is driven by `_pump_turn` — the agent loop runs as a task writing to a
queue, and the loop races that queue against the socket.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware

from .agent import ConfirmationDecision, run_turn
from .config import Settings, get_settings
from .llm import LLMClient, build_client
from .session import SessionStore
from .speech import SpeechService
from .tools import tool_names, tool_risks


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
    state.sessions = SessionStore(
        max_messages=state.settings.max_history_messages,
        path=state.settings.history_path or None,
    )
    state.llm = build_client(state.settings)  # fails fast on bad config
    state.speech = SpeechService(state.settings)  # cheap: engines are lazy
    yield


app = FastAPI(title="Jarvis Backend", version="0.3.0", lifespan=lifespan)

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
        "wake_enabled": s.wake_enabled,
        "wake_word": s.wake_word,
        "tools": tool_names(),
        "tool_risks": tool_risks(),
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """Non-streaming one-shot chat (tools run, only the answer is returned).

    Handy for curl smoke tests. There is no human on this channel, so gated
    tools are declined by the agent loop's default gate — a tool the model
    can't run without permission reports that back and the model answers
    without it.
    """
    assert state.llm is not None  # set by lifespan
    parts: list[str] = []
    try:
        async for event in run_turn(state.llm, state.sessions, req.session_id, req.text):
            if event["type"] == "token":
                parts.append(event["text"])
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"LLM backend error: {exc}") from exc
    return ChatResponse(reply="".join(parts))


class Confirmer:
    """The one confirmation question a turn may have outstanding.

    Deliberately transport-free: `ask` registers the question, the caller is
    responsible for getting the request to the client, and `resolve` is called
    when the answer comes back. A question nobody answers counts as a refusal
    once the timeout elapses, so an abandoned prompt can't wedge the turn (or
    the mic).
    """

    def __init__(self, timeout: float):
        self._timeout = timeout
        self._pending: asyncio.Future | None = None

    def ask(self) -> "asyncio.Future[ConfirmationDecision]":
        """Register the question *before* it is sent, so a fast reply to it
        can't arrive before we have something to resolve."""
        self._pending = asyncio.get_running_loop().create_future()
        return self._pending

    async def wait(self, pending: "asyncio.Future[ConfirmationDecision]") -> ConfirmationDecision:
        try:
            return await asyncio.wait_for(pending, timeout=self._timeout)
        except asyncio.TimeoutError:
            return ConfirmationDecision(approved=False)
        finally:
            self._pending = None

    def resolve(self, approved: bool, challenge: str | None) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.set_result(
                ConfirmationDecision(approved=bool(approved), challenge=challenge)
            )


@app.websocket("/ws/{session_id}")
async def ws_chat(ws: WebSocket, session_id: str) -> None:
    await ws.accept()
    # Serializes voice turns: one mic + one speaker, no interleaving.
    voice_lock = asyncio.Lock()
    recording = False
    listen_events: asyncio.Queue | None = None
    confirmer = Confirmer(state.settings.confirm_timeout_seconds)

    # Read frames off the socket in a task so the receive loop can also service
    # wake-word events and confirmation replies while it waits for the next
    # client message.
    disconnect = object()
    inbox: asyncio.Queue = asyncio.Queue()

    async def _reader() -> None:
        while True:
            try:
                message = await ws.receive_json()
            except json.JSONDecodeError:
                continue  # ignore a malformed frame
            except Exception:  # noqa: BLE001 - disconnect or transport error
                inbox.put_nowait(disconnect)
                return
            inbox.put_nowait(message)

    reader = asyncio.create_task(_reader())

    turn_done = object()

    async def _pump_turn(text: str) -> bool:
        """Stream one agent turn, staying able to read the socket meanwhile.

        Returns True if the turn completed, False if it failed (the client got
        an `error` frame). The loop runs as a task so `await confirm(...)`
        inside it doesn't stop us receiving the answer.
        """
        assert state.llm is not None
        events: asyncio.Queue = asyncio.Queue()
        failed = False

        async def _ask(request: dict) -> ConfirmationDecision:
            """Queue the question behind the tool_call that caused it, then
            wait for the socket loop to resolve it. Queueing is what keeps
            frame order honest — this task must not write to the socket, or
            the confirm prompt could overtake the call it is about."""
            pending = confirmer.ask()
            events.put_nowait(request)
            return await confirmer.wait(pending)

        async def _produce() -> None:
            nonlocal failed
            try:
                async for event in run_turn(
                    state.llm,
                    state.sessions,
                    session_id,
                    text,
                    confirm=_ask,
                ):
                    events.put_nowait(event)
            except RuntimeError as exc:
                failed = True
                events.put_nowait({"type": "error", "message": str(exc)})
            finally:
                events.put_nowait(turn_done)

        producer = asyncio.create_task(_produce())
        try:
            while True:
                msg_task = asyncio.ensure_future(inbox.get())
                evt_task = asyncio.ensure_future(events.get())
                done, _pending = await asyncio.wait(
                    {msg_task, evt_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in (msg_task, evt_task):
                    if task not in done:
                        task.cancel()

                if evt_task in done:
                    event = evt_task.result()
                    if event is turn_done:
                        break
                    await ws.send_json(event)

                if msg_task in done:
                    msg = msg_task.result()
                    if msg is disconnect:
                        inbox.put_nowait(disconnect)  # let the outer loop exit
                        break
                    if msg.get("type") == "confirm_response":
                        confirmer.resolve(
                            bool(msg.get("approved")), msg.get("challenge")
                        )
                    else:
                        await ws.send_json(
                            {
                                "type": "error",
                                "message": "a turn is already in progress",
                            }
                        )
        finally:
            producer.cancel()
        return not failed

    async def _voice_turn(audio) -> None:
        """Transcribe captured audio, stream the reply, then speak it aloud.

        Shared by push-to-talk and always-listening turns.
        """
        assert state.speech is not None
        await ws.send_json({"type": "ptt_state", "state": "transcribing"})
        transcript = await state.speech.transcribe(audio)
        if not transcript.strip():
            await ws.send_json({"type": "ptt_state", "state": "idle"})
            return
        await ws.send_json({"type": "transcript", "text": transcript})
        if not await _pump_turn(transcript):
            return
        await ws.send_json({"type": "tts_start"})
        try:
            reply = state.sessions.history(session_id)[-1]["content"]
            await state.speech.speak(reply)
        except Exception as exc:  # noqa: BLE001 - audio is best-effort
            await ws.send_json({"type": "error", "message": f"audio failed: {exc}"})
        await ws.send_json({"type": "tts_done"})

    try:
        while True:
            event = None
            if listen_events is not None:
                msg_task = asyncio.ensure_future(inbox.get())
                evt_task = asyncio.ensure_future(listen_events.get())
                done, _pending = await asyncio.wait(
                    {msg_task, evt_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in (msg_task, evt_task):
                    if task not in done:
                        task.cancel()
                if evt_task in done:
                    event = evt_task.result()
                msg = msg_task.result() if msg_task in done else None
            else:
                msg = await inbox.get()

            if event is not None:
                if event.get("type") == "utterance":
                    async with voice_lock:
                        await _voice_turn(event["audio"])
                    # Re-arm; the listener emits the resulting `armed` state.
                    state.speech.resume_listening()
                    await ws.send_json({"type": "done"})
                else:
                    await ws.send_json(event)

            if msg is None:
                continue
            if msg is disconnect:
                break
            mtype = msg.get("type")

            if mtype == "reset":
                state.sessions.reset(session_id)
                await ws.send_json({"type": "reset_done"})
                continue

            if mtype == "listen_start":
                assert state.speech is not None
                if recording:
                    await ws.send_json({"type": "error", "message": "recording in progress"})
                    continue
                if not state.settings.wake_enabled:
                    await ws.send_json({"type": "error", "message": "wake word disabled"})
                    continue
                if listen_events is not None:
                    await ws.send_json({"type": "error", "message": "already listening"})
                    continue
                events = state.speech.start_listening()
                if events is None:
                    await ws.send_json(
                        {"type": "error", "message": "could not start listening"}
                    )
                    continue
                listen_events = events
                continue

            if mtype == "listen_stop":
                assert state.speech is not None
                state.speech.stop_listening()
                listen_events = None
                await ws.send_json({"type": "listen_state", "state": "idle"})
                continue

            if mtype == "ptt_start":
                assert state.speech is not None
                if recording:
                    await ws.send_json({"type": "error", "message": "already recording"})
                    continue
                if listen_events is not None:
                    # Push-to-talk and wake listening share the mic; PTT wins.
                    state.speech.stop_listening()
                    listen_events = None
                    await ws.send_json({"type": "listen_state", "state": "idle"})
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
                async with voice_lock:
                    audio = state.speech.stop_recording()
                    await _voice_turn(audio)
                await ws.send_json({"type": "done"})
                continue

            if mtype == "confirm_response":
                # Belongs to a running turn; outside one it's just stale.
                confirmer.resolve(bool(msg.get("approved")), msg.get("challenge"))
                continue

            if mtype != "user_message" or not str(msg.get("text", "")).strip():
                await ws.send_json({"type": "error", "message": "expected user_message"})
                continue
            text = str(msg["text"]).strip()
            if await _pump_turn(text):
                await ws.send_json({"type": "done"})
    except WebSocketDisconnect:
        pass
    finally:
        if recording:
            state.speech.stop_recording()
        if state.speech is not None and state.speech.is_listening:
            state.speech.stop_listening()
        reader.cancel()