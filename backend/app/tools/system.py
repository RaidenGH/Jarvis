"""`system_stats` tool: read-only CPU/memory/disk/platform snapshot.

Stdlib only (ctypes on Windows, /proc/meminfo elsewhere) — no psutil dep.
"""

import os
import platform
import shutil
import sys
from pathlib import Path

from .base import Tool

_GIB = 1024**3


def _memory() -> dict:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {}
        return {
            "used_percent": status.dwMemoryLoad,
            "total_bytes": status.ullTotalPhys,
            "available_bytes": status.ullAvailPhys,
        }

    # POSIX fallback so tests/tools behave off-Windows too.
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, sep, rest = line.partition(":")
            if sep and rest.split():
                values[key] = int(rest.split()[0]) * 1024
        return {
            "total_bytes": values.get("MemTotal"),
            "available_bytes": values.get("MemAvailable"),
        }
    except OSError:
        return {}


def _disk(path: Path) -> dict:
    usage = shutil.disk_usage(path)
    return {
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "used_percent": round(usage.used / usage.total * 100, 1),
    }


def system_stats(_args: dict) -> dict:
    return {
        "cpu": {
            "logical_cores": os.cpu_count(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "memory": _memory(),
        "disk": _disk(Path.home()),
        "python": platform.python_version(),
    }


SYSTEM_STATS = Tool(
    name="system_stats",
    description=(
        "Read current system stats: CPU core count, platform, memory usage, "
        "and free space on the home drive. Read-only."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    handler=system_stats,
)
