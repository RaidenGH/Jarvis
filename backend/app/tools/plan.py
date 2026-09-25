"""The agent's own todo list.

Multi-step work goes wrong in a specific way: the model starts on step one,
reads six files, and by step four has forgotten what it was doing or which
parts it already finished. Writing the steps down fixes most of that, and it
gives the human running the CLI something to watch — you can see the plan
before the agent spends five minutes on it.

Read-only by design: it changes nothing on disk, so it never needs approval.
The agent loop picks the `plan` key out of the result and forwards it to the
client as a `plan` frame.
"""

from __future__ import annotations

from .base import Tool

STATUSES = ("pending", "in_progress", "done")
MAX_ITEMS = 20
MAX_TASK_CHARS = 200

#: The most recent plan. Single-user, localhost, one turn at a time — a module
#: global is the honest shape for this, and it keeps the tool handlers pure
#: functions of their arguments like every other tool.
_current: list[dict] = []


def current_plan() -> list[dict]:
    return [dict(item) for item in _current]


def clear_plan() -> None:
    _current.clear()


def update_plan(args: dict) -> dict:
    items = args.get("items")
    if not isinstance(items, list) or not items:
        return {"error": "items must be a non-empty list of {task, status}"}
    if len(items) > MAX_ITEMS:
        return {"error": f"keep the plan to {MAX_ITEMS} items or fewer"}

    cleaned: list[dict] = []
    for raw in items:
        if not isinstance(raw, dict):
            return {"error": "every item looks like {task: str, status: str}"}
        task = str(raw.get("task") or "").strip()
        if not task:
            return {"error": "every item needs a task"}
        status = str(raw.get("status") or "pending").strip().lower().replace(" ", "_")
        if status in ("in-progress", "active", "doing"):
            status = "in_progress"
        if status in ("complete", "completed", "finished"):
            status = "done"
        if status not in STATUSES:
            return {"error": f"status must be one of: {', '.join(STATUSES)}"}
        cleaned.append({"task": task[:MAX_TASK_CHARS], "status": status})

    _current[:] = cleaned
    return {
        "plan": current_plan(),
        "remaining": sum(1 for item in cleaned if item["status"] != "done"),
    }


UPDATE_PLAN = Tool(
    name="update_plan",
    description=(
        "Write down the steps for the task you are working on and keep them "
        "current. Call it as soon as a task needs more than a couple of steps, "
        "then call it again every time a step starts or finishes — exactly one "
        "item should be in_progress at a time. Mark items done only when they "
        "really are done. This changes nothing on disk."
    ),
    parameters={
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "The full plan, in order, replacing the previous one.",
                "items": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "One short, verifiable step.",
                        },
                        "status": {
                            "type": "string",
                            "enum": list(STATUSES),
                        },
                    },
                    "required": ["task", "status"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    },
    handler=update_plan,
)
