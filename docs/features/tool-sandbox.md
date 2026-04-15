# Tool Sandbox

Sandboxed tool execution layer for agents. Each agent receives a restricted set of tools (read_file, write_file, list_directory, search_code, run_command, git_operation) based on its allowlist. All file operations are confined to the workspace's `source/` and `reports/` directories. Protected files (from repo config) cannot be written. Every tool call is logged.

## Tools
- `validate_git_identity` — checks that git user.name and user.email are set for a workspace (wraps `health.validators.check_git_identity`)

## Key Decisions
- Path traversal blocked — agents cannot escape workspace boundaries
- Per-agent allowlist enforced at runtime
- Tool call logging for audit trail

## References
- Architecture: `docs/architecture-v2.md` §9 (Agent Execution Model)
- Contracts: `docs/agent-contracts.md` (Tool Allowlist Matrix)
- Implementation: `orchestrator/tool_sandbox.py`
