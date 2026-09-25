"""Filesystem tools, sandboxed to a configured root.

The plan's blast-radius rule: filesystem access is scoped to specific folders,
never arbitrary paths. The root comes from `JARVIS_TOOL_ROOT` (default: the
repo root) and every candidate path is resolved (collapsing `..` and symlinks)
before the containment check — so no tool here can reach outside it, whatever
the model asks for.

Two families live in this module:

* **Reading** — `read_file` (whole file, or a line window for big ones),
  `list_dir` and `grep_files` so the agent can survey a repo before touching
  it. These are `read-only`: they run without asking.
* **Writing** — `write_file` and `edit_file`, both `reversible-write`, so the
  human sees a diff and approves before anything changes on disk. Writes into
  vendor/VCS directories (`.git`, `.venv`, `node_modules`, `build`, …) are
  refused outright: there is no legitimate edit there, and it is where an
  agent can do the most damage.

Edits return a unified diff as part of the tool result. That serves two
readers at once: the human approving the change, and the model, which gets to
see exactly what it altered before deciding the next step.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from ..config import get_settings
from .base import REVERSIBLE_WRITE, Tool

MAX_BYTES = 64_000  # keep single tool results small enough to reason over
MAX_RESULTS = 50
MAX_SCAN = 20_000  # bound the walk so a huge tree can't hang a turn

MAX_ENTRIES = 200  # list_dir
MAX_GREP_RESULTS = 100
MAX_GREP_FILES = 2_000  # files scanned by one grep
MAX_GREP_FILE_BYTES = 256_000  # ignore anything bigger than a large source file
MAX_MATCH_CHARS = 200
MAX_WRITE_CHARS = 400_000
DIFF_LINES = 400  # a diff longer than this is summarised, not dumped

#: Directories a search never descends into *and* a write never touches:
#: dependency/vendor noise that would drown the answer and slow the walk to a
#: crawl, plus the two places an edit can silently break the whole checkout.
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


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - _inside_root guarantees containment
        return str(path)


def _blocked_write(target: Path, root: Path) -> dict | None:
    """Refuse writes into vendor/VCS directories."""
    parts = target.relative_to(root).parts
    for part in parts:
        if part in SKIP_DIRS:
            return {
                "error": (
                    f"refusing to write inside '{part}/' — it is vendor or VCS "
                    "territory, not project source"
                )
            }
    return None


def _diff(before: str, after: str, rel: str) -> dict:
    """Unified diff plus add/remove counts, capped so one huge edit can't
    swallow the model's context."""
    old, new = before.splitlines(), after.splitlines()
    lines = list(
        difflib.unified_diff(
            old, new, fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="", n=3
        )
    )
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    text = "\n".join(lines[:DIFF_LINES])
    truncated = len(lines) > DIFF_LINES
    if truncated:
        text += f"\n… {len(lines) - DIFF_LINES} more diff lines"
    return {
        "diff": text,
        "lines_added": added,
        "lines_removed": removed,
        "diff_truncated": truncated,
    }


# --- reading --------------------------------------------------------------


def read_file(args: dict) -> dict:
    raw = args.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return {"error": "path is required"}

    root = _sandbox_root()
    target = _inside_root(raw, root)
    if isinstance(target, dict):
        return target
    if target.is_dir():
        return {"error": f"path is a directory: {_relative(target, root)} (use list_dir)"}

    size = target.stat().st_size  # FileNotFoundError -> caught by execute_tool
    start, end = _window(args)
    windowed = start > 1 or end is not None

    if size > MAX_BYTES and not windowed:
        return {
            "error": (
                f"file too large ({size} bytes > {MAX_BYTES}); read a window "
                "with start_line and end_line instead"
            )
        }

    if windowed:
        text, more = _read_window(target, start, end or start + 399)
        result = {"path": str(target), "bytes": size, "content": text}
        result["range"] = [start, end or start + 399]
        result["more"] = more
        return result

    content = target.read_text(encoding="utf-8", errors="replace")
    return {
        "path": str(target),
        "bytes": size,
        "lines": content.count("\n") + 1,
        "content": content,
    }


