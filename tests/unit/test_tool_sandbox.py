"""Tests for orchestrator/tool_sandbox.py."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from orchestrator.tool_sandbox import ToolError, ToolSandbox, get_tool_definitions


@pytest.fixture
def workspace(tmp_path):
    """Create a minimal workspace directory structure."""
    source = tmp_path / "source"
    source.mkdir()
    reports = tmp_path / "reports"
    reports.mkdir()
    meta = tmp_path / "meta"
    meta.mkdir()

    # Create some source files
    (source / "main.py").write_text("print('hello')\n")
    (source / "utils").mkdir()
    (source / "utils" / "helper.py").write_text("def help(): pass\n")

    # Create a report
    (reports / "ba.md").write_text("# BA Report\n")

    return tmp_path


@pytest.fixture
def sandbox(workspace):
    """Create a sandbox with all tools allowed."""
    return ToolSandbox(
        workspace_root=str(workspace),
        allowed_tools=["read_file", "write_file", "list_directory", "search_code", "run_command", "git_operation"],
    )


@pytest.fixture
def readonly_sandbox(workspace):
    """Create a sandbox with read-only tools (like BA agent)."""
    return ToolSandbox(
        workspace_root=str(workspace),
        allowed_tools=["read_file", "list_directory", "search_code"],
    )


def run(coro):
    """Helper to run async functions in sync tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


class TestSandboxInit:
    def test_creates_with_valid_tools(self, workspace):
        sandbox = ToolSandbox(str(workspace), ["read_file", "write_file"])
        assert sandbox is not None

    def test_unknown_tool_raises(self, workspace):
        with pytest.raises(ToolError, match="Unknown tools"):
            ToolSandbox(str(workspace), ["read_file", "hack_system"])

    def test_empty_allowlist(self, workspace):
        sandbox = ToolSandbox(str(workspace), [])
        assert sandbox is not None

    def test_protected_files_stored(self, workspace):
        sandbox = ToolSandbox(str(workspace), ["read_file"], protected_files=["config.yaml"])
        assert sandbox._protected_files == {"config.yaml"}


class TestToolAllowlist:
    def test_allowed_tool_executes(self, sandbox, workspace):
        result = run(sandbox.execute_tool("read_file", {"path": "main.py"}))
        assert "hello" in result

    def test_disallowed_tool_raises(self, readonly_sandbox):
        with pytest.raises(ToolError, match="not in this agent's allowlist"):
            run(readonly_sandbox.execute_tool("write_file", {"path": "x.py", "content": "bad"}))

    def test_unknown_tool_raises(self, sandbox):
        with pytest.raises(ToolError, match="Unknown tool"):
            run(sandbox.execute_tool("delete_everything", {}))


class TestReadFile:
    def test_read_source_file(self, sandbox):
        result = run(sandbox.execute_tool("read_file", {"path": "main.py"}))
        assert "print('hello')" in result

    def test_read_nested_file(self, sandbox):
        result = run(sandbox.execute_tool("read_file", {"path": "utils/helper.py"}))
        assert "def help" in result

    def test_read_report(self, sandbox):
        result = run(sandbox.execute_tool("read_file", {"path": "reports/ba.md"}))
        assert "BA Report" in result

    def test_read_missing_file(self, sandbox):
        with pytest.raises(ToolError, match="File not found"):
            run(sandbox.execute_tool("read_file", {"path": "nonexistent.py"}))

    def test_read_no_path(self, sandbox):
        with pytest.raises(ToolError, match="requires 'path'"):
            run(sandbox.execute_tool("read_file", {}))


class TestWriteFile:
    def test_write_source_file(self, sandbox, workspace):
        run(sandbox.execute_tool("write_file", {"path": "new.py", "content": "x = 1\n"}))
        assert (workspace / "source" / "new.py").read_text() == "x = 1\n"

    def test_write_creates_directories(self, sandbox, workspace):
        run(sandbox.execute_tool("write_file", {"path": "deep/nested/file.py", "content": "ok"}))
        assert (workspace / "source" / "deep" / "nested" / "file.py").exists()

    def test_write_report(self, sandbox, workspace):
        run(sandbox.execute_tool("write_file", {"path": "reports/dev.md", "content": "# Dev\n"}))
        assert (workspace / "reports" / "dev.md").read_text() == "# Dev\n"

    def test_write_no_path(self, sandbox):
        with pytest.raises(ToolError, match="requires 'path'"):
            run(sandbox.execute_tool("write_file", {"content": "x"}))

    def test_write_protected_file(self, workspace):
        sandbox = ToolSandbox(
            str(workspace),
            ["write_file"],
            protected_files=["config.yaml"],
        )
        with pytest.raises(ToolError, match="protected"):
            run(sandbox.execute_tool("write_file", {"path": "config.yaml", "content": "bad"}))

    def test_write_protected_nested(self, workspace):
        sandbox = ToolSandbox(
            str(workspace),
            ["write_file"],
            protected_files=[".github"],
        )
        with pytest.raises(ToolError, match="protected"):
            run(sandbox.execute_tool("write_file", {"path": ".github/workflows/ci.yml", "content": "bad"}))


