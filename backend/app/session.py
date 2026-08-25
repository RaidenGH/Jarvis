"""Session-scoped conversation buffer.

Phase 0 scope: in-memory, per-session history only. Persistent memory
(SQLite / vector store) is explicitly a v2+ concern per the plan.
"""

from collections import defaultdict, deque
from typing import Deque

Message = dict  # {"role": "user"|"assistant"|"system", "content": str}

SYSTEM_PROMPT = (
    "You are Jarvis, a personal assistant running locally on the user's "
    "Windows PC. Be concise and helpful. You cannot control the device yet."
)


class SessionStore:
    def __init__(self, max_messages: int = 24):
        self._max = max_messages
        self._sessions: dict[str, Deque[Message]] = defaultdict(deque)

    def history(self, session_id: str) -> list[Message]:
        return list(self._sessions[session_id])

    def add(self, session_id: str, role: str, content: str) -> None:
        buf = self._sessions[session_id]
        buf.append({"role": role, "content": content})
        while len(buf) > self._max:
            buf.popleft()

    def reset(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
