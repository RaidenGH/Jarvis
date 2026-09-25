"""End-to-end tests for `jarvis_cli.py`, the terminal front-end.

The agent loop and the tool layer are covered elsewhere with stubs. What is
*not* covered there is the thing the user actually looks at: does the CLI
render a plan, show a diff before it writes, ask before it writes, auto-approve
when told to, and send the right frames back? A local stub backend answers
those questions exactly, without Ollama and without touching the repo.

The CLI is a script at the repo root, not a package, so it is imported off a
path insert rather than an install.
"""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import websockets

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import jarvis_cli  # noqa: E402  (needs the path insert above)

HEALTH = {
    "model": "test-model",
    "tools": ["read_file", "edit_file", "run_checks"],
    "tool_risks": {
        "read_file": "read-only",
        "edit_file": "reversible-write",
        "run_checks": "reversible-write",
    },
    "tool_root": "C:/repo",
    "max_tool_rounds": 25,
    "checks": ["pytest"],
}

DIFF = "--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,3 @@\n keep\n+added\n"


class StubBackend:
    """A scripted backend: each client message gets the next batch of frames.

    `replies` is a list of `(expected_type, frames)`. The first entry whose
    expected type matches what arrived is consumed and its frames sent, which
    is enough to express "answer the user, then answer the approval".
    """

    def __init__(self, replies: list[tuple[str | None, list[dict]]]):
        self.replies = list(replies)
        self.seen: list[dict] = []

    async def handler(self, ws) -> None:
        async for raw in ws:
            message = json.loads(raw)
            self.seen.append(message)
            for index, (expected, frames) in enumerate(self.replies):
                if expected is None or message.get("type") == expected:
                    self.replies.pop(index)
                    for frame in frames:
                        await ws.send(json.dumps(frame))
                    break

    def sent(self, kind: str) -> list[dict]:
        return [m for m in self.seen if m.get("type") == kind]


@asynccontextmanager
async def stub(backend: StubBackend):
    """Run `backend` on an ephemeral port and yield its WebSocket URL."""
    server = await websockets.serve(backend.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}/ws/cli"
    finally:
        server.close()
        await server.wait_closed()


def make_cli(url: str, *extra: str) -> jarvis_cli.JarvisCLI:
    return jarvis_cli.JarvisCLI(jarvis_cli.parse_args(["--url", url, *extra]), HEALTH)


def edit_frames() -> list[dict]:
    """A tool call plus the approval prompt that carries its diff."""
    arguments = {"path": "f.py", "find": "keep", "replace": "keep\nadded"}
    return [
        {
            "type": "tool_call",
            "id": "1",
            "name": "edit_file",
            "arguments": arguments,
        },
        {
            "type": "confirm_request",
            "id": "1",
            "name": "edit_file",
            "risk": "reversible-write",
            "mode": "tap",
            "arguments": arguments,
            "preview": DIFF,
        },
    ]


def result_frames() -> list[dict]:
    return [
        {
            "type": "tool_result",
            "id": "1",
            "name": "edit_file",
            "result": {
                "path": "f.py",
                "replaced": 1,
                "lines_added": 1,
                "lines_removed": 0,
                "diff": DIFF,
            },
        },
        {"type": "done"},
    ]


# --- the working turn -----------------------------------------------------


async def test_edit_asks_with_a_diff_then_reports_the_change(capsys, monkeypatch):
    """The user sees the diff *before* approving, and a summary after."""
    backend = StubBackend([(None, edit_frames()), ("confirm_response", result_frames())])
    monkeypatch.setattr(jarvis_cli, "read_line", lambda prompt="": "y")

    async with stub(backend) as url:
        assert await make_cli(url).once("rename that variable") == 0

    out = capsys.readouterr().out
    assert "+added" in out, "the diff has to be visible at the approval prompt"
    assert "[reversible-write]" in out, "the prompt has to name the risk tier"
    assert "replaced 1× in f.py" in out
    assert backend.sent("confirm_response") == [
        {"type": "confirm_response", "approved": True}
    ]


async def test_declining_sends_a_refusal_and_exits_zero(capsys, monkeypatch):
    backend = StubBackend(
        [(None, edit_frames()), ("confirm_response", [{"type": "done"}])]
    )
    monkeypatch.setattr(jarvis_cli, "read_line", lambda prompt="": "n")

    async with stub(backend) as url:
        assert await make_cli(url).once("edit something") == 0

    assert backend.sent("confirm_response") == [
        {"type": "confirm_response", "approved": False}
    ]


async def test_yes_pre_approves_edits_without_a_prompt(capsys):
    """`--yes` means the agent works through a multi-file change unattended."""
    backend = StubBackend([(None, edit_frames()), ("confirm_response", result_frames())])

    async with stub(backend) as url:
        assert await make_cli(url, "--yes").once("edit something") == 0

    out = capsys.readouterr().out
    assert "auto-approved (reversible-write)" in out
    assert "approve?" not in out
    assert backend.sent("confirm_response")[0]["approved"] is True


async def test_a_typed_tier_is_never_auto_approved(capsys, monkeypatch):
    """The whole point of a typed tier is that a flag cannot satisfy it."""
    challenge = "delete 3 files"
    frames = [
        {
            "type": "confirm_request",
            "id": "2",
            "name": "delete_thing",
            "risk": "destructive",
            "mode": "typed",
            "challenge": challenge,
            "arguments": {},
        }
    ]
    backend = StubBackend([(None, frames), ("confirm_response", [{"type": "done"}])])
    monkeypatch.setattr(jarvis_cli, "read_line", lambda prompt="": challenge)

    async with stub(backend) as url:
        await make_cli(url, "--allow", "destructive").once("delete things")

    out = capsys.readouterr().out
    assert "auto-approved" not in out
    assert backend.sent("confirm_response")[0]["challenge"] == challenge


