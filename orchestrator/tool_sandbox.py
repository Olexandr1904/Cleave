"""Tool sandbox — sandboxed tool execution for agents within workspace restrictions."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import yaml

from integrations.config import config_tools

logger = logging.getLogger(__name__)

# All available tools
ALL_TOOLS = {
    "read_file",
    "write_file",
    "list_directory",
    "search_code",
    "run_command",
    "git_operation",
    # Config management tools (project-setup-agent)
    "validate_jira",
    "validate_github",
    "validate_gitlab",
    "validate_jenkins",
    "list_projects",
    "read_project_config",
    "write_project_config",
    "write_repo_config",
    "remove_project",
}


class ToolError(Exception):
    """Raised when a tool call fails validation or execution."""


class ToolSandbox:
    """Executes tool calls within workspace restrictions.

    All file operations are confined to the workspace's source/ and reports/
    directories. Protected files cannot be written. Only tools in the agent's
    allowlist can be called.
    """

    def __init__(
        self,
        workspace_root: str,
        allowed_tools: list[str],
        protected_files: list[str] | None = None,
    ) -> None:
        self._root = Path(workspace_root).resolve()
        self._source_dir = self._root / "source"
        self._reports_dir = self._root / "reports"
        self._allowed_tools = set(allowed_tools)
        self._protected_files = set(protected_files or [])
        self._call_log: list[dict[str, Any]] = []

        # Validate allowlist
        unknown = self._allowed_tools - ALL_TOOLS
        if unknown:
            raise ToolError(f"Unknown tools in allowlist: {unknown}")

    @property
    def call_log(self) -> list[dict[str, Any]]:
        return list(self._call_log)

    async def execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Execute a sandboxed tool call.

        Args:
            tool_name: Name of the tool to execute.
            tool_input: Tool-specific input parameters.

        Returns:
            Tool output as a string.

        Raises:
            ToolError: If the tool is not allowed, path is invalid, or file is protected.
        """
        if tool_name not in ALL_TOOLS:
            raise ToolError(f"Unknown tool: {tool_name}")
        if tool_name not in self._allowed_tools:
            raise ToolError(
                f"Tool '{tool_name}' is not in this agent's allowlist. "
                f"Allowed: {sorted(self._allowed_tools)}"
            )

        handler = getattr(self, f"_tool_{tool_name}")
        try:
            result = await handler(tool_input)
            self._log_call(tool_name, tool_input, result, success=True)
            return result
        except ToolError:
            self._log_call(tool_name, tool_input, "", success=False)
            raise
        except Exception as e:
            self._log_call(tool_name, tool_input, str(e), success=False)
            raise ToolError(f"Tool '{tool_name}' failed: {e}") from e

    def _log_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        result: str,
        success: bool,
    ) -> None:
        entry = {
            "tool": tool_name,
            "input": tool_input,
            "success": success,
            "result_length": len(result),
        }
        self._call_log.append(entry)
        status = "OK" if success else "FAIL"
        logger.info("Tool call [%s] %s: %s", status, tool_name, tool_input)

    # --- Path validation ---

    def _resolve_path(self, file_path: str, allow_reports: bool = False) -> Path:
        """Resolve and validate a file path within workspace boundaries.

        Args:
            file_path: Relative path (from workspace source or reports root).
            allow_reports: If True, also allow paths in reports/.

        Returns:
            Resolved absolute path.

        Raises:
            ToolError: If path escapes workspace boundaries.
        """
        # Block absolute paths explicitly
        if file_path.startswith("/"):
            raise ToolError(
                f"Path '{file_path}' escapes workspace boundaries. "
                f"Files must be within source/ or reports/."
            )

        clean = file_path
        resolved: Path | None = None

        # Check if path starts with "reports/" explicitly
        if clean.startswith("reports/") and allow_reports:
            resolved = (self._reports_dir / clean[len("reports/"):]).resolve()
            if self._is_within(resolved, self._reports_dir):
                return resolved

        # Default: resolve relative to source/
        resolved = (self._source_dir / clean).resolve()
        if self._is_within(resolved, self._source_dir):
            return resolved

        # If allow_reports, also try reports/ as fallback
        if allow_reports:
            resolved = (self._reports_dir / clean).resolve()
            if self._is_within(resolved, self._reports_dir):
                return resolved

        raise ToolError(
            f"Path '{file_path}' escapes workspace boundaries. "
            f"Files must be within source/ or reports/."
        )

    def _resolve_read_path(self, file_path: str) -> Path:
        """Resolve path for reading — allowed in source/ and reports/."""
        return self._resolve_path(file_path, allow_reports=True)

    def _resolve_write_path(self, file_path: str) -> Path:
        """Resolve path for writing — allowed in source/ and reports/."""
        resolved = self._resolve_path(file_path, allow_reports=True)
        self._check_protected(resolved)
        return resolved

    def _is_within(self, path: Path, parent: Path) -> bool:
        """Check if resolved path is within the parent directory."""
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def _check_protected(self, path: Path) -> None:
        """Check if path matches any protected file pattern."""
        # Get path relative to source dir for matching
        try:
            rel = str(path.relative_to(self._source_dir))
        except ValueError:
            return  # Not in source dir, skip protected check (reports are fine)

        for pattern in self._protected_files:
            if rel == pattern or rel.startswith(pattern + "/"):
                raise ToolError(
                    f"File '{rel}' is protected by architecture rules and cannot be modified."
                )

    # --- Tool implementations ---

    async def _tool_read_file(self, params: dict[str, Any]) -> str:
        """Read a file from workspace source/ or reports/."""
        file_path = params.get("path", "")
        if not file_path:
            raise ToolError("read_file requires 'path' parameter")

        resolved = self._resolve_read_path(file_path)
        if not resolved.exists():
            raise ToolError(f"File not found: {file_path}")
        if not resolved.is_file():
            raise ToolError(f"Not a file: {file_path}")

        content = resolved.read_text(encoding="utf-8", errors="replace")

        # Limit output size to prevent context explosion
        max_size = 100_000
        if len(content) > max_size:
            content = content[:max_size] + f"\n\n... (truncated, file is {len(content)} bytes)"

        return content

    async def _tool_write_file(self, params: dict[str, Any]) -> str:
        """Write a file in workspace source/ or reports/."""
        file_path = params.get("path", "")
        content = params.get("content", "")
        if not file_path:
            raise ToolError("write_file requires 'path' parameter")

        resolved = self._resolve_write_path(file_path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"Written {len(content)} bytes to {file_path}"

    async def _tool_list_directory(self, params: dict[str, Any]) -> str:
        """List directory contents in workspace source/."""
        dir_path = params.get("path", ".")
        resolved = self._resolve_read_path(dir_path)

        if not resolved.exists():
            raise ToolError(f"Directory not found: {dir_path}")
        if not resolved.is_dir():
            raise ToolError(f"Not a directory: {dir_path}")

        entries = sorted(resolved.iterdir())
        max_entries = 500
        lines = []
        for entry in entries[:max_entries]:
            suffix = "/" if entry.is_dir() else ""
            try:
                rel = entry.relative_to(self._source_dir)
            except ValueError:
                rel = entry.relative_to(self._reports_dir)
            lines.append(f"{rel}{suffix}")

        if len(entries) > max_entries:
            lines.append(f"... ({len(entries) - max_entries} more entries)")

        return "\n".join(lines) if lines else "(empty directory)"

    async def _tool_search_code(self, params: dict[str, Any]) -> str:
        """Search for a pattern in workspace source files using grep."""
        pattern = params.get("pattern", "")
        if not pattern:
            raise ToolError("search_code requires 'pattern' parameter")

        glob_filter = params.get("glob", "")
        search_dir = str(self._source_dir)

        cmd = ["grep", "-rn", "--include=*", pattern, search_dir]
        if glob_filter:
            cmd = ["grep", "-rn", f"--include={glob_filter}", pattern, search_dir]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=search_dir,
            )
        except subprocess.TimeoutExpired:
            raise ToolError("search_code timed out after 30 seconds")

        output = result.stdout
        if not output:
            return f"No matches found for pattern: {pattern}"

        # Strip absolute paths, show relative to source/
        source_prefix = str(self._source_dir) + "/"
        output = output.replace(source_prefix, "")

        # Limit output
        lines = output.split("\n")
        max_lines = 200
        if len(lines) > max_lines:
            output = "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more matches)"

        return output

    async def _tool_run_command(self, params: dict[str, Any]) -> str:
        """Run a shell command in the workspace source directory."""
        command = params.get("command", "")
        if not command:
            raise ToolError("run_command requires 'command' parameter")

        timeout = min(params.get("timeout", 120), 300)

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self._source_dir),
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"Command timed out after {timeout} seconds: {command}")

        output_parts = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(f"STDERR:\n{result.stderr}")
        output_parts.append(f"Exit code: {result.returncode}")

        output = "\n".join(output_parts)

        # Limit output
        max_size = 50_000
        if len(output) > max_size:
            output = output[:max_size] + f"\n... (truncated, output was {len(output)} bytes)"

        return output

    async def _tool_git_operation(self, params: dict[str, Any]) -> str:
        """Run a git command in the workspace source directory.

        Only a subset of git subcommands are allowed.
        """
        subcommand = params.get("command", "")
        if not subcommand:
            raise ToolError("git_operation requires 'command' parameter (e.g. 'diff', 'log')")

        allowed_git_commands = {
            "status", "diff", "log", "show", "branch",
            "add", "commit", "checkout", "stash",
        }

        # Extract the git subcommand (first word)
        parts = subcommand.split()
        git_sub = parts[0] if parts else ""

        if git_sub not in allowed_git_commands:
            raise ToolError(
                f"Git subcommand '{git_sub}' is not allowed. "
                f"Allowed: {sorted(allowed_git_commands)}"
            )

        full_cmd = f"git {subcommand}"

        try:
            result = subprocess.run(
                full_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(self._source_dir),
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"Git command timed out: {full_cmd}")

        output_parts = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(f"STDERR:\n{result.stderr}")
        if result.returncode != 0:
            output_parts.append(f"Exit code: {result.returncode}")

        output = "\n".join(output_parts) if output_parts else "(no output)"

        max_size = 50_000
        if len(output) > max_size:
            output = output[:max_size] + "\n... (truncated)"

        return output

    # --- Config management tools (project-setup-agent) ---

    async def _tool_validate_jira(self, params: dict[str, Any]) -> str:
        result = await config_tools.validate_jira(
            url=params.get("url", ""),
            token=params.get("token", ""),
            email=params.get("email", ""),
            project_key=params.get("project_key", ""),
        )
        if result["success"]:
            return f"OK: Jira project '{result['project_name']}' is accessible."
        return f"FAILED: {result['error']}"

    async def _tool_validate_github(self, params: dict[str, Any]) -> str:
        result = await config_tools.validate_github(
            token=params.get("token", ""),
            owner=params.get("owner", ""),
            repo=params.get("repo", ""),
        )
        if result["success"]:
            return (
                f"OK: GitHub repo '{result['full_name']}' is accessible "
                f"(default branch: {result['default_branch']})."
            )
        return f"FAILED: {result['error']}"

    async def _tool_validate_gitlab(self, params: dict[str, Any]) -> str:
        result = await config_tools.validate_gitlab(
            token=params.get("token", ""),
            project_id=params.get("project_id", ""),
            url=params.get("url", "https://gitlab.com"),
        )
        if result["success"]:
            return f"OK: GitLab project '{result['project_name']}' is accessible."
        return f"FAILED: {result['error']}"

    async def _tool_validate_jenkins(self, params: dict[str, Any]) -> str:
        result = await config_tools.validate_jenkins(
            url=params.get("url", ""),
            username=params.get("username", ""),
            token=params.get("token", ""),
            job_key=params.get("job_key", ""),
        )
        if result["success"]:
            return f"OK: Jenkins job '{result['job_name']}' is accessible."
        return f"FAILED: {result['error']}"

    async def _tool_list_projects(self, params: dict[str, Any]) -> str:
        config_dir = params.get("config_dir", "")
        if not config_dir:
            raise ToolError("list_projects requires 'config_dir' parameter")
        projects = config_tools.list_projects(config_dir)
        if not projects:
            return "No projects found."
        lines = []
        for p in projects:
            status = "enabled" if p["enabled"] else "disabled"
            lines.append(f"- {p['id']}: {p['name']} ({p['repo_count']} repos, {status})")
        return "\n".join(lines)

    async def _tool_read_project_config(self, params: dict[str, Any]) -> str:
        config_dir = params.get("config_dir", "")
        project_id = params.get("project_id", "")
        if not config_dir or not project_id:
            raise ToolError("read_project_config requires 'config_dir' and 'project_id'")
        try:
            data = config_tools.read_project_config(config_dir, project_id)
        except FileNotFoundError as e:
            raise ToolError(str(e)) from e
        except ValueError as e:
            raise ToolError(str(e)) from e
        return yaml.safe_dump(data, default_flow_style=False)

    async def _tool_write_project_config(self, params: dict[str, Any]) -> str:
        config_dir = params.get("config_dir", "")
        project_id = params.get("project_id", "")
        yaml_content = params.get("yaml_content", "")
        if not config_dir or not project_id or not yaml_content:
            raise ToolError(
                "write_project_config requires 'config_dir', 'project_id', 'yaml_content'"
            )
        try:
            result = config_tools.write_project_config(config_dir, project_id, yaml_content)
        except ValueError as e:
            raise ToolError(str(e)) from e
        if result["success"]:
            return f"Successfully written to {result['path']}"
        return f"Failed: {result.get('error', 'unknown error')}"

    async def _tool_write_repo_config(self, params: dict[str, Any]) -> str:
        config_dir = params.get("config_dir", "")
        project_id = params.get("project_id", "")
        repo_id = params.get("repo_id", "")
        yaml_content = params.get("yaml_content", "")
        if not config_dir or not project_id or not repo_id or not yaml_content:
            raise ToolError(
                "write_repo_config requires 'config_dir', 'project_id', 'repo_id', 'yaml_content'"
            )
        try:
            result = config_tools.write_repo_config(
                config_dir, project_id, repo_id, yaml_content
            )
        except ValueError as e:
            raise ToolError(str(e)) from e
        if result["success"]:
            return f"Successfully written to {result['path']}"
        return f"Failed: {result.get('error', 'unknown error')}"

    async def _tool_remove_project(self, params: dict[str, Any]) -> str:
        config_dir = params.get("config_dir", "")
        project_id = params.get("project_id", "")
        if not config_dir or not project_id:
            raise ToolError("remove_project requires 'config_dir' and 'project_id'")
        try:
            result = config_tools.remove_project(config_dir, project_id)
        except ValueError as e:
            raise ToolError(str(e)) from e
        if result["success"]:
            return f"Removed project '{project_id}'. Backup at: {result['backup_path']}"
        return f"Failed: {result['error']}"


