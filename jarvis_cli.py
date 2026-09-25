#!/usr/bin/env python
"""Jarvis CLI — an agentic terminal front-end to the backend's agent loop.

This is the *working* front-end, as opposed to the chat one: you give it a
job ("find why the transcript test fails and fix it"), and it explores the
repo on its own — listing directories, grepping for symbols, reading files —
writes its plan down, edits code, runs the project's checks, and tells you
what it actually did.

It talks to the same FastAPI/WebSocket backend the Flutter shell uses, so
every guarantee the plan promises still holds here: the tool allow-list, the
risk tiers, the approval gate, and the append-only audit log. Nothing is
executed on the model's say-so alone.

Interactive:

    backend/.venv/Scripts/python jarvis_cli.py

One-shot, for scripts and for "just do this":

    backend/.venv/Scripts/python jarvis_cli.py "add a --dry-run flag to the volume tool"
    ... --model qwen2.5:7b      run on a different (bigger) local model
    ... --yes                   approve tap-tier actions (edits, checks) up front
    ... --new                   start from an empty conversation

Commands inside the chat:  /help /tools /plan /audit /model /verbose /reset
/quit. History persists on the backend side (JARVIS_HISTORY_PATH), so
restarting the CLI or the backend keeps the conversation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.request

try:
    import websockets
except ImportError:  # running with a Python that isn't the backend venv
    sys.exit(
        "Missing 'websockets'. Run me with the backend's venv:\n"
        "    backend/.venv/Scripts/python jarvis_cli.py"
    )

DEFAULT_WS_URL = os.environ.get("JARVIS_WS_URL", "ws://127.0.0.1:8000/ws/cli")

# --- terminal -------------------------------------------------------------

INTERACTIVE = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _code(value: str) -> str:
    return value if INTERACTIVE else ""


RESET = _code("\x1b[0m")
BOLD = _code("\x1b[1m")
DIM = _code("\x1b[2m")
ITALIC = _code("\x1b[3m")
RED = _code("\x1b[31m")
GREEN = _code("\x1b[32m")
YELLOW = _code("\x1b[33m")
BLUE = _code("\x1b[34m")
MAGENTA = _code("\x1b[35m")
CYAN = _code("\x1b[36m")
GREY = _code("\x1b[90m")

TOOL_GLYPH = "⚙"
RESULT_GLYPH = "↳"
WARN_GLYPH = "⚠"
DENY_GLYPH = "✋"
OK_GLYPH = "✓"
FAIL_GLYPH = "✗"
DONE_STEP = "✓"
NOW_STEP = "▸"
TODO_STEP = "☐"

MAX_RESULT_CHARS = 800
MAX_DIFF_LINES = 40

#: Risk tiers, mirroring backend/app/tools/base.py. Only tap tiers can be
#: pre-approved: a typed tier exists precisely so a human retypes a phrase,
#: and the backend ignores an approval that doesn't carry the challenge.
RISK_TIERS = ("read-only", "reversible-write", "destructive", "external-facing")
#: What `--yes` covers without naming a tier: edits and checks.
PRE_APPROVED_TIERS = ("reversible-write",)

#: Matches the parameter count in a model tag, e.g. the `4b` of `qwen3:4b`.
_MODEL_SIZE = re.compile(r"[:_-](\d+(?:\.\d+)?)b\b", re.IGNORECASE)
#: Below this, multi-step tool use gets unreliable.
SMALL_MODEL_PARAMS = 7.0

HELP = f"""\
{BOLD}How this works{RESET}
  Ask for something in plain language. For anything beyond one step the agent
  will list a plan, read the files it needs, make the edits, then run the
  project's checks and tell you the result.
  {DIM}Read-only tools (list_dir, read_file, grep_files, search_files,
  system_stats, update_plan) run without asking. Everything else — edits and
  checks — stops for your approval and is written to the audit log.{RESET}

{BOLD}Commands{RESET}
  /tools              tools the brain may call, with their risk tiers
  /plan               the agent's current todo list
  /audit              recent tool calls from the audit log
  /model [name]       show, or switch, the model for the next messages
  /verbose            show full tool output instead of summaries
  /reset              clear this conversation's history
  /help  /quit        this help, or exit (Ctrl+C also works)

