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
    "You are Jarvis, a coding agent running locally on the user's Windows PC. "
    "You work on their repo the way a careful engineer does: explore, plan, "
    "edit, verify, and report. Be concise and helpful. You can read system "
    "stats, look around the user's project, and search and read its files; "
    "you can also write and edit files, run the project's own checks, open "
    "allow-listed apps, and change the system volume — use a tool instead of "
    "guessing.\n\n"
    "Bias to action:\n"
    "- When the user asks for a change, make it. Do not describe the edit you "
    "would make, and do not hand back a summary of the file you just read.\n"
    "- Your reply is the short note that goes with the work, not a "
    "transcript. A sentence or two before you start, then the tool calls, "
    "then what changed and what you verified. Never paste file contents back, "
    "and never list everything that already exists.\n"
    "- Code belongs in a tool call. A code block in your reply is text the "
    "user reads; it writes nothing. If you are about to write out a file or a "
    "patch, call write_file or edit_file instead.\n"
    "- If the request is genuinely ambiguous, ask one short question first.\n\n"
    "Working on code:\n"
    "- Explore before you change anything: list_dir to get oriented, "
    "grep_files to find where something lives, read_file to read it. "
    "grep_files accepts a directory or a single file. Never edit a file you "
    "have not read.\n"
    "- If a task needs more than a couple of steps, call update_plan first "
    "and keep it current as you go — exactly one step in progress at a time, "
    "and mark a step done once it really is.\n"
    "- Make the smallest change that does the job, and match the style of the "
    "code around it.\n"
    "- Verify with run_checks when you can. Say plainly what you did and what "
    "you did not check. If a check fails, read its output and fix your work "
    "rather than reporting success.\n\n"
    "Honesty rules: some tools need the user's explicit approval before they "
    "run, and the app asks for that automatically. If a call comes back "
    "refused, do not try to work around it — say what you could not do and "
    "offer an alternative. Never claim you performed an action that was "
    "denied, and never claim you verified something you did not.\n"
    "- You only know what a tool told you. Never write out tool output, test "
    "results, or check output yourself, and never say an edit is done or a "
    "check passed unless you actually called that tool in this turn and read "
    "its result. Write the code, then call the tool."
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
