"""Tests for the coding tools: exploring, editing, verifying, planning.

The interesting assertions here are the *refusals*. An agent that can write
files is only as safe as its worst path check, so the sandbox, the vendor-dir
write guard, and the ambiguous-edit rejection all get pinned down.
"""

from types import SimpleNamespace

from app.tools import clear_plan, execute_tool
from app.tools.files import preview_edit, preview_write


def _sandbox(monkeypatch, tmp_path):
    """Point the filesystem tools at a throwaway directory."""
    monkeypatch.setattr(
        "app.tools.files.get_settings",
        lambda: SimpleNamespace(tool_root=str(tmp_path)),
    )
    return tmp_path


# --- list_dir -------------------------------------------------------------


def test_list_dir_puts_directories_first_with_sizes(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "readme.md").write_text("hello", encoding="utf-8")

    result = execute_tool("list_dir", {})

    assert [entry["name"] for entry in result["entries"]] == ["src", "readme.md"]
    assert result["entries"][0] == {"name": "src", "type": "dir", "bytes": None}
    assert result["entries"][1]["bytes"] == 5
    assert result["path"] == "."


def test_list_dir_rejects_paths_outside_the_root(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)

    assert "outside" in execute_tool("list_dir", {"path": "../.."})["error"]


# --- grep_files -----------------------------------------------------------