class TestPathTraversal:
    def test_dotdot_blocked(self, sandbox):
        with pytest.raises(ToolError, match="escapes workspace"):
            run(sandbox.execute_tool("read_file", {"path": "../../etc/passwd"}))

    def test_absolute_path_blocked(self, sandbox):
        with pytest.raises(ToolError, match="escapes workspace"):
            run(sandbox.execute_tool("read_file", {"path": "/etc/passwd"}))

    def test_dotdot_in_middle_blocked(self, sandbox):
        with pytest.raises(ToolError, match="escapes workspace"):
            run(sandbox.execute_tool("read_file", {"path": "utils/../../etc/passwd"}))

    def test_write_traversal_blocked(self, sandbox):
        with pytest.raises(ToolError, match="escapes workspace"):
            run(sandbox.execute_tool("write_file", {"path": "../escape.py", "content": "bad"}))


class TestListDirectory:
    def test_list_root(self, sandbox):
        result = run(sandbox.execute_tool("list_directory", {"path": "."}))
        assert "main.py" in result
        assert "utils/" in result

    def test_list_subdir(self, sandbox):
        result = run(sandbox.execute_tool("list_directory", {"path": "utils"}))
        assert "helper.py" in result

    def test_list_missing(self, sandbox):
        with pytest.raises(ToolError, match="not found"):
            run(sandbox.execute_tool("list_directory", {"path": "nonexistent"}))

    def test_list_file_not_dir(self, sandbox):
        with pytest.raises(ToolError, match="Not a directory"):
            run(sandbox.execute_tool("list_directory", {"path": "main.py"}))


class TestSearchCode:
    def test_search_finds_pattern(self, sandbox):
        result = run(sandbox.execute_tool("search_code", {"pattern": "hello"}))
        assert "main.py" in result

    def test_search_no_match(self, sandbox):
        result = run(sandbox.execute_tool("search_code", {"pattern": "zzz_no_match_zzz"}))
        assert "No matches" in result

    def test_search_no_pattern(self, sandbox):
        with pytest.raises(ToolError, match="requires 'pattern'"):
            run(sandbox.execute_tool("search_code", {}))

    def test_search_with_glob(self, sandbox):
        result = run(sandbox.execute_tool("search_code", {"pattern": "def", "glob": "*.py"}))
        assert "helper.py" in result


class TestRunCommand:
    def test_echo(self, sandbox):
        result = run(sandbox.execute_tool("run_command", {"command": "echo test123"}))
        assert "test123" in result

    def test_exit_code_reported(self, sandbox):
        result = run(sandbox.execute_tool("run_command", {"command": "false"}))
        assert "Exit code: 1" in result

    def test_no_command(self, sandbox):
        with pytest.raises(ToolError, match="requires 'command'"):
            run(sandbox.execute_tool("run_command", {}))

    def test_cwd_is_source(self, sandbox, workspace):
        result = run(sandbox.execute_tool("run_command", {"command": "pwd"}))
        assert str(workspace / "source") in result


class TestGitOperation:
    def test_status(self, sandbox, workspace):
        # Init git repo in source for testing
        os.system(f"cd {workspace / 'source'} && git init -q && git add . && git commit -q -m init")
        result = run(sandbox.execute_tool("git_operation", {"command": "status"}))
        assert "branch" in result.lower() or "clean" in result.lower()

    def test_diff(self, sandbox, workspace):
        os.system(f"cd {workspace / 'source'} && git init -q && git add . && git commit -q -m init")
        result = run(sandbox.execute_tool("git_operation", {"command": "diff"}))
        # Should succeed even with no diff
        assert result is not None

    def test_disallowed_subcommand(self, sandbox):
        with pytest.raises(ToolError, match="not allowed"):
            run(sandbox.execute_tool("git_operation", {"command": "push origin main"}))

    def test_reset_blocked(self, sandbox):
        with pytest.raises(ToolError, match="not allowed"):
            run(sandbox.execute_tool("git_operation", {"command": "reset --hard HEAD"}))

    def test_no_command(self, sandbox):
        with pytest.raises(ToolError, match="requires 'command'"):
            run(sandbox.execute_tool("git_operation", {}))


class TestCallLog:
    def test_log_records_calls(self, sandbox):
        run(sandbox.execute_tool("read_file", {"path": "main.py"}))
        assert len(sandbox.call_log) == 1
        assert sandbox.call_log[0]["tool"] == "read_file"
        assert sandbox.call_log[0]["success"] is True

    def test_log_records_failures(self, sandbox):
        with pytest.raises(ToolError):
            run(sandbox.execute_tool("read_file", {"path": "nonexistent"}))
        assert len(sandbox.call_log) == 1
        assert sandbox.call_log[0]["success"] is False

    def test_log_is_copy(self, sandbox):
        run(sandbox.execute_tool("read_file", {"path": "main.py"}))
        log = sandbox.call_log
        run(sandbox.execute_tool("read_file", {"path": "main.py"}))
        assert len(log) == 1  # original copy unchanged
        assert len(sandbox.call_log) == 2


class TestGetToolDefinitions:
    def test_returns_allowed_only(self):
        defs = get_tool_definitions(["read_file", "write_file"])
        assert len(defs) == 2
        names = {d["name"] for d in defs}
        assert names == {"read_file", "write_file"}

    def test_all_tools(self):
        defs = get_tool_definitions(["read_file", "write_file", "list_directory", "search_code", "run_command", "git_operation"])
        assert len(defs) == 6

    def test_empty_list(self):
        defs = get_tool_definitions([])
        assert defs == []

    def test_definitions_have_schema(self):
        defs = get_tool_definitions(["read_file"])
        assert "input_schema" in defs[0]
        assert defs[0]["input_schema"]["type"] == "object"
