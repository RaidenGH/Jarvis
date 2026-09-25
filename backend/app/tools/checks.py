"""Project checks: a fixed menu of commands the agent may run.

The plan's hard rule is that the model never gets a field where it can put a
free-form shell string (plan §2.3). `run_checks` keeps that rule and still
gives a coding agent the thing it actually needs to be useful: the ability to
run the project's tests and linters and read the result, instead of guessing
whether its edit worked.

There is no `command` argument. The only input is which check to run, and the
menu is this dict:

    pytest           backend suite
    flutter-test     app widget/unit tests
    flutter-analyze  app static analysis
    dart-format      app formatting check (exit 1 = files need formatting)

Every check runs with a fixed argv, no shell, a working directory inside the
sandbox root, and a timeout. Risk tier is `reversible-write` — running tests
executes code from the repo, so it goes through the approval gate like any
other non-read action.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..config import get_settings
from .base import REVERSIBLE_WRITE, Tool

#: Marker replaced with the interpreter running the backend, so `pytest` runs
#: in the same venv as the server rather than whatever is on PATH.
_PYTHON = "<python>"

CHECKS: dict[str, dict] = {
    "pytest": {
        "cwd": "backend",
        "argv": [_PYTHON, "-m", "pytest", "-q"],
        "about": "the backend test suite",
    },
    "flutter-test": {
        "cwd": "app",
        "argv": ["flutter", "test"],
        "about": "the Flutter widget/unit tests",
    },
    "flutter-analyze": {
        "cwd": "app",
        "argv": ["flutter", "analyze"],
        "about": "static analysis of the Flutter app",
    },
    "dart-format": {
        "cwd": "app",
        "argv": ["dart", "format", "--output=none", "--set-exit-if-changed", "lib", "test"],
        "about": "formatting check (exit 1 means some files need formatting)",
    },
}

MAX_OUTPUT_CHARS = 6_000
HEAD_CHARS = 1_500


def check_names() -> list[str]:
    return list(CHECKS)


def _trim(output: str) -> str:
    """Keep both ends of a long log: the summary is at the end, the command
    echo at the start, and the middle is usually the least interesting part."""
    if len(output) <= MAX_OUTPUT_CHARS:
        return output
    tail = MAX_OUTPUT_CHARS - HEAD_CHARS
    return (
        output[:HEAD_CHARS]
        + f"\n… [{len(output) - MAX_OUTPUT_CHARS} chars trimmed] …\n"
        + output[-tail:]
    )


def _resolved(argv: list[str]) -> list[str] | None:
    """Substitute the interpreter and resolve the executable.

    Windows ships `flutter`/`dart` as `.bat` shims, which CreateProcess won't
    launch directly, so batch shims are wrapped in `cmd.exe /c`.
    """
    args = [sys.executable if part == _PYTHON else part for part in argv]
    resolved = shutil.which(args[0]) or (args[0] if Path(args[0]).exists() else None)
    if resolved is None:
        return None
    args[0] = resolved
    if os.name == "nt" and resolved.lower().endswith((".bat", ".cmd")):
        args = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *args]
    return args


def run_checks(args: dict) -> dict:
    name = args.get("check")
    if not isinstance(name, str) or name not in CHECKS:
        return {
            "error": (
                f"unknown check {name!r}; choose one of: "
                + ", ".join(f"{key} ({value['about']})" for key, value in CHECKS.items())
            )
        }

    spec = CHECKS[name]
    root = Path(get_settings().tool_root).expanduser().resolve()
    cwd = root / spec["cwd"]
    if not cwd.is_dir():
        return {"error": f"'{spec['cwd']}' is not inside the allowed root ({root})"}

    argv = _resolved(spec["argv"])
    if argv is None:
        return {
            "error": (
                f"'{spec['argv'][0]}' is not installed or not on PATH, so "
                f"'{name}' can't run"
            )
        }

    timeout = float(get_settings().check_timeout_seconds)
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user string
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "check": name,
            "command": " ".join(argv),
            "timed_out": True,
            "error": f"'{name}' did not finish within {timeout:.0f}s",
            "output": _trim(_as_text(exc.stdout) + _as_text(exc.stderr)),
        }
    except OSError as exc:
        return {"error": f"could not run '{name}': {exc}"}

    output = _trim((proc.stdout or "") + (proc.stderr or ""))
    return {
        "check": name,
        "command": " ".join(argv),
        "exit_code": proc.returncode,
        "passed": proc.returncode == 0,
        "output": output or "(no output)",
    }


def _as_text(raw: object) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


RUN_CHECKS = Tool(
    name="run_checks",
    description=(
        "Run one of the project's own checks and get its output back. This is "
        "how you verify your work instead of assuming it worked. Available "
        "checks: "
        + "; ".join(f"{key} = {value['about']}" for key, value in CHECKS.items())
        + ". There are no other commands and no arguments to pass. Needs the "
        "user's approval."
    ),
    parameters={
        "type": "object",
        "properties": {
            "check": {
                "type": "string",
                "description": "Which check to run.",
                "enum": check_names(),
            }
        },
        "required": ["check"],
        "additionalProperties": False,
    },
    handler=run_checks,
    risk=REVERSIBLE_WRITE,
)