def get_tool_definitions(allowed_tools: list[str]) -> list[dict[str, Any]]:
    """Generate Claude API tool definitions for the given allowlist.

    Returns a list of tool definitions in the format expected by
    the Anthropic messages API.
    """
    all_definitions: dict[str, dict[str, Any]] = {
        "read_file": {
            "name": "read_file",
            "description": "Read the contents of a file. Path is relative to the repository root.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to repository root",
                    }
                },
                "required": ["path"],
            },
        },
        "write_file": {
            "name": "write_file",
            "description": "Write content to a file. Creates parent directories if needed. Path is relative to the repository root.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to repository root",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full content to write to the file",
                    },
                },
                "required": ["path", "content"],
            },
        },
        "list_directory": {
            "name": "list_directory",
            "description": "List the contents of a directory. Returns file and subdirectory names.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to repository root. Use '.' for root.",
                    }
                },
                "required": ["path"],
            },
        },
        "search_code": {
            "name": "search_code",
            "description": "Search for a text pattern across source files. Returns matching lines with file paths and line numbers.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Text or regex pattern to search for",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Optional file glob filter (e.g. '*.py', '*.kt')",
                    },
                },
                "required": ["pattern"],
            },
        },
        "run_command": {
            "name": "run_command",
            "description": "Run a shell command in the repository root directory. Use for running tests, linters, build tools.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (max 300, default 120)",
                    },
                },
                "required": ["command"],
            },
        },
        "git_operation": {
            "name": "git_operation",
            "description": "Run a git command. Allowed subcommands: status, diff, log, show, branch, add, commit, checkout, stash.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Git subcommand and arguments (without 'git' prefix). E.g. 'diff --stat', 'log --oneline -10'",
                    }
                },
                "required": ["command"],
            },
        },
        "validate_jira": {
            "name": "validate_jira",
            "description": "Validate Jira credentials and project key by hitting the Jira REST API.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Jira instance URL (e.g. https://company.atlassian.net)"},
                    "token": {"type": "string", "description": "Jira API token (resolved from env var)"},
                    "email": {"type": "string", "description": "Jira account email"},
                    "project_key": {"type": "string", "description": "Jira project key (e.g. PROJ)"},
                },
                "required": ["url", "token", "email", "project_key"],
            },
        },
        "validate_github": {
            "name": "validate_github",
            "description": "Validate GitHub token and repo access.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "token": {"type": "string", "description": "GitHub personal access token"},
                    "owner": {"type": "string", "description": "GitHub org or user"},
                    "repo": {"type": "string", "description": "Repository name"},
                },
                "required": ["token", "owner", "repo"],
            },
        },
        "validate_gitlab": {
            "name": "validate_gitlab",
            "description": "Validate GitLab token and project access.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "token": {"type": "string", "description": "GitLab personal access token"},
                    "project_id": {"type": "string", "description": "GitLab numeric project ID or namespaced path"},
                    "url": {"type": "string", "description": "GitLab instance URL (default: https://gitlab.com)"},
                },
                "required": ["token", "project_id"],
            },
        },
        "validate_jenkins": {
            "name": "validate_jenkins",
            "description": "Validate Jenkins credentials and job access.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Jenkins URL (e.g. https://jenkins.company.com)"},
                    "username": {"type": "string", "description": "Jenkins username"},
                    "token": {"type": "string", "description": "Jenkins API token"},
                    "job_key": {"type": "string", "description": "Jenkins job path (e.g. my-project/main)"},
                },
                "required": ["url", "username", "token", "job_key"],
            },
        },
        "list_projects": {
            "name": "list_projects",
            "description": "List all projects in a Sickle config directory.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "config_dir": {"type": "string", "description": "Path to config directory containing projects/"},
                },
                "required": ["config_dir"],
            },
        },
        "read_project_config": {
            "name": "read_project_config",
            "description": "Read a project's full configuration (project.yaml + all repo configs).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "config_dir": {"type": "string", "description": "Path to config directory"},
                    "project_id": {"type": "string", "description": "Project ID to read"},
                },
                "required": ["config_dir", "project_id"],
            },
        },
        "write_project_config": {
            "name": "write_project_config",
            "description": "Write project.yaml for a project. Creates directories if needed.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "config_dir": {"type": "string", "description": "Path to config directory"},
                    "project_id": {"type": "string", "description": "Project ID (directory name)"},
                    "yaml_content": {"type": "string", "description": "Full YAML content for project.yaml"},
                },
                "required": ["config_dir", "project_id", "yaml_content"],
            },
        },
        "write_repo_config": {
            "name": "write_repo_config",
            "description": "Write a repo config file for a project. Creates directories if needed.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "config_dir": {"type": "string", "description": "Path to config directory"},
                    "project_id": {"type": "string", "description": "Project ID"},
                    "repo_id": {"type": "string", "description": "Repo ID (file name without .yaml)"},
                    "yaml_content": {"type": "string", "description": "Full YAML content for the repo config"},
                },
                "required": ["config_dir", "project_id", "repo_id", "yaml_content"],
            },
        },
        "remove_project": {
            "name": "remove_project",
            "description": "Remove a project from config (backs up first to .backups/).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "config_dir": {"type": "string", "description": "Path to config directory"},
                    "project_id": {"type": "string", "description": "Project ID to remove"},
                },
                "required": ["config_dir", "project_id"],
            },
        },
    }

    return [all_definitions[name] for name in allowed_tools if name in all_definitions]
