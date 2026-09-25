"""Tool registry tests: specs, sandboxing, error handling. No network, no LLM."""

from pathlib import Path
from types import SimpleNamespace

from app.tools import execute_tool, tool_names, tool_specs
from app.tools.files import MAX_BYTES, MAX_RESULTS


def _sandbox(monkeypatch, tmp_path) -> Path:
    """Point the filesystem tools at a throwaway directory."""
    monkeypatch.setattr(
        "app.tools.files.get_settings",
        lambda: SimpleNamespace(tool_root=str(tmp_path)),
    )
    return tmp_path


def test_specs_use_openai_function_shape():
    specs = tool_specs()
    assert tool_names() == [
        "system_stats",
        "list_dir",
        "read_file",
        "search_files",
        "grep_files",
        "update_plan",
        "write_file",
        "edit_file",
        "run_checks",
        "open_app",
        "change_volume",
    ]
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
    assert rel == {
        "path": str(target),
        "bytes": 8,
        "lines": 1,
        "content": "hi there",
    }

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


# --- search_files ---------------------------------------------------------


def test_search_files_finds_matches_relative_to_root(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "test_one.py").write_text("x", encoding="utf-8")
    (tmp_path / "other.py").write_text("x", encoding="utf-8")

    result = execute_tool("search_files", {"query": "test_"})
    # Tool paths are always POSIX-style, on every platform, so what the model
    # sees matches what the docs and tool descriptions use.
    assert result["matches"] == ["sub/test_one.py"]
    assert result["truncated"] is False
    assert result["searched"] == "."


def test_search_files_is_case_insensitive_and_scopes_to_a_subfolder(
    tmp_path, monkeypatch
):
    _sandbox(monkeypatch, tmp_path)
    for folder in ("sub", "other"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "Report.PDF").write_text("x", encoding="utf-8")

    everything = execute_tool("search_files", {"query": "report"})
    assert len(everything["matches"]) == 2

    scoped = execute_tool("search_files", {"query": "report", "path": "sub"})
    assert scoped["matches"] == ["sub/Report.PDF"]
    assert scoped["searched"] == "sub"


def test_search_files_skips_vendor_directories(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)
    for noise in (".git", "node_modules", "__pycache__", "build"):
        (tmp_path / noise).mkdir()
        (tmp_path / noise / "needle.py").write_text("x", encoding="utf-8")
    (tmp_path / "needle.py").write_text("x", encoding="utf-8")

    assert execute_tool("search_files", {"query": "needle"})["matches"] == [
        "needle.py"
    ]


def test_search_files_caps_results(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)
    for i in range(5):
        (tmp_path / f"match_{i}.txt").write_text("x", encoding="utf-8")

    result = execute_tool("search_files", {"query": "match_", "max_results": 2})
    assert len(result["matches"]) == 2
    assert result["truncated"] is True

    huge = execute_tool("search_files", {"query": "match_", "max_results": 99999})
    assert len(huge["matches"]) == min(5, MAX_RESULTS)


def test_search_files_validates_inputs(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)
    assert "error" in execute_tool("search_files", {})
    assert "error" in execute_tool("search_files", {"query": "   "})
    assert "error" in execute_tool(
        "search_files", {"query": "x", "max_results": "lots"}
    )


def test_search_files_cannot_escape_the_root(tmp_path, monkeypatch):
    sandbox = tmp_path / "inner"
    sandbox.mkdir()
    _sandbox(monkeypatch, sandbox)
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")

    escaped = execute_tool("search_files", {"query": "secret", "path": ".."})
    assert "outside the allowed root" in escaped["error"]

    into_file = execute_tool("search_files", {"query": "x", "path": "secret.txt"})
    assert "not a directory" in into_file["error"]


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
