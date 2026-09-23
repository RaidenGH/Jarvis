"""Shared test setup: keep tests hermetic, and share the fake-tool helpers.

Blank the history file so lifespan-built SessionStores never write JSON to
disk during test runs (must happen before app.main is first imported).
"""

import contextlib
import os

os.environ["JARVIS_HISTORY_PATH"] = ""
os.environ["JARVIS_AUDIT_PATH"] = ""

from app.tools import TOOLS, Tool, _BY_NAME  # noqa: E402 - after the env above


def make_tool(name: str, risk: str, ran: list | None = None) -> Tool:
    """A do-nothing tool at a given risk tier."""

    def handler(_args: dict) -> dict:
        if ran is not None:
            ran.append(name)
        return {"ran": name}

    return Tool(
        name=name,
        description=f"test tool {name}",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=handler,
        risk=risk,
    )


@contextlib.contextmanager
def registered(tool: Tool):
    """Temporarily add a tool to the allow-list the way a real one is added."""
    TOOLS.append(tool)
    _BY_NAME[tool.name] = tool
    try:
        yield tool
    finally:
        TOOLS.remove(tool)
        _BY_NAME.pop(tool.name, None)
