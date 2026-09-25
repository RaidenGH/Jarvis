"""Tool registry: the allow-list of actions the brain is allowed to call.

Ordered the way an agent should think about them: look, then remember, then
act, then check. `tool_specs()` renders OpenAI function-calling JSON — the
common currency Ollama's /v1 endpoint speaks today (and the Anthropic
translation layer can consume in Phase 4).
"""

import json

from .apps import OPEN_APP
from .base import Tool, policy_for
from .checks import RUN_CHECKS, check_names
from .files import (
    EDIT_FILE,
    GREP_FILES,
    LIST_DIR,
    READ_FILE,
    SEARCH_FILES,
    WRITE_FILE,
)
from .plan import UPDATE_PLAN, clear_plan, current_plan
from .system import SYSTEM_STATS
from .volume import CHANGE_VOLUME

TOOLS: list[Tool] = [
    # look
    SYSTEM_STATS,
    LIST_DIR,
    READ_FILE,
    SEARCH_FILES,
    GREP_FILES,
    # remember
    UPDATE_PLAN,
    # act
    WRITE_FILE,
    EDIT_FILE,
    # verify
    RUN_CHECKS,
    OPEN_APP,
    CHANGE_VOLUME,
]
_BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}


def get_tool(name: str) -> Tool | None:
    return _BY_NAME.get(name)


def tool_risks() -> dict[str, str]:
    """Tool name -> risk tier, for /health and client-side display."""
    return {t.name: t.risk for t in TOOLS}


def tool_specs() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in TOOLS
    ]


def tool_names() -> list[str]:
    return [t.name for t in TOOLS]


def execute_tool(name: str, arguments: dict) -> dict:
    """Run an allow-listed tool; never raises — errors come back as data."""
    tool = _BY_NAME.get(name)
    if tool is None:
        return {"error": f"unknown tool: {name}"}
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        result = tool.handler(arguments)
    except Exception as exc:  # noqa: BLE001 - report, don't crash the loop
        return {"error": f"{type(exc).__name__}: {exc}"}
    if isinstance(result, dict):
        return result
    return {"result": result}


def tool_preview(name: str, arguments: dict) -> str | None:
    """The approval-prompt preview for a call, or None if the tool has none.

    Never raises: a preview that blows up just means a plainer prompt, never a
    failed turn.
    """
    tool = _BY_NAME.get(name)
    if tool is None or tool.preview is None:
        return None
    try:
        return tool.preview(arguments if isinstance(arguments, dict) else {})
    except Exception as exc:  # noqa: BLE001 - cosmetic only
        return f"(preview unavailable: {type(exc).__name__})"


def result_json(result: dict) -> str:
    """Compact JSON for the `role: \"tool\"` message the model reads back."""
    return json.dumps(result, ensure_ascii=False, default=str)


__all__ = [
    "Tool",
    "TOOLS",
    "tool_specs",
    "tool_names",
    "tool_risks",
    "get_tool",
    "policy_for",
    "execute_tool",
    "tool_preview",
    "result_json",
    "check_names",
    "clear_plan",
    "current_plan",
]
