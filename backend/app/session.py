"""Session-scoped conversation buffer.

In-memory per session, with optional JSON persistence so a session survives
restarts (Phase 2.5: the CLI resumes where it left off). Persistent *memory*
(SQLite / vector store) remains a v2+ concern per the plan.
"""

import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Deque

Message = dict  # {"role": "user"|"assistant"|"system", "content": str}

SYSTEM_PROMPT = (
    "You are Jarvis, a personal assistant running locally on the user's "
    "Windows PC. Be concise and helpful. You have tools for reading system "
    "stats and reading files inside your allowed folder — use them when they "
    "help answer.\n\n"
    "Some tools require the user's explicit confirmation before they run; the "
    "assistant app asks for that automatically. If a call comes back saying "
    "the user did not confirm it, do not try to work around it — acknowledge "
    "the refusal and offer an alternative. Never claim you performed an "
    "action that was denied."
)


class SessionStore:
    def __init__(self, max_messages: int = 24, path: str | Path | None = None):
        self._max = max_messages
        self._path = Path(path) if path else None
        self._sessions: dict[str, Deque[Message]] = defaultdict(deque)
        self._load()

    def history(self, session_id: str) -> list[Message]:
        return list(self._sessions[session_id])

    def add(self, session_id: str, role: str, content: str) -> None:
        buf = self._sessions[session_id]
        buf.append({"role": role, "content": content})
        while len(buf) > self._max:
            buf.popleft()
        self._save()

    def reset(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._save()

    # --- persistence -----------------------------------------------------

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return  # a corrupt history file must never block startup
        if not isinstance(data, dict):
            return
        for session_id, messages in data.items():
            if not isinstance(session_id, str) or not isinstance(messages, list):
                continue
            buf = self._sessions[session_id]
            for msg in messages[-self._max :]:
                if (
                    isinstance(msg, dict)
                    and isinstance(msg.get("role"), str)
                    and isinstance(msg.get("content"), str)
                ):
                    buf.append({"role": msg["role"], "content": msg["content"]})

    def _save(self) -> None:
        if self._path is None:
            return
        payload = {sid: list(buf) for sid, buf in self._sessions.items()}
        try:
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass  # read-only location: degrade to in-memory only
