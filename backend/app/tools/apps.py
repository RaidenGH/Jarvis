"""`open_app` tool: launch an allow-listed application by name.

Structured-API discipline (plan §2.4): the model supplies a *name*, never a
command line. There is deliberately no parameter for flags, paths, or shell
strings, and names that aren't on the allow-list are refused with the list
read back, so the brain can tell you what it is actually allowed to open
instead of guessing.

Extend the allow-list without touching code via `JARVIS_APP_ALLOWLIST`, a
comma-separated list of `name=target` pairs (e.g. `steam=steam,code=code`).
"""

import os
import shutil
import subprocess
import sys

from ..config import get_settings
from .base import REVERSIBLE_WRITE, Tool

#: Friendly name -> what to hand to the OS. Windows resolves these through
#: ShellExecute (App Paths + packaged-app aliases), so the plain executable
#: name is enough.
DEFAULT_APPS: dict[str, str] = {
    "notepad": "notepad",
    "calculator": "calc",
    "explorer": "explorer",
    "files": "explorer",
    "paint": "mspaint",
    "settings": "SystemSettings",
    "task manager": "Taskmgr",
    "taskmgr": "Taskmgr",
    "terminal": "wt",
    "browser": "msedge",
    "edge": "msedge",
    "chrome": "chrome",
    "code": "code",
}


def allowlist() -> dict[str, str]:
    """Built-in apps plus anything the user configured."""
    apps = dict(DEFAULT_APPS)
    for pair in (get_settings().app_allowlist or "").split(","):
        name, sep, target = pair.partition("=")
        if sep and name.strip() and target.strip():
            apps[name.strip().lower()] = target.strip()
    return apps


def _launch(target: str) -> None:
    """Start `target` with no arguments and no shell involved."""
    if sys.platform == "win32":
        os.startfile(target)  # noqa: S606 - ShellExecute by design
        return
    path = shutil.which(target)
    if path is None:
        raise FileNotFoundError(f"{target} not found on PATH")
    subprocess.Popen([path], close_fds=True)


def open_app(args: dict) -> dict:
    raw = args.get("name")
    if not isinstance(raw, str) or not raw.strip():
        return {"error": "name is required"}

    apps = allowlist()
    name = raw.strip().lower()
    if name not in apps:
        return {
            "error": f"{raw.strip()!r} is not in the app allow-list",
            "allowed": sorted(apps),
        }

    try:
        _launch(apps[name])
    except Exception as exc:  # noqa: BLE001 - report, don't crash the loop
        return {"error": f"could not open {name}: {type(exc).__name__}: {exc}"}
    return {"opened": name, "target": apps[name]}


OPEN_APP = Tool(
    name="open_app",
    description=(
        "Open an application on the user's PC by name. Only apps on the "
        "assistant's allow-list can be opened; anything else is refused. "
        "Changing what's on screen, so it needs the user's confirmation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "App to open, e.g. 'notepad' or 'chrome'.",
            }
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    handler=open_app,
    risk=REVERSIBLE_WRITE,
)