def test_grep_finds_content_with_line_numbers(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)
    (tmp_path / "a.py").write_text("one\ntwo\nTODO: fix this\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("nothing here\n", encoding="utf-8")

    result = execute_tool("grep_files", {"pattern": "todo"})

    assert result["matches"] == [{"path": "a.py", "line": 3, "text": "TODO: fix this"}]
    assert result["files_scanned"] == 2


def test_grep_filters_by_glob_and_skips_vendor_dirs(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)
    (tmp_path / "keep.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "skip.md").write_text("needle\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("needle\n", encoding="utf-8")

    by_glob = execute_tool("grep_files", {"pattern": "needle", "glob": "*.py"})
    assert [m["path"] for m in by_glob["matches"]] == ["keep.py"]

    everything = execute_tool("grep_files", {"pattern": "needle"})
    assert sorted(m["path"] for m in everything["matches"]) == ["keep.py", "skip.md"]


def test_grep_treats_an_invalid_regex_as_literal_text(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)
    (tmp_path / "a.py").write_text("x = dict[str, int]\n", encoding="utf-8")

    result = execute_tool("grep_files", {"pattern": "dict[str, int"})

    assert result["matches"][0]["path"] == "a.py"


def test_grep_caps_results_and_validates_input(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)
    (tmp_path / "a.py").write_text("hit\n" * 10, encoding="utf-8")

    capped = execute_tool("grep_files", {"pattern": "hit", "max_results": 3})
    assert len(capped["matches"]) == 3
    assert capped["truncated"] is True

    assert "error" in execute_tool("grep_files", {})
    assert "max_results" in execute_tool(
        "grep_files", {"pattern": "hit", "max_results": "lots"}
    )["error"]


def test_grep_skips_binary_files(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01needle\x00")

    result = execute_tool("grep_files", {"pattern": "needle"})

    assert result["matches"] == []


def test_grep_accepts_a_single_file(tmp_path, monkeypatch):
    """"Where does this appear in *this* file" must not need a directory."""
    _sandbox(monkeypatch, tmp_path)
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("needle\n", encoding="utf-8")

    result = execute_tool("grep_files", {"pattern": "needle", "path": "a.py"})

    assert [m["path"] for m in result["matches"]] == ["a.py"]
    assert result["files_scanned"] == 1


def test_grep_says_so_when_the_path_does_not_exist(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)

    result = execute_tool("grep_files", {"pattern": "x", "path": "missing.py"})

    assert "no such file or directory" in result["error"]


def test_grep_cannot_escape_the_root(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)

    assert "outside" in execute_tool("grep_files", {"pattern": "x", "path": ".."})["error"]


# --- write_file -----------------------------------------------------------


def test_write_file_creates_a_file_and_reports_a_diff(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)

    result = execute_tool(
        "write_file", {"path": "pkg/new.py", "content": "line one\nline two\n"}
    )

    assert result["created"] is True
    assert result["lines_added"] == 2
    assert result["lines_removed"] == 0
    assert "+line one" in result["diff"]
    assert (tmp_path / "pkg" / "new.py").read_text(encoding="utf-8") == "line one\nline two\n"


def test_write_file_replaces_and_diffs_against_the_old_content(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)
    (tmp_path / "a.txt").write_text("keep\ndrop\n", encoding="utf-8")

    result = execute_tool("write_file", {"path": "a.txt", "content": "keep\nadded\n"})

    assert result["created"] is False
    assert result["lines_added"] == 1
    assert result["lines_removed"] == 1
    assert "-drop" in result["diff"] and "+added" in result["diff"]


def test_write_file_is_a_no_op_when_content_is_identical(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)
    (tmp_path / "a.txt").write_text("same\n", encoding="utf-8")

    assert execute_tool("write_file", {"path": "a.txt", "content": "same\n"})["changed"] is False


def test_write_file_refuses_vendor_and_vcs_directories(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)

    for path in (".git/hooks/pre-commit", ".venv/lib/site.py", "node_modules/x.js"):
        result = execute_tool("write_file", {"path": path, "content": "nope"})
        assert "refusing to write" in result["error"], path


def test_write_file_refuses_paths_outside_the_root(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)

    assert "outside" in execute_tool("write_file", {"path": "../oops", "content": "x"})["error"]


def test_write_file_validates_its_arguments(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)

    assert "content must be a string" in execute_tool(
        "write_file", {"path": "a", "content": 5}
    )["error"]
    assert "path is required" in execute_tool("write_file", {"content": "x"})["error"]
    assert "path is a directory" in execute_tool(
        "write_file", {"path": ".", "content": "x"}
    )["error"]


# --- edit_file ------------------------------------------------------------


def test_edit_file_replaces_a_unique_snippet(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)
    target = tmp_path / "a.py"
    target.write_text("def hello():\n    return 1\n", encoding="utf-8")

    result = execute_tool(
        "edit_file",
        {"path": "a.py", "find": "    return 1", "replace": "    return 2"},
    )

    assert result["replaced"] == 1
    assert target.read_text(encoding="utf-8") == "def hello():\n    return 2\n"
    assert "-    return 1" in result["diff"]


def test_edit_file_refuses_a_snippet_that_is_not_there(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)
    (tmp_path / "a.py").write_text("real content\n", encoding="utf-8")

    result = execute_tool("edit_file", {"path": "a.py", "find": "made up", "replace": "x"})

    assert "not in a.py" in result["error"]
    assert "read the file" in result["error"]
    # And nothing was written.
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "real content\n"


def test_edit_file_refuses_an_ambiguous_snippet_unless_asked(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)
    (tmp_path / "a.py").write_text("pass\npass\n", encoding="utf-8")

    refused = execute_tool("edit_file", {"path": "a.py", "find": "pass", "replace": "ok"})
    assert "2 occurrences" in refused["error"]
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "pass\npass\n"

    forced = execute_tool(
        "edit_file",
        {"path": "a.py", "find": "pass", "replace": "ok", "replace_all": True},
    )
    assert forced["replaced"] == 2
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "ok\nok\n"


def test_edit_file_requires_the_file_to_exist(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)

    assert "no such file" in execute_tool(
        "edit_file", {"path": "nope.py", "find": "a", "replace": "b"}
    )["error"]


def test_edit_file_refuses_vendor_directories(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("core\n", encoding="utf-8")

    assert "refusing to write" in execute_tool(
        "edit_file", {"path": ".git/config", "find": "core", "replace": "x"}
    )["error"]


# --- approval previews ----------------------------------------------------


def test_preview_edit_shows_the_diff_without_writing(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)
    target = tmp_path / "a.py"
    target.write_text("value = 1\n", encoding="utf-8")

    preview = preview_edit({"path": "a.py", "find": "value = 1", "replace": "value = 2"})

    assert "-value = 1" in preview and "+value = 2" in preview
    assert target.read_text(encoding="utf-8") == "value = 1\n"  # untouched


def test_preview_says_when_the_edit_will_be_rejected(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)
    (tmp_path / "a.py").write_text("pass\npass\n", encoding="utf-8")

    missing = preview_edit({"path": "a.py", "find": "nope", "replace": "x"})
    assert "not found" in missing

    ambiguous = preview_edit({"path": "a.py", "find": "pass", "replace": "x"})
    assert "2x" in ambiguous


def test_preview_never_raises_on_junk():
    assert preview_write({}) is not None
    assert preview_edit({"path": "../outside", "find": "a", "replace": "b"}) is not None


# --- run_checks -----------------------------------------------------------


def test_run_checks_rejects_anything_not_on_the_menu(tmp_path, monkeypatch):
    _sandbox(monkeypatch, tmp_path)

    result = execute_tool("run_checks", {"check": "rm -rf /"})

    assert "unknown check" in result["error"]
    assert "pytest" in result["error"]  # the menu is spelled out


def _checks_sandbox(monkeypatch, root):
    monkeypatch.setattr(
        "app.tools.checks.get_settings",
        lambda: SimpleNamespace(tool_root=str(root), check_timeout_seconds=120.0),
    )


def test_run_checks_reports_a_missing_project_directory(tmp_path, monkeypatch):
    _checks_sandbox(monkeypatch, tmp_path)  # empty root: no backend/, no app/

    result = execute_tool("run_checks", {"check": "pytest"})

    assert "not inside the allowed root" in result["error"]


def test_run_checks_runs_pytest_in_the_project_directory(tmp_path, monkeypatch):
    """The plumbing end to end: cwd, interpreter, exit code, captured output.

    The throwaway root contains one trivial test, so this exercises the real
    subprocess without re-running this suite from inside itself.
    """
    _checks_sandbox(monkeypatch, tmp_path)
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )

    result = execute_tool("run_checks", {"check": "pytest"})

    assert result["check"] == "pytest"
    assert result["exit_code"] == 0
    assert result["passed"] is True
    assert "1 passed" in result["output"]


def test_run_checks_reports_a_failing_check(tmp_path, monkeypatch):
    _checks_sandbox(monkeypatch, tmp_path)
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "test_sample.py").write_text(
        "def test_nope():\n    assert False, 'boom'\n", encoding="utf-8"
    )

    result = execute_tool("run_checks", {"check": "pytest"})

    assert result["passed"] is False
    assert result["exit_code"] != 0
    assert "boom" in result["output"]


# --- update_plan ----------------------------------------------------------


def test_update_plan_stores_the_list_and_counts_the_work_left():
    clear_plan()

    result = execute_tool(
        "update_plan",
        {
            "items": [
                {"task": "read the failing test", "status": "done"},
                {"task": "fix it", "status": "in_progress"},
                {"task": "run pytest", "status": "pending"},
            ]
        },
    )

    assert result["remaining"] == 2
    assert [item["status"] for item in result["plan"]] == [
        "done",
        "in_progress",
        "pending",
    ]
    clear_plan()


def test_update_plan_validates_its_input():
    clear_plan()

    assert "non-empty" in execute_tool("update_plan", {"items": []})["error"]
    assert "task" in execute_tool("update_plan", {"items": [{"status": "done"}]})["error"]
    assert "status" in execute_tool(
        "update_plan", {"items": [{"task": "x", "status": "nearly"}]}
    )["error"]
    assert "plan" not in execute_tool("update_plan", {"items": "nope"})
