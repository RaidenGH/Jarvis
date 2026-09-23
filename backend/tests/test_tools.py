"""Tool registry tests: specs, sandboxing, error handling. No network, no LLM."""

from types import SimpleNamespace

from app.tools import execute_tool, tool_names, tool_specs
from app.tools.files import MAX_BYTES


def test_specs_use_openai_function_shape():
    specs = tool_specs()
    assert tool_names() == ["system_stats", "read_file"]
    for spec in specs:
        assert spec["type"] == "function"
        fn = spec["function"]
        assert isinstance(fn["name"], str) and fn["name"]
        assert isinstance(fn["description"], str) and fn["description"]
        assert fn["parameters"]["type"] == "object"


def test_system_stats_returns_expected_keys():
    result = execute_tool("system_stats", {})
    assert "error" not in result
    assert result["cpu"]["logical_cores"] >= 1
    assert result["disk"]["free_bytes"] > 0
    assert "memory" in result and "python" in result


def test_read_file_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.tools.files.get_settings",
        lambda: SimpleNamespace(tool_root=str(tmp_path)),
    )
    (tmp_path / "sub").mkdir()
    target = tmp_path / "sub" / "hello.txt"
    target.write_text("hi there", encoding="utf-8")

    rel = execute_tool("read_file", {"path": "sub/hello.txt"})
    assert rel == {"path": str(target), "bytes": 8, "content": "hi there"}

    absolute = execute_tool("read_file", {"path": str(target)})
    assert absolute["content"] == "hi there"


def test_read_file_rejects_paths_outside_root(tmp_path, monkeypatch):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setattr(
        "app.tools.files.get_settings",
        lambda: SimpleNamespace(tool_root=str(sandbox)),
    )
    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")

    relative_escape = execute_tool("read_file", {"path": "../secret.txt"})
    assert "outside the allowed root" in relative_escape["error"]

    absolute_escape = execute_tool("read_file", {"path": str(outside)})
    assert "outside the allowed root" in absolute_escape["error"]


def test_read_file_requires_path():
    assert "error" in execute_tool("read_file", {})
    assert "error" in execute_tool("read_file", {"path": "   "})


def test_read_file_rejects_oversized_files(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.tools.files.get_settings",
        lambda: SimpleNamespace(tool_root=str(tmp_path)),
    )
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (MAX_BYTES + 1))
    assert "too large" in execute_tool("read_file", {"path": "big.bin"})["error"]


def test_unknown_tool_is_an_error_not_a_crash():
    result = execute_tool("launch_missiles", {"target": "mars"})
    assert result == {"error": "unknown tool: launch_missiles"}


def test_handler_exceptions_become_error_results(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.tools.files.get_settings",
        lambda: SimpleNamespace(tool_root=str(tmp_path)),
    )
    result = execute_tool("read_file", {"path": "does-not-exist.txt"})
    assert "FileNotFoundError" in result["error"]
