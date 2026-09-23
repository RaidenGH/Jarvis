#!/usr/bin/env python
"""Jarvis CLI — Phase 2.5 front-end to the backend's agent loop.

A terminal chat that talks to the same FastAPI/WebSocket backend the Flutter
shell uses, and shows the brain/tool loop working: when the model calls a
tool you watch the call and its result scroll by before the answer.

Run (backend must already be running):

    backend/.venv/Scripts/python jarvis_cli.py

Commands inside the chat:  /help  /tools  /audit  /reset  /quit
History persists on the backend side (JARVIS_HISTORY_PATH), so restarting
the CLI or the backend keeps the conversation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request

try:
    import websockets
except ImportError:  # running with a Python that isn't the backend venv
    sys.exit(
        "Missing 'websockets'. Run me with the backend's venv:\n"
        "    backend/.venv/Scripts/python jarvis_cli.py"
    )

DEFAULT_WS_URL = os.environ.get(
    "JARVIS_WS_URL", "ws://127.0.0.1:8000/ws/cli"
)

RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"
RED = "\x1b[31m"

HELP = """\
Type a message and press enter. Commands:
  /tools   list the tools the brain may call, with their risk tiers
  /audit   show recent tool calls from the audit log
  /reset   clear this conversation's history
  /help    show this help
  /quit    exit (Ctrl+C also works)

