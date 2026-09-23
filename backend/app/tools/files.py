"""Filesystem tools, sandboxed to a configured root.

The plan's blast-radius rule: filesystem access is scoped to specific folders,
never arbitrary paths. The root comes from `JARVIS_TOOL_ROOT` (default: the
repo root) and every candidate path is resolved (collapsing `..` and symlinks)
before the containment check — so neither `read_file` nor `search_files` can
reach outside it, whatever the model asks for.
"""

from pathlib import Path

from ..config import get_settings
from .base import Tool

MAX_BYTES = 64_000  # keep single tool results small enough to reason over
MAX_RESULTS = 50
MAX_SCAN = 20_000  # bound the walk so a huge tree can't hang a turn

#: Directories a search never descends into: dependency/vendor noise that
#: would drown the answer and slow the walk to a crawl.
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".dart_tool",
    ".pytest_cache",
    "build",
    "dist",
    ".models",
    ".idea",
}


def _sandbox_root() -> Path:
    return Path(get_settings().tool_root).expanduser().resolve()


def _inside_root(raw: str | None, root: Path) -> Path | dict:
    """Resolve `raw` inside `root`, or return an error dict."""
    candidate = Path(raw.strip() or ".").expanduser() if isinstance(raw, str) else Path(".")
    if not candidate.is_absolute():
        candidate = root / candidate
    target = candidate.resolve()
    if not target.is_relative_to(root):
        return {"error": f"path is outside the allowed root ({root})"}
    return target


def read_file(args: dict) -> dict:
    raw = args.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return {"error": "path is required"}

    root = _sandbox_root()
    target = _inside_root(raw, root)
    if isinstance(target, dict):
        return target
    if target.is_dir():
        return {"error": f"path is a directory: {target}"}

    size = target.stat().st_size  # FileNotFoundError -> caught by execute_tool
    if size > MAX_BYTES:
        return {"error": f"file too large ({size} bytes > {MAX_BYTES})"}

    content = target.read_text(encoding="utf-8", errors="replace")
    return {"path": str(target), "bytes": size, "content": content}


def search_files(args: dict) -> dict:
    """Find files by name (substring, case-insensitive) under the root."""
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"error": "query is required"}

    root = _sandbox_root()
    start = _inside_root(args.get("path"), root)
    if isinstance(start, dict):
        return start
    if not start.is_dir():
        return {"error": f"not a directory: {start}"}

    try:
        limit = int(args.get("max_results") or 20)
    except (TypeError, ValueError):
        return {"error": "max_results must be a whole number"}
    limit = max(1, min(limit, MAX_RESULTS))

    needle = query.strip().lower()
    matches: list[str] = []
    scanned = 0
    try:
        for path in start.rglob("*"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            scanned += 1
            if scanned > MAX_SCAN:
                break
            if path.is_file() and needle in path.name.lower():
                matches.append(str(path.relative_to(root)))
                if len(matches) >= limit:
                    break
    except OSError as exc:  # unreadable subtree: report what we did find
        return {"query": query, "matches": matches, "error": str(exc)}

    return {
        "query": query,
        "searched": str(start.relative_to(root)) or ".",
        "matches": sorted(matches),
        "truncated": len(matches) >= limit,
    }


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

SEARCH_FILES = Tool(
    name="search_files",
    description=(
        "Find files by name inside the assistant's allowed folder root. "
        "Matches on a case-insensitive substring of the filename. Read-only."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Substring to look for in filenames, e.g. 'test_'.",
            },
            "path": {
                "type": "string",
                "description": "Folder to search, relative to the allowed root.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum matches to return (default 20).",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    handler=search_files,
)
