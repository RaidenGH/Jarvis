#!/usr/bin/env python
"""Jarvis CLI — Phase 2.5 front-end to the backend's agent loop.

A terminal chat that talks to the same FastAPI/WebSocket backend the Flutter
shell uses, and shows the brain/tool loop working: when the model calls a
tool you watch the call and its result scroll by before the answer.

Run (backend must already be running):

    backend/.venv/Scripts/python jarvis_cli.py

Commands inside the chat:  /help  /tools  /reset  /quit
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
  /tools   list the tools the brain may call
  /reset   clear this conversation's history
  /help    show this help
  /quit    exit (Ctrl+C also works)
"""

MAX_RESULT_CHARS = 600


def color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{code}{text}{RESET}"


def read_line() -> str | None:
    """Blocking input on a worker thread; None means EOF (Ctrl+D)."""
    try:
        return input()
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


def print_tool_call(event: dict) -> None:
    args = json.dumps(event.get("arguments") or {}, ensure_ascii=False)
    print(color(f"  ⚙ {event.get('name')}({args})", YELLOW))


def print_tool_result(event: dict) -> None:
    result = json.dumps(event.get("result"), ensure_ascii=False, default=str)
    if len(result) > MAX_RESULT_CHARS:
        result = result[:MAX_RESULT_CHARS] + "…"
    print(color(f"  ↳ {result}", DIM))


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
                listed = (health or {}).get("tools", [])
                print(color("  " + (", ".join(listed) or "none"), DIM))
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