Anything above the read-only risk tier needs your approval before it runs —
you'll be prompted right here.
"""

MAX_RESULT_CHARS = 600


def color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{code}{text}{RESET}"


def read_line(prompt: str = "") -> str | None:
    """Blocking input on a worker thread; None means EOF (Ctrl+D)."""
    try:
        return input(prompt)
    except EOFError:
        return None


def fetch_health(ws_url: str) -> dict | None:
    """GET /health next to the WS endpoint (stdlib only)."""
    parts = ws_url.split("/ws/")[0].replace("ws://", "http://", 1)
    try:
        with urllib.request.urlopen(parts + "/health", timeout=3) as resp:
            return json.loads(resp.read())
    except Exception:  # noqa: BLE001 - health is a nice-to-have
        return None


def fetch_audit(ws_url: str, limit: int = 10) -> list[dict]:
    """GET /audit next to the WS endpoint (stdlib only)."""
    parts = ws_url.split("/ws/")[0].replace("ws://", "http://", 1)
    try:
        with urllib.request.urlopen(f"{parts}/audit?limit={limit}", timeout=3) as resp:
            return json.loads(resp.read()).get("entries", [])
    except Exception:  # noqa: BLE001 - the log is a nice-to-have
        return []


def print_audit(entries: list[dict]) -> None:
    if not entries:
        print(color("  nothing recorded yet", DIM))
        return
    for entry in entries:
        decision = entry.get("decision", "?")
        args = json.dumps(entry.get("arguments") or {}, ensure_ascii=False)
        line = (
            f"  {entry.get('ts')}  {entry.get('tool')}({args})"
            f"  [{entry.get('risk')}]  {decision}"
        )
        print(color(line, RED if decision in ("declined", "disabled") else DIM))


def print_tool_call(event: dict) -> None:
    args = json.dumps(event.get("arguments") or {}, ensure_ascii=False)
    print(color(f"  ⚙ {event.get('name')}({args})", YELLOW))


def print_tool_result(event: dict) -> None:
    result = json.dumps(event.get("result"), ensure_ascii=False, default=str)
    if len(result) > MAX_RESULT_CHARS:
        result = result[:MAX_RESULT_CHARS] + "…"
    print(color(f"  ↳ {result}", DIM))


def print_tool_denied(event: dict) -> None:
    print(color(f"  ✋ {event.get('name')} did not run — {event.get('reason')}", RED))


def ask_confirmation(event: dict) -> dict:
    """Blocking prompt for a confirm_request; returns our confirm_response.

    Runs on a worker thread (the caller wraps it), because the socket must stay
    readable while the human thinks. Typed tiers require retyping the phrase
    exactly — the backend checks it, we just report what was typed.
    """
    args = json.dumps(event.get("arguments") or {}, ensure_ascii=False)
    print()
    print(color(f"  ⚠ {event.get('name')}({args})", YELLOW))
    print(color(f"    risk tier: {event.get('risk')}", DIM))

    if event.get("mode") == "typed":
        challenge = event.get("challenge") or ""
        print(
            color(
                f'    type "{challenge}" and press enter to approve, '
                "or just press enter to decline",
                RED,
            )
        )
        typed = (read_line("    > ") or "").strip()
        if not typed:
            return {"type": "confirm_response", "approved": False}
        return {
            "type": "confirm_response",
            "approved": True,
            "challenge": typed,
        }

    answer = (read_line("    approve? [y/N] ") or "").strip().lower()
    return {"type": "confirm_response", "approved": answer in ("y", "yes")}


async def consume_reply(ws) -> None:
    """Print frames until the backend says the turn is done."""
    async for raw in ws:
        event = json.loads(raw)
        kind = event.get("type")
        if kind == "token":
            print(event.get("text", ""), end="", flush=True)
        elif kind == "tool_call":
            print_tool_call(event)
        elif kind == "tool_result":
            print_tool_result(event)
        elif kind == "tool_denied":
            print_tool_denied(event)
        elif kind == "confirm_request":
            reply = await asyncio.to_thread(ask_confirmation, event)
            await ws.send(json.dumps(reply))
        elif kind == "done":
            print()
            return
        elif kind == "error":
            print(color(f"error: {event.get('message')}", RED))
            return
        elif kind == "reset_done":
            print(color("history cleared.", DIM))
            return


async def repl(ws_url: str, new: bool) -> None:
    try:
        ws = await websockets.connect(ws_url, open_timeout=5)
    except (OSError, asyncio.TimeoutError):
        sys.exit(
            "Cannot reach the backend at "
            f"{ws_url}\nStart it first:\n"
            "    cd backend && .venv/Scripts/uvicorn app.main:app --port 8000"
        )

    async with ws:
        if new:
            await ws.send(json.dumps({"type": "reset"}))
            await consume_reply(ws)

        health = await asyncio.to_thread(fetch_health, ws_url)
        brain = (
            f"{health['provider']}:{health['model']}"
            if health and "provider" in health
            else "unknown (backend not fully up?)"
        )
        tools = ", ".join((health or {}).get("tools", [])) or "none"
        print(color(f"Jarvis CLI — brain {brain}", BOLD))
        print(color(f"tools: {tools}   (/help for commands)", DIM))

        while True:
            print(color("you> ", CYAN), end="", flush=True)
            line = await asyncio.to_thread(read_line)
            if line is None:
                return
            text = line.strip()
            if not text:
                continue
            if text in ("/quit", "/exit"):
                return
            if text == "/help":
                print(HELP, end="")
                continue
            if text == "/tools":
                health = await asyncio.to_thread(fetch_health, ws_url)
                risks = (health or {}).get("tool_risks") or {}
                listed = (health or {}).get("tools", [])
                for name in listed:
                    print(color(f"  {name:<14} {risks.get(name, '?')}", DIM))
                if not listed:
                    print(color("  none", DIM))
                continue
            if text == "/audit":
                entries = await asyncio.to_thread(fetch_audit, ws_url)
                print_audit(entries)
                continue
            if text == "/reset":
                await ws.send(json.dumps({"type": "reset"}))
                await consume_reply(ws)
                continue

            await ws.send(json.dumps({"type": "user_message", "text": text}))
            print(color("jarvis> ", BOLD), end="", flush=True)
            try:
                await consume_reply(ws)
            except websockets.ConnectionClosed:
                print(color("connection lost.", RED))
                return


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis terminal chat")
    parser.add_argument(
        "--url", default=DEFAULT_WS_URL, help="backend WebSocket URL"
    )
    parser.add_argument(
        "--new", action="store_true", help="clear this session's history first"
    )
    args = parser.parse_args()
    try:
        asyncio.run(repl(args.url, args.new))
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