def _window(args: dict) -> tuple[int, int | None]:
    """Parse optional start_line/end_line (1-based, inclusive)."""
    start = _positive(args.get("start_line")) or 1
    end = _positive(args.get("end_line"))
    if end is not None and end < start:
        end = None
    return start, end


def _positive(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _read_window(target: Path, start: int, end: int) -> tuple[str, bool]:
    """Read lines `start..end` without loading a huge file into memory."""
    kept: list[str] = []
    more = False
    with target.open("r", encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, start=1):
            if number < start:
                continue
            if number > end:
                more = True
                break
            kept.append(line.rstrip("\n"))
    return "\n".join(kept), more


def list_dir(args: dict) -> dict:
    """One directory listing: names, types and sizes, directories first."""
    root = _sandbox_root()
    target = _inside_root(args.get("path"), root)
    if isinstance(target, dict):
        return target
    if not target.is_dir():
        return {"error": f"not a directory: {_relative(target, root)}"}

    try:
        children = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError as exc:
        return {"error": str(exc)}

    entries = []
    for child in children[:MAX_ENTRIES]:
        try:
            is_dir = child.is_dir()
            size = None if is_dir else child.stat().st_size
        except OSError:
            is_dir, size = False, None
        entries.append(
            {"name": child.name, "type": "dir" if is_dir else "file", "bytes": size}
        )

    return {
        "path": _relative(target, root) or ".",
        "entries": entries,
        "truncated": len(children) > MAX_ENTRIES,
    }


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
        return {"error": f"not a directory: {_relative(start, root)}"}

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
                matches.append(_relative(path, root))
                if len(matches) >= limit:
                    break
    except OSError as exc:  # unreadable subtree: report what we did find
        return {"query": query, "matches": matches, "error": str(exc)}

    return {
        "query": query,
        "searched": _relative(start, root) or ".",
        "matches": sorted(matches),
        "truncated": len(matches) >= limit,
    }


def grep_files(args: dict) -> dict:
    """Search file *contents*, returning path:line hits.

    `search_files` answers "which files are called this"; this answers "where
    does this string or pattern live", which is what finding a bug actually
    needs. An invalid regex is treated as a literal string rather than an
    error, because a model that meant `dict[str, int]` shouldn't get a lecture.

    `path` may be a single file as well as a directory: "where does this appear
    in this file" is a normal question, and answering it with a "not a
    directory" error just burns a round.
    """
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return {"error": "pattern is required"}

    root = _sandbox_root()
    start = _inside_root(args.get("path"), root)
    if isinstance(start, dict):
        return start
    if not start.exists():
        return {"error": f"no such file or directory: {_relative(start, root)}"}

    flags = 0 if args.get("ignore_case") is False else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error:
        regex = re.compile(re.escape(pattern), flags)

    try:
        limit = int(args.get("max_results") or 30)
    except (TypeError, ValueError):
        return {"error": "max_results must be a whole number"}
    limit = max(1, min(limit, MAX_GREP_RESULTS))

    glob = args.get("glob")
    glob = glob if isinstance(glob, str) and glob.strip() else "*"

    matches: list[dict] = []
    scanned = 0
    single = start.is_file()
    try:
        candidates = [start] if single else start.rglob(glob)
        for path in candidates:
            if any(part in SKIP_DIRS for part in path.parts) or not path.is_file():
                continue
            try:
                if path.stat().st_size > MAX_GREP_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "\x00" in text[:4096]:  # binary dressed up as text
                continue
            scanned += 1
            if scanned > MAX_GREP_FILES:
                break
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(
                        {
                            "path": _relative(path, root),
                            "line": number,
                            "text": line.strip()[:MAX_MATCH_CHARS],
                        }
                    )
                    if len(matches) >= limit:
                        break
            if len(matches) >= limit:
                break
    except OSError as exc:  # unreadable subtree: report what we did find
        return {"pattern": pattern, "matches": matches, "error": str(exc)}

    return {
        "pattern": pattern,
        "searched": _relative(start, root) or ".",
        "files_scanned": scanned,
        "matches": matches,
        "truncated": len(matches) >= limit,
    }


# --- writing --------------------------------------------------------------


def write_file(args: dict) -> dict:
    """Create or replace a file, returning the diff of what changed."""
    raw = args.get("path")
    content = args.get("content")
    if not isinstance(raw, str) or not raw.strip():
        return {"error": "path is required"}
    if not isinstance(content, str):
        return {"error": "content must be a string"}

    root = _sandbox_root()
    target = _inside_root(raw, root)
    if isinstance(target, dict):
        return target
    if target.is_dir():
        return {"error": f"path is a directory: {_relative(target, root)}"}
    if len(content) > MAX_WRITE_CHARS:
        return {"error": f"content too large ({len(content)} chars > {MAX_WRITE_CHARS})"}

    blocked = _blocked_write(target, root)
    if blocked:
        return blocked

    rel = _relative(target, root)
    before = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
    if before == content:
        return {"path": rel, "changed": False, "note": "file already had this content"}

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {
        "path": rel,
        "created": before == "",
        "bytes": len(content.encode("utf-8")),
        "lines": content.count("\n") + 1,
        **_diff(before, content, rel),
    }


def edit_file(args: dict) -> dict:
    """Replace an exact snippet inside one file.

    Deliberately strict: the snippet must appear verbatim, and an ambiguous
    match is an error rather than a guess. Sloppy find-and-replace is how an
    agent silently corrupts a file, so the failure mode here is "read it again
    and be precise".
    """
    raw = args.get("path")
    find = args.get("find")
    replace = args.get("replace", "")
    if not isinstance(raw, str) or not raw.strip():
        return {"error": "path is required"}
    if not isinstance(find, str) or not find:
        return {"error": "find is required (the exact text to replace)"}
    if not isinstance(replace, str):
        return {"error": "replace must be a string"}

    root = _sandbox_root()
    target = _inside_root(raw, root)
    if isinstance(target, dict):
        return target
    blocked = _blocked_write(target, root)
    if blocked:
        return blocked
    rel = _relative(target, root)
    if not target.is_file():
        return {"error": f"no such file: {rel} (use write_file to create it)"}

    before = target.read_text(encoding="utf-8", errors="replace")
    occurrences = before.count(find)
    if occurrences == 0:
        return {
            "error": (
                f"that exact text is not in {rel} — read the file and copy the "
                "snippet verbatim; whitespace and indentation must match"
            )
        }
    replace_all = args.get("replace_all") is True
    if occurrences > 1 and not replace_all:
        return {
            "error": (
                f"{occurrences} occurrences of that snippet in {rel}; include "
                "more surrounding lines so it is unique, or pass replace_all"
            )
        }

    after = before.replace(find, replace) if replace_all else before.replace(find, replace, 1)
    target.write_text(after, encoding="utf-8")
    return {
        "path": rel,
        "changed": True,
        "occurrences": occurrences,
        "replaced": occurrences if replace_all else 1,
        **_diff(before, after, rel),
    }


# --- approval previews ----------------------------------------------------
# Shown in the confirmation prompt *before* anything is written: the same diff
# the tool would return, computed against the current file on disk.


def preview_write(args: dict) -> str | None:
    return _preview(args, write=True)


def preview_edit(args: dict) -> str | None:
    return _preview(args, write=False)


def _preview(args: dict, *, write: bool) -> str | None:
    root = _sandbox_root()
    target = _inside_root(args.get("path") if isinstance(args.get("path"), str) else None, root)
    if isinstance(target, dict):
        return str(target.get("error", ""))

    rel = _relative(target, root)
    before = ""
    if target.is_file():
        try:
            before = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"(could not read {rel}: {exc})"

    if write:
        after = args.get("content")
        if not isinstance(after, str):
            return "(content missing)"
    else:
        find = args.get("find")
        replace = args.get("replace", "")
        if not isinstance(find, str) or not find:
            return "(find missing)"
        if find not in before:
            return f"(snippet not found in {rel} — the call will be rejected)"
        if before.count(find) > 1 and args.get("replace_all") is not True:
            return f"(snippet appears {before.count(find)}x in {rel} — will be rejected as ambiguous)"
        after = before.replace(find, replace) if args.get("replace_all") is True else before.replace(find, replace, 1)

    if before == after:
        return f"({rel} already matches — nothing to write)"
    diff = _diff(before, after, rel)
    return diff["diff"] or f"(no textual change in {rel})"


# --- tool definitions -----------------------------------------------------

READ_FILE = Tool(
    name="read_file",
    description=(
        "Read a UTF-8 text file inside the assistant's allowed folder root. "
        "Paths are relative to the root (e.g. 'app/lib/main.dart'). For a file "
        "too large to read whole, pass start_line and end_line to read a window. "
        "Read-only."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File to read, relative to the allowed root.",
            },
            "start_line": {
                "type": "integer",
                "description": "First line to read (1-based). Optional.",
            },
            "end_line": {
                "type": "integer",
                "description": "Last line to read (inclusive). Optional.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    handler=read_file,
)

LIST_DIR = Tool(
    name="list_dir",
    description=(
        "List one directory inside the allowed root: names, types and sizes, "
        "directories first. Use it to get oriented in a project. Read-only."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory to list, relative to the root (default '.').",
            }
        },
        "required": [],
        "additionalProperties": False,
    },
    handler=list_dir,
)