{BOLD}Approvals{RESET}
  {YELLOW}y{RESET} allow once    {YELLOW}n{RESET} no    {YELLOW}a{RESET} allow this tier for the rest of the run
  Dangerous actions make you retype a challenge phrase instead — a stray "y"
  can't satisfy one.
"""


# --- rendering ------------------------------------------------------------


def _base(path: object) -> str:
    text = str(path or "")
    return text.replace("\\", "/").rsplit("/", 1)[-1]


def render_value(key: str, value: object) -> str:
    """One argument, short enough to read at a glance.

    Long payloads (the body of a file being written, a big find/replace) are
    reported as a size instead of a wall of text — the diff in the approval
    prompt is where that detail belongs.
    """
    if isinstance(value, str):
        if "\n" in value or len(value) > 48:
            return f"{key}=«{len(value)} chars»"
        return f'{key}="{value}"'
    return f"{key}={json.dumps(value, ensure_ascii=False, default=str)}"


def format_arguments(arguments: object, limit: int = 4) -> str:
    if not isinstance(arguments, dict) or not arguments:
        return ""
    items = list(arguments.items())
    parts = [render_value(key, value) for key, value in items[:limit]]
    if len(items) > limit:
        parts.append(f"+{len(items) - limit} more")
    return ", ".join(parts)


def summarize_result(name: str, result: object) -> str:
    """The one line worth printing for a tool result."""
    if not isinstance(result, dict):
        return _clip(str(result), 100)
    if result.get("error"):
        return f"{RED}error: {result['error']}{RESET}"

    changed = f"{GREEN}+{result.get('lines_added', 0)}{RESET}/{RED}-{result.get('lines_removed', 0)}{RESET}"
    if name == "read_file":
        where = str(result.get("path", "")).replace("\\", "/")
        size = result.get("lines", "?")
        window = f" (lines {result['range'][0]}–{result['range'][1]})" if result.get("range") else ""
        return f"{size} lines · {where}{window}"
    if name == "list_dir":
        entries = result.get("entries", [])
        dirs = sum(1 for e in entries if e.get("type") == "dir")
        return f"{len(entries)} entries ({dirs} dirs) · {result.get('path', '.')}"
    if name == "grep_files":
        return (
            f"{len(result.get('matches', []))} matches in "
            f"{result.get('files_scanned', 0)} files"
        )
    if name == "search_files":
        return f"{len(result.get('matches', []))} matching files"
    if name == "write_file":
        what = "created" if result.get("created") else "replaced"
        return f"{what} {result.get('path')} ({changed})"
    if name == "edit_file":
        return f"replaced {result.get('replaced')}× in {result.get('path')} ({changed})"
    if name == "run_checks":
        if result.get("timed_out"):
            return f"{RED}{name} timed out{RESET}"
        ok = result.get("passed")
        mark = f"{GREEN}passed{RESET}" if ok else f"{RED}FAILED{RESET}"
        return f"{result.get('check')} → exit {result.get('exit_code')} · {mark}"
    if name == "update_plan":
        return f"{result.get('remaining', 0)} steps remaining"
    if name == "system_stats":
        cpu = result.get("cpu", {})
        memory = result.get("memory", {})
        return (
            f"{cpu.get('logical_cores', '?')} cores · "
            f"RAM {memory.get('used_percent', '?')}% used"
        )
    if name == "open_app":
        return f"opened {result.get('app') or result.get('opened') or 'app'}"
    if name == "change_volume":
        return f"volume {result.get('volume', result.get('level', '?'))}"
    return _clip(json.dumps(result, ensure_ascii=False, default=str), 100)


def render_result_body(name: str, result: object, *, verbose: bool) -> None:
    """Anything a result deserves beyond its one-line summary.

    Diffs and check output are the two that carry real information; summaries
    are enough for everything else unless /verbose is on.
    """
    if not isinstance(result, dict):
        return
    diff = result.get("diff")
    if isinstance(diff, str) and diff.strip():
        print_diff(diff)
        if result.get("diff_truncated"):
            print(f"    {DIM}… diff truncated{RESET}")
        return
    if name == "run_checks" and not result.get("passed"):
        body = result.get("output") or ""
        print(_indent(body.strip(), "    ", GREY))
        return
    if verbose:
        print(_indent(json.dumps(result, indent=2, ensure_ascii=False, default=str), "    ", GREY))


def print_diff(diff: str) -> None:
    """Colour a unified diff the way `git diff` does, and cap its length."""
    lines = diff.splitlines()
    for line in lines[:MAX_DIFF_LINES]:
        if line.startswith("+++") or line.startswith("---"):
            print(f"    {BOLD}{line}{RESET}")
        elif line.startswith("@@"):
            print(f"    {CYAN}{line}{RESET}")
        elif line.startswith("+"):
            print(f"    {GREEN}{line}{RESET}")
        elif line.startswith("-"):
            print(f"    {RED}{line}{RESET}")
        else:
            print(f"    {GREY}{line}{RESET}")
    if len(lines) > MAX_DIFF_LINES:
        print(f"    {DIM}… {len(lines) - MAX_DIFF_LINES} more diff lines{RESET}")


def render_plan(items: list) -> None:
    done = sum(1 for item in items if item.get("status") == "done")
    print(f"  {BOLD}plan{RESET} {DIM}{done}/{len(items)}{RESET}")
    for item in items:
        status = item.get("status")
        if status == "done":
            mark, colour = DONE_STEP, GREY
        elif status == "in_progress":
            mark, colour = NOW_STEP, CYAN
        else:
            mark, colour = TODO_STEP, DIM
        print(f"    {colour}{mark} {item.get('task', '')}{RESET}")


def _clip(text: str, limit: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _indent(text: str, prefix: str, colour: str = "") -> str:
    body = "\n".join(prefix + line for line in text.splitlines())
    return f"{colour}{body}{RESET}" if colour else body


def _since(start: float) -> str:
    return f"{time.monotonic() - start:.1f}s"


def small_brain_hint(model: str) -> str | None:
    """A note when the model is too small to sustain an agent loop.

    Worth saying out loud rather than letting the user discover it: a small
    model tends to *describe* an edit instead of making it, and a really small
    one will invent a plausible-looking result ("pytest passed") without ever
    calling a tool. Both are confusing when the CLI says nothing.
    """
    match = _MODEL_SIZE.search(model or "")
    if not match:
        return None
    try:
        params = float(match.group(1))
    except ValueError:  # pragma: no cover - the regex only yields numbers
        return None
    if params >= SMALL_MODEL_PARAMS:
        return None
    return (
        f"  {DIM}note    {model} is small for agent work: it may describe edits "
        f"instead of making them. A 7B+ model is more reliable (--model NAME).{RESET}"
    )


# --- backend helpers ------------------------------------------------------

HttpBase = str


def _http_base(ws_url: str) -> HttpBase:
    return ws_url.split("/ws/")[0].replace("ws://", "http://", 1).replace(
        "wss://", "https://", 1
    )


def fetch_json(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return json.loads(response.read())
    except Exception:  # noqa: BLE001 - these are all nice-to-haves
        return None


def fetch_health(ws_url: str) -> dict | None:
    return fetch_json(_http_base(ws_url) + "/health")


def fetch_audit(ws_url: str, limit: int = 10) -> list[dict]:
    payload = fetch_json(f"{_http_base(ws_url)}/audit?limit={limit}") or {}
    return payload.get("entries", [])


# --- progress line --------------------------------------------------------


class Ticker:
    """A dim `⋯ working 12s` line that any real output replaces.

    Only on a terminal: piped output must stay clean for scripts.
    """

    def __init__(self, label: str):
        self.label = label
        self._task: asyncio.Task | None = None
        self._start = 0.0

    def start(self) -> None:
        if not INTERACTIVE or self._task is not None:
            return
        self._start = time.monotonic()
        self._task = asyncio.ensure_future(self._run())

    def clear(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        self._task = None
        sys.stdout.write("\r\x1b[K")
        sys.stdout.flush()

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                sys.stdout.write(
                    f"\r\x1b[K{GREY}  ⋯ {self.label} {time.monotonic() - self._start:.0f}s{RESET}"
                )
                sys.stdout.flush()
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            pass


def read_line(prompt: str = "") -> str | None:
    """Blocking input, off the event loop; None means EOF (Ctrl+D).

    `OSError` is caught as well as `EOFError`: a closed or unavailable stdin
    (piped input that ran out, a test harness) raises an OSError on some
    platforms, and an unanswered approval must mean "no", never a traceback.
    """
    try:
        return input(prompt)
    except (EOFError, OSError):
        return None


# --- the session ----------------------------------------------------------


class JarvisCLI:
    def __init__(self, args: argparse.Namespace, health: dict | None):
        self.url = args.url
        self.model = args.model or (health or {}).get("model") or "?"
        self.auto_approve = set(args.allow or [])
        if args.yes:
            self.auto_approve.update(PRE_APPROVED_TIERS)
        self.verbose = bool(args.verbose)
        self.health = health or {}
        self.plan: list = []
        self.changed: list[tuple[str, int, int]] = []
        self.tool_calls = 0
        self.turn_start = 0.0

    # --- one turn ---------------------------------------------------------

    async def ask(self, ws, text: str) -> bool:
        """Send one message and print everything that comes back.

        Returns False if the turn failed (the backend sent an error), which is
        what makes a one-shot run exit non-zero.
        """
        payload = {"type": "user_message", "text": text}
        if self.model and self.model != self.health.get("model"):
            payload["model"] = self.model
        await ws.send(json.dumps(payload))

        self.turn_start = time.monotonic()
        ticker = Ticker("working")
        ticker.start()
        printed = False
        ok = True
        try:
            async for raw in ws:
                event = json.loads(raw)
                kind = event.get("type")

                if kind == "token":
                    text_out = event.get("text", "")
                    if not text_out:
                        continue
                    if not printed:
                        ticker.clear()
                        sys.stdout.write(f"{BOLD}jarvis>{RESET} ")
                    sys.stdout.write(text_out)
                    sys.stdout.flush()
                    printed = True
                elif kind == "reasoning":
                    continue  # the model's private working; not worth the scroll
                elif kind == "tool_call":
                    ticker.clear()
                    if printed:
                        print()
                        printed = False
                    self.tool_calls += 1
                    print(
                        f"  {YELLOW}{TOOL_GLYPH} {event.get('name')}{RESET}"
                        f"{DIM}({format_arguments(event.get('arguments'))}){RESET}"
                    )
                elif kind == "tool_result":
                    self._record_result(event)
                    summary = summarize_result(event.get("name", ""), event.get("result"))
                    print(f"    {GREY}{RESULT_GLYPH} {RESET}{summary}")
                    render_result_body(
                        event.get("name", ""), event.get("result"), verbose=self.verbose
                    )
                elif kind == "tool_denied":
                    ticker.clear()
                    decision = event.get("decision", "declined")
                    print(
                        f"    {RED}{DENY_GLYPH} {event.get('name')} did not run "
                        f"({decision}): {event.get('reason')}{RESET}"
                    )
                elif kind == "plan":
                    ticker.clear()
                    self.plan = event.get("items", [])
                    render_plan(self.plan)
                elif kind == "confirm_request":
                    ticker.clear()
                    await self._answer_confirmation(ws, event)
                elif kind == "error":
                    ticker.clear()
                    if printed:
                        print()
                        printed = False
                    print(f"{RED}error: {event.get('message')}{RESET}")
                    ok = False
                    return ok
                elif kind == "reset_done":
                    ticker.clear()
                    print(f"{DIM}history cleared.{RESET}")
                    return ok
                elif kind == "done":
                    ticker.clear()
                    if printed:
                        print()
                    self._turn_summary()
                    return ok
        finally:
            ticker.clear()
        return ok

    def _record_result(self, event: dict) -> None:
        name = event.get("name")
        result = event.get("result")
        if name not in ("write_file", "edit_file") or not isinstance(result, dict):
            return
        if result.get("error") or result.get("changed") is False:
            return
        self.changed.append(
            (
                str(result.get("path", "?")),
                int(result.get("lines_added", 0)),
                int(result.get("lines_removed", 0)),
            )
        )

    def _turn_summary(self) -> None:
        if self.tool_calls == 0:
            # The single most useful line in the whole CLI: a turn that ran no
            # tool changed nothing, however confident the reply sounded.
            print(f"  {DIM}· no tools ran — nothing on disk changed{RESET}")
            return
        bits = [f"{self.tool_calls} tools", _since(self.turn_start)]
        if self.changed:
            bits.append(f"{len(self.changed)} files changed")
        print(f"  {DIM}· {' · '.join(bits)}{RESET}")

    # --- approvals --------------------------------------------------------

    async def _answer_confirmation(self, ws, event: dict) -> None:
        """Answer one `confirm_request`, showing what is about to happen.

        Tap tiers offer "allow this tier for the run" — the agent can then get
        on with a multi-file refactor without a prompt per file, while the
        backend still sees a perfectly normal approval for each call. Typed
        tiers are never offered that shortcut: their whole point is that a
        human retypes a phrase.
        """
        tier = event.get("risk", "?")
        mode = event.get("mode", "tap")
        arguments = format_arguments(event.get("arguments"))

        if mode != "typed" and tier in self.auto_approve:
            print(f"    {DIM}{OK_GLYPH} {event.get('name')} auto-approved ({tier}){RESET}")
            await ws.send(json.dumps({"type": "confirm_response", "approved": True}))
            return

        name = event.get("name")
        print(f"  {YELLOW}{WARN_GLYPH} {name}{RESET}{DIM}({arguments}){RESET} {DIM}[{tier}]{RESET}")
        preview = event.get("preview")
        if isinstance(preview, str) and preview.strip():
            print_diff(preview)

        if mode == "typed":
            challenge = event.get("challenge") or ""
            print(
                f"    {RED}type {challenge!r} to approve, "
                f"or press enter to refuse{RESET}"
            )
            typed = (await asyncio.to_thread(read_line, "    > ") or "").strip()
            await ws.send(
                json.dumps(
                    {
                        "type": "confirm_response",
                        "approved": bool(typed),
                        "challenge": typed,
                    }
                )
            )
            return

        answer = (await asyncio.to_thread(read_line, "    approve? [y/n/a] ") or "").strip().lower()
        if answer in ("a", "all"):
            self.auto_approve.add(tier)
            print(f"    {DIM}allowing every {tier} action for this run{RESET}")
            answer = "y"
        await ws.send(
            json.dumps(
                {"type": "confirm_response", "approved": answer in ("y", "yes")}
            )
        )

    # --- commands ---------------------------------------------------------

    async def command(self, ws, text: str) -> bool:
        """Handle a /command. Returns False if the user asked to quit."""
        parts = text.split(maxsplit=1)
        name = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if name in ("/quit", "/exit"):
            return False
        if name == "/help":
            print(HELP, end="")
        elif name == "/tools":
            self.print_tools()
        elif name == "/plan":
            render_plan(self.plan) if self.plan else print(f"{DIM}  no plan yet{RESET}")
        elif name == "/audit":
            self.print_audit()
        elif name == "/verbose":
            self.verbose = not self.verbose
            print(f"{DIM}  verbose {'on' if self.verbose else 'off'}{RESET}")
        elif name == "/model":
            await self.set_model(rest)
        elif name in ("/reset", "/new"):
            await self.reset_history(ws)
        else:
            print(f"{DIM}  unknown command {name} — /help for the list{RESET}")
        return True

    async def reset_history(self, ws) -> None:
        """Tell the backend to forget this conversation.

        `reset` is answered with a single `reset_done` frame — it is not a
        turn, so it must not go through `ask()` (which would send an empty
        `user_message` and get an error back).
        """
        await ws.send(json.dumps({"type": "reset"}))
        while True:
            event = json.loads(await ws.recv())
            if event.get("type") == "reset_done":
                break
        self.plan = []
        self.changed = []
        self.tool_calls = 0
        print(f"{DIM}  history cleared.{RESET}")

    async def set_model(self, name: str) -> None:
        if not name:
            print(f"  model {BOLD}{self.model}{RESET}")
            return
        self.model = name
        print(f"  {DIM}model set to {name} for the rest of this session{RESET}")

    def print_tools(self) -> None:
        risks = self.health.get("tool_risks") or {}
        tools = self.health.get("tools") or []
        if not tools:
            print(f"{DIM}  no tool list from the backend{RESET}")
            return
        for tool in tools:
            risk = risks.get(tool, "?")
            allowed = risk in self.auto_approve
            tag = f"{DIM}auto{RESET}" if allowed else f"{DIM}{risk}{RESET}"
            print(f"  {tool:<16} {tag}")
        checks = ", ".join(self.health.get("checks") or []) or "none"
        print(f"{DIM}  run_checks can run: {checks}{RESET}")

    def print_audit(self) -> None:
        entries = fetch_audit(self.url)
        if not entries:
            print(f"{DIM}  nothing recorded yet{RESET}")
            return
        for entry in entries:
            decision = entry.get("decision", "?")
            colour = RED if decision in ("declined", "disabled") else GREY
            arguments = json.dumps(entry.get("arguments") or {}, ensure_ascii=False)
            print(
                f"  {colour}{entry.get('ts')}  {entry.get('tool')}"
                f"({_clip(arguments, 60)})  [{entry.get('risk')}]  {decision}{RESET}"
            )

    # --- entry points -----------------------------------------------------

    async def connect(self):
        try:
            return await websockets.connect(self.url, open_timeout=5, max_size=None)
        except (OSError, asyncio.TimeoutError):
            sys.exit(
                f"Cannot reach the backend at {self.url}\nStart it first:\n"
                "    cd backend && .venv/Scripts/uvicorn app.main:app --port 8000"
            )

    def banner(self) -> None:
        root = self.health.get("tool_root") or "?"
        rounds = self.health.get("max_tool_rounds") or "?"
        print(f"{BOLD}Jarvis{RESET} {DIM}· agent mode{RESET}")
        print(f"{DIM}  brain   {self.model}{RESET}")
        print(f"{DIM}  root    {root}{RESET}")
        print(
            f"{DIM}  tools   {len(self.health.get('tools') or [])} "
            f"· up to {rounds} steps per turn · /help for commands{RESET}"
        )
        hint = small_brain_hint(self.model)
        if hint:
            print(hint)
        print()

    async def interactive(self) -> None:
        ws = await self.connect()
        async with ws:
            self.banner()
            while True:
                line = await asyncio.to_thread(read_line, f"{CYAN}you>{RESET} ")
                if line is None:
                    print()
                    return
                text = line.strip()
                if not text:
                    continue
                if text.startswith("/"):
                    if not await self.command(ws, text):
                        return
                    continue
                print()
                try:
                    await self.ask(ws, text)
                except websockets.ConnectionClosed:
                    print(f"{RED}connection lost.{RESET}")
                    return

    async def once(self, task: str) -> int:
        ws = await self.connect()
        async with ws:
            self.banner()
            print(f"{CYAN}you>{RESET} {task}")
            print()
            try:
                ok = await self.ask(ws, task)
            except websockets.ConnectionClosed:
                print(f"{RED}connection lost.{RESET}")
                return 1
            if self.changed:
                print()
                print(f"{BOLD}  files changed{RESET}")
                for path, added, removed in self.changed:
                    print(f"    {path}  {GREEN}+{added}{RESET}{RED}-{removed}{RESET}")
            return 0 if ok else 1


# --- entry point ----------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="Agentic terminal front-end to the Jarvis backend.",
        epilog='Examples:\n  jarvis_cli.py\n  jarvis_cli.py "fix the failing volume test"\n'
        '  jarvis_cli.py --model qwen2.5:7b --yes "add docstrings to tools/files.py"',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "task",
        nargs="*",
        help="a job to run once and exit (omit for interactive mode)",
    )
    parser.add_argument("--url", default=DEFAULT_WS_URL, help="backend WebSocket URL")
    parser.add_argument("--model", help="model for this run, e.g. qwen2.5:7b")
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "pre-approve tap-tier actions for this run, i.e. edits and checks, "
            "so the agent can work without a prompt per file. Typed tiers "
            "always require confirmation."
        ),
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        choices=sorted(RISK_TIERS),
        metavar="TIER",
        help=(
            "pre-approve one specific risk tier (repeatable): "
            + ", ".join(sorted(RISK_TIERS))
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="print full tool output")
    parser.add_argument(
        "--new", action="store_true", help="clear this session's history first"
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    health = fetch_health(args.url)
    cli = JarvisCLI(args, health)

    async def run() -> int:
        if args.new:
            ws = await cli.connect()
            async with ws:
                await cli.reset_history(ws)
        task = " ".join(args.task).strip()
        if task:
            return await cli.once(task)
        await cli.interactive()
        return 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
