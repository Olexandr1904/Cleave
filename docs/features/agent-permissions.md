# Agent Permissions

How Cleave's pipeline agents get tool access in Claude Code.

## Overview

Cleave runs Claude Code agents as non-interactive subprocesses (`claude -p`).
Each agent needs specific tools (read files, write code, run git) without
human approval prompts — the pipeline is fully automated.

## Permission Layers

### Layer 1: Project settings (`.claude/settings.json`)

Pre-approves tools at the project level. Claude Code reads this file and
skips permission prompts for matching patterns.

```json
{
  "permissions": {
    "allow": [
      "Read", "Write", "Edit",
      "Bash(git *)", "Bash(ls *)", "Bash(./gradlew *)", ...
    ],
    "deny": [
      "Bash(rm -rf /)", "Bash(sudo *)", "Bash(curl *)", "Bash(wget *)"
    ]
  }
}
```

**Allow:** File operations, git, build tools, standard shell commands.
**Deny:** Destructive operations, privilege escalation, external network
calls (API access goes through Cleave's adapters, not the agent).

### Layer 2: Per-agent tool list (agent `.md` frontmatter)

Each agent declares which tools it needs:

```yaml
# dev-agent.md
tools:
  - read_file      # Read, LS, Glob
  - write_file     # Write, Edit
  - list_directory  # LS, Glob
  - search_code    # Grep
  - run_command    # Bash
  - git_operation  # Bash (git commands)
```

Read-only agents (BA, scope-guard) only get `read_file`, `list_directory`,
`search_code`. They cannot modify code.

### Layer 3: CLI flag (`--allowedTools`)

`AgentRuntime` maps the agent's tool list to Claude Code tool names via
`TOOL_MAP` in `integrations/llm/claude_code_adapter.py`:

```python
TOOL_MAP = {
    "read_file": "Read",
    "write_file": "Write,Edit",
    "list_directory": "LS,Glob",
    "search_code": "Grep",
    "run_command": "Bash",
    "git_operation": "Bash",
}
```

The CLI is invoked as:
```bash
claude -p --allowedTools Read,Write,Edit,Bash,Glob,Grep,LS --cwd /path/to/workspace/source --max-turns 50
```

### Layer 4: Workspace sandbox (`--cwd`)

Each agent runs with its working directory set to the workspace's `source/`
directory. File operations are scoped to this directory by Claude Code's
built-in path restrictions.

## Agent Tool Matrix

| Agent | read_file | write_file | run_command | git_operation | Special |
|---|---|---|---|---|---|
| ba-agent | Yes | - | - | - | Read-only analysis |
| dev-agent | Yes | Yes | Yes | Yes | Full code access |
| fix-agent | Yes | Yes | Yes | Yes | Same as dev |
| qa-agent | Yes | Yes | Yes | Yes | Runs tests |
| scope-guard-agent | Yes | - | - | Yes | Git diff only |
| pr-comment-responder | Yes | - | - | - | Read-only |
| project-setup-agent | - | - | - | - | Custom sandbox tools |

## Adding a New Agent

1. Create `agents/my-agent.md` with frontmatter listing needed tools
2. Only add tools the agent actually needs (principle of least privilege)
3. If the agent needs a new tool not in `TOOL_MAP`, add the mapping
4. Test with `claude -p --allowedTools <tools> --cwd <workspace>` manually first

## Troubleshooting

**Agent runs but doesn't use tools (tool_calls=0):**
- Check the agent's frontmatter `tools:` list is correct
- Don't set `model:` in frontmatter — leave empty to use CLI adapter
- Setting a model name routes to the API adapter which has a different tool loop

**Agent can't write/commit:**
- Verify `.claude/settings.json` allows the `Bash(git *)` pattern
- Check workspace `source/` directory exists and is a git repo
- Check git identity is configured (`git config user.email`)

**Permission denied in Claude Code:**
- Add the pattern to `.claude/settings.json` `allow` list
- Use wildcards: `Bash(git *)` covers all git subcommands

## Changelog

| Date | Change |
|---|---|
| 2026-04-21 | Initial documentation. Project settings + per-agent tools + CLI flags. |
| 2026-05-14 | Added `add_dirs` parameter to `execute_in_workspace` / `_run_cli`; maps to `--add-dir` so agents can read directories outside `cwd` (e.g. `meta/`). Documented `add_dirs` in the `execute_in_workspace` docstring `Args:` block. |