SEARCH_FILES = Tool(
    name="search_files",
    description=(
        "Find files by *name* inside the allowed folder root (case-insensitive "
        "substring). To search within file contents, use grep_files instead. "
        "Read-only."
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

GREP_FILES = Tool(
    name="grep_files",
    description=(
        "Search the *contents* of files inside the allowed root and return "
        "path, line number and the matching line. This is how you find where "
        "something is defined or used. `pattern` is a regular expression "
        "(treated as a literal string if it is not valid). Narrow with `path` "
        "and `glob` (e.g. glob='*.dart'). Read-only."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regular expression (or literal text) to find.",
            },
            "path": {
                "type": "string",
                "description": "Folder to search under, relative to the root.",
            },
            "glob": {
                "type": "string",
                "description": "Filename filter, e.g. '*.py' or '*.dart'.",
            },
            "ignore_case": {
                "type": "boolean",
                "description": "Case-insensitive search (default true).",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum matches to return (default 30).",
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
    handler=grep_files,
)

WRITE_FILE = Tool(
    name="write_file",
    description=(
        "Create a file, or replace an existing one wholesale, inside the "
        "allowed root. This is the only thing that actually writes a file: "
        "code written into your reply is just text to the user and changes "
        "nothing. Pass the complete new contents here. Prefer edit_file for a "
        "small change to an existing file. Needs the user's approval."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File to write, relative to the allowed root.",
            },
            "content": {
                "type": "string",
                "description": "The complete new contents of the file.",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
    handler=write_file,
    risk=REVERSIBLE_WRITE,
    preview=preview_write,
)

EDIT_FILE = Tool(
    name="edit_file",
    description=(
        "Replace an exact snippet inside an existing file, inside the allowed "
        "root. Call this to make the change; describing the edit in your reply "
        "changes nothing. `find` must match the file byte for byte, including "
        "indentation — read the file first and copy it verbatim. If the "
        "snippet occurs more than once the call is rejected unless you pass "
        "replace_all. Needs the user's approval."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File to edit, relative to the allowed root.",
            },
            "find": {
                "type": "string",
                "description": "Exact existing text to replace (include indentation).",
            },
            "replace": {
                "type": "string",
                "description": "Text to put in its place.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence (default false).",
            },
        },
        "required": ["path", "find", "replace"],
        "additionalProperties": False,
    },
    handler=edit_file,
    risk=REVERSIBLE_WRITE,
    preview=preview_edit,
)