async def test_read_only_tools_never_prompt(capsys):
    frames = [
        {"type": "tool_call", "id": "1", "name": "read_file", "arguments": {"path": "f.py"}},
        {
            "type": "tool_result",
            "id": "1",
            "name": "read_file",
            "result": {"path": "f.py", "lines": 12},
        },
        {"type": "token", "text": "It reads the file."},
        {"type": "done"},
    ]
    backend = StubBackend([(None, frames)])

    async with stub(backend) as url:
        await make_cli(url).once("read it")

    out = capsys.readouterr().out
    assert "12 lines · f.py" in out
    assert "jarvis> It reads the file." in out
    assert "approve?" not in out


async def test_a_denied_tool_is_reported_not_hidden(capsys):
    frames = [
        {"type": "tool_call", "id": "1", "name": "run_checks", "arguments": {"check": "pytest"}},
        {
            "type": "tool_denied",
            "id": "1",
            "name": "run_checks",
            "decision": "declined",
            "risk": "reversible-write",
            "reason": "the user did not confirm run_checks",
        },
        {"type": "done"},
    ]
    backend = StubBackend([(None, frames)])

    async with stub(backend) as url:
        await make_cli(url).once("run the tests")

    out = capsys.readouterr().out
    assert "did not run (declined)" in out


async def test_a_plan_frame_is_rendered(capsys):
    frames = [
        {
            "type": "plan",
            "items": [
                {"task": "find the bug", "status": "done"},
                {"task": "fix it", "status": "in_progress"},
                {"task": "run pytest", "status": "pending"},
            ],
        },
        {"type": "done"},
    ]
    backend = StubBackend([(None, frames)])

    async with stub(backend) as url:
        cli = make_cli(url)
        await cli.once("fix it")

    out = capsys.readouterr().out
    assert "plan 1/3" in out
    assert "▸ fix it" in out
    assert cli.plan[0]["status"] == "done"


async def test_a_turn_with_no_tools_says_so(capsys):
    """A confident-sounding reply that ran nothing must not read as work.

    This is the failure mode a small model falls into: it narrates a tool call
    and invents the output, and the user has no way to tell.
    """
    frames = [
        {"type": "token", "text": "Done — pytest passed."},
        {"type": "done"},
    ]
    backend = StubBackend([(None, frames)])

    async with stub(backend) as url:
        await make_cli(url).once("run the tests")

    assert "no tools ran" in capsys.readouterr().out


async def test_a_backend_error_exits_non_zero(capsys):
    backend = StubBackend([(None, [{"type": "error", "message": "model exploded"}])])

    async with stub(backend) as url:
        assert await make_cli(url).once("do it") == 1

    assert "model exploded" in capsys.readouterr().out


# --- commands -------------------------------------------------------------


async def test_reset_sends_reset_and_not_an_empty_message(capsys):
    """Regression: /reset used to go through the turn loop and send text="".

    The backend answers that with an "expected user_message" error, so the
    command has to be its own frame (`reset`, answered by `reset_done`).
    """
    backend = StubBackend([("reset", [{"type": "reset_done"}])])

    async with stub(backend) as url:
        cli = make_cli(url)
        ws = await cli.connect()
        async with ws:
            assert await cli.command(ws, "/reset") is True

    assert backend.seen == [{"type": "reset"}]
    assert "history cleared" in capsys.readouterr().out


async def test_quit_stops_the_loop():
    backend = StubBackend([])

    async with stub(backend) as url:
        cli = make_cli(url)
        ws = await cli.connect()
        async with ws:
            assert await cli.command(ws, "/quit") is False


# --- argument parsing -----------------------------------------------------


def test_yes_does_not_swallow_the_task():
    """Regression: `--yes` used to take an optional value, so argparse ate the
    task string as its tier and the run came out empty."""
    args = jarvis_cli.parse_args(["--model", "qwen2.5:7b", "--yes", "fix the bug"])

    assert args.task == ["fix the bug"]
    assert args.yes is True
    assert args.allow == []


def test_allow_names_a_tier_explicitly():
    args = jarvis_cli.parse_args(["--allow", "destructive", "fix the bug"])

    assert args.task == ["fix the bug"]
    assert args.allow == ["destructive"]


def test_an_unknown_tier_is_rejected():
    with pytest.raises(SystemExit):
        jarvis_cli.parse_args(["--allow", "whatever", "fix the bug"])


def test_small_brain_hint_only_fires_for_a_small_model():
    assert "small for agent work" in jarvis_cli.small_brain_hint("qwen3:4b")
    assert "small for agent work" in jarvis_cli.small_brain_hint("qwen2.5-coder:1.5b")
    assert jarvis_cli.small_brain_hint("qwen2.5:7b") is None
    assert jarvis_cli.small_brain_hint("qwen2.5-coder:14b") is None
    assert jarvis_cli.small_brain_hint("some-model") is None


# --- rendering helpers ----------------------------------------------------


def test_render_value_reports_a_payload_as_a_size():
    """A file body must not be pasted into the tool-call line."""
    assert jarvis_cli.render_value("content", "line\n" * 40) == "content=«200 chars»"


def test_summarize_result_reads_the_important_field():
    assert jarvis_cli.summarize_result("run_checks", {"check": "pytest", "exit_code": 0, "passed": True}).endswith(
        "exit 0 · passed"
    )
    assert "error" in jarvis_cli.summarize_result("read_file", {"error": "nope"})
