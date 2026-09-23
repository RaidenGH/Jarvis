"""Tool registry: the allow-list of actions the brain is allowed to call.

`tool_specs()` renders OpenAI function-calling JSON — the common currency
Ollama's /v1 endpoint speaks today (and the Anthropic translation layer can
consume in Phase 4).
"""

import json

from .apps import OPEN_APP
from .base import Tool, policy_for
from .files import READ_FILE, SEARCH_FILES
from .system import SYSTEM_STATS
from .volume import CHANGE_VOLUME

TOOLS: list[Tool] = [
    SYSTEM_STATS,
    READ_FILE,
    SEARCH_FILES,
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


def result_json(result: dict) -> str:
    """Compact JSON for the `role: "tool"` message the model reads back."""
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
    "result_json",
]
