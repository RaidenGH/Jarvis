"""`read_file` tool: read a text file, sandboxed to a configured root.

The plan's blast-radius rule: filesystem access is scoped to specific
folders, never arbitrary paths. The root comes from `JARVIS_TOOL_ROOT`
(default: the repo root) and every candidate path is resolved (collapsing
`..` and symlinks) before the containment check.
"""

from pathlib import Path

from ..config import get_settings
from .base import Tool

MAX_BYTES = 64_000  # keep single tool results small enough to reason over


def read_file(args: dict) -> dict:
    raw = args.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return {"error": "path is required"}

    root = Path(get_settings().tool_root).expanduser().resolve()
    target = Path(raw.strip()).expanduser()
    if not target.is_absolute():
        target = root / target
    target = target.resolve()

    if not target.is_relative_to(root):
        return {"error": f"path is outside the allowed root ({root})"}
    if target.is_dir():
        return {"error": f"path is a directory: {target}"}

    size = target.stat().st_size  # FileNotFoundError -> caught by execute_tool
    if size > MAX_BYTES:
        return {"error": f"file too large ({size} bytes > {MAX_BYTES})"}

    content = target.read_text(encoding="utf-8", errors="replace")
    return {"path": str(target), "bytes": size, "content": content}


READ_FILE = Tool(
    name="read_file",
    description=(
        "Read a UTF-8 text file inside the assistant's allowed folder root. "
        "Paths are relative to the root (e.g. 'app/lib/main.dart'). Read-only."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File to read, relative to the allowed root.",
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    handler=read_file,
)
