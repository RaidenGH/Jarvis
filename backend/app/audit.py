"""Append-only audit log (plan §2.5): every tool call, and what came of it.

One JSON object per line, so the file stays greppable and strictly appending —
a system with device access that can't tell you what it did last Tuesday is
not one you should trust with more access over time.

Logging is best-effort on purpose: a read-only location or a full disk must
never break a turn, so write failures are swallowed the same way audio
failures are.
"""

import json
from datetime import datetime
from pathlib import Path


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _ends_with_newline(path: Path) -> bool:
    """True if the file is empty, missing, or ends a line."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                return True
            handle.seek(-1, 2)
            return handle.read(1) == b"\n"
    except OSError:
        return True


class AuditLog:
    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else None

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def record(self, **fields) -> None:
        """Append one entry. Never raises."""
        if self._path is None:
            return
        entry = {"ts": _now(), **fields}
        try:
            # If the last write was interrupted mid-line, close that line off
            # first — otherwise this entry would be glued onto the torn JSON
            # and one crash would cost us two records instead of one.
            torn = not _ends_with_newline(self._path)
            with self._path.open("a", encoding="utf-8") as handle:
                if torn:
                    handle.write("\n")
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass  # degrade to no logging rather than fail the action

    def tail(self, limit: int = 20) -> list[dict]:
        """The most recent entries, oldest first (for `GET /audit`)."""
        if self._path is None or limit <= 0 or not self._path.exists():
            return []
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        entries: list[dict] = []
        for line in lines[-limit:]:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn last line must not hide the rest
            if isinstance(entry, dict):
                entries.append(entry)
        return entries
