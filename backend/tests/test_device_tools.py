"""Device-tool tests: the open_app allow-list and volume key presses.

Nothing here launches a process or touches the machine's real audio — the
launcher and the key-tap are both stubbed.
"""

import sys
from types import SimpleNamespace

import pytest

from app.tools import execute_tool
from app.tools.volume import MAX_STEPS, VK_VOLUME_DOWN, VK_VOLUME_MUTE, VK_VOLUME_UP


@pytest.fixture
def no_process(monkeypatch):
    """Fail loudly if a test actually tries to start something."""

    def boom(target):
        pytest.fail(f"should not have launched {target!r}")

    monkeypatch.setattr("app.tools.apps._launch", boom)


@pytest.fixture
def pressed(monkeypatch):
    """Record volume key taps instead of sending them."""
    taps: list[tuple[int, int]] = []
    monkeypatch.setattr("app.tools.volume._press", lambda vk, n: taps.append((vk, n)))
    return taps


# --- open_app -------------------------------------------------------------


def test_open_app_launches_an_allowlisted_app(monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr("app.tools.apps._launch", launched.append)

    assert execute_tool("open_app", {"name": "notepad"}) == {
        "opened": "notepad",
        "target": "notepad",
    }
    assert launched == ["notepad"]


def test_open_app_is_case_and_space_insensitive(monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr("app.tools.apps._launch", launched.append)

    assert execute_tool("open_app", {"name": "  NotePad  "})["opened"] == "notepad"
    assert execute_tool("open_app", {"name": "Task Manager"})["target"] == "Taskmgr"


def test_open_app_refuses_anything_off_the_list(no_process):
    result = execute_tool("open_app", {"name": "powershell"})
    assert "not in the app allow-list" in result["error"]
    assert "notepad" in result["allowed"]  # the brain can see what is allowed


def test_open_app_will_not_take_a_path_or_a_command(no_process):
    """A name that isn't on the list is refused, path or not."""
    for name in (r"C:\Windows\System32\cmd.exe", "cmd /c calc", "..\\evil.exe", ""):
        assert "error" in execute_tool("open_app", {"name": name}), name


def test_open_app_allowlist_extends_from_config(monkeypatch):
    monkeypatch.setattr(
        "app.tools.apps.get_settings",
        lambda: SimpleNamespace(app_allowlist="steam=steam, code = code"),
    )
    launched: list[str] = []
    monkeypatch.setattr("app.tools.apps._launch", launched.append)

    assert execute_tool("open_app", {"name": "steam"})["target"] == "steam"
    assert execute_tool("open_app", {"name": "code"})["target"] == "code"
    assert launched == ["steam", "code"]


def test_open_app_reports_a_launch_failure(monkeypatch):
    def boom(_target):
        raise OSError("no such file")

    monkeypatch.setattr("app.tools.apps._launch", boom)
    assert "could not open" in execute_tool("open_app", {"name": "notepad"})["error"]


# --- change_volume --------------------------------------------------------


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")


def test_change_volume_taps_the_right_key(windows, pressed):
    assert execute_tool("change_volume", {"direction": "up"}) == {
        "direction": "up",
        "steps": 2,
    }
    assert execute_tool("change_volume", {"direction": "down", "steps": 3}) == {
        "direction": "down",
        "steps": 3,
    }
    assert pressed == [(VK_VOLUME_UP, 2), (VK_VOLUME_DOWN, 3)]


def test_change_volume_clamps_steps(windows, pressed):
    assert execute_tool("change_volume", {"direction": "up", "steps": 99})["steps"] == (
        MAX_STEPS
    )
    assert execute_tool("change_volume", {"direction": "up", "steps": 0})["steps"] == 1
    assert pressed == [(VK_VOLUME_UP, MAX_STEPS), (VK_VOLUME_UP, 1)]


def test_change_volume_mute_taps_once(windows, pressed):
    assert execute_tool("change_volume", {"direction": "mute"}) == {"direction": "mute"}
    assert pressed == [(VK_VOLUME_MUTE, 1)]


def test_change_volume_rejects_nonsense(pressed):
    assert "error" in execute_tool("change_volume", {})
    assert "error" in execute_tool("change_volume", {"direction": "sideways"})
    assert "error" in execute_tool("change_volume", {"direction": "up", "steps": "loud"})
    assert pressed == []


def test_change_volume_is_windows_only(monkeypatch, pressed):
    monkeypatch.setattr(sys, "platform", "linux")
    assert "Windows-only" in execute_tool("change_volume", {"direction": "up"})["error"]
    assert pressed == []
