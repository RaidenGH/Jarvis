"""`change_volume` tool: nudge system volume up/down, or mute it.

Uses the virtual volume keys through ctypes — plan §2.4's "low-level system
control" tier. No extra dependency, no shell string for a model to inject
into, and no COM plumbing to maintain.

Relative steps rather than an absolute level, because that is what you
actually say out loud ("turn it down", "mute") and it needs no read-back of
the current volume. An absolute `set_volume(40%)` would mean driving the Core
Audio API, which is a Phase 5 concern.
"""

import ctypes
import sys

from .base import REVERSIBLE_WRITE, Tool

VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
KEYEVENTF_KEYUP = 0x0002

DEFAULT_STEPS = 2
MAX_STEPS = 10  # one "step" is 2% on Windows; 10 keeps a single call sane


def _press(vk: int, presses: int) -> None:
    """Tap a virtual key `presses` times (Windows only; patched in tests)."""
    user32 = ctypes.windll.user32
    for _ in range(presses):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def change_volume(args: dict) -> dict:
    direction = args.get("direction")
    if direction not in ("up", "down", "mute"):
        return {"error": "direction must be 'up', 'down' or 'mute'"}

    if direction == "mute":
        if sys.platform != "win32":
            return {"error": "volume control is Windows-only for now"}
        _press(VK_VOLUME_MUTE, 1)
        return {"direction": "mute"}

    try:
        steps = int(args.get("steps", DEFAULT_STEPS))
    except (TypeError, ValueError):
        return {"error": "steps must be a whole number"}
    steps = max(1, min(steps, MAX_STEPS))

    if sys.platform != "win32":
        return {"error": "volume control is Windows-only for now"}

    _press(VK_VOLUME_UP if direction == "up" else VK_VOLUME_DOWN, steps)
    return {"direction": direction, "steps": steps}


CHANGE_VOLUME = Tool(
    name="change_volume",
    description=(
        "Turn the system volume up or down by a few steps, or mute it. "
        "Relative changes only — there is no way to set an exact level. "
        "Changes the device state, so it needs the user's confirmation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["up", "down", "mute"],
                "description": "Which way to change the volume.",
            },
            "steps": {
                "type": "integer",
                "description": "How many steps up/down (default 2, max 10).",
            },
        },
        "required": ["direction"],
        "additionalProperties": False,
    },
    handler=change_volume,
    risk=REVERSIBLE_WRITE,
)
