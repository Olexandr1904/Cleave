# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## 5. Pipeline Agent Permissions

Cleave dispatches Claude Code agents via `claude -p` as automated subprocesses.
These agents need tool access to read/write code, run git, and execute build commands.

**Permission setup:** `.claude/settings.json` pre-approves tools for the pipeline:

- **File access:** Read, Write, Edit, Glob, Grep, LS
- **Git:** all git subcommands (add, commit, checkout, push, etc.)
- **Build tools:** gradlew, npm, yarn, python, pytest
- **Shell basics:** ls, find, cat, grep, mkdir, cp, mv

**Denied:** `rm -rf /`, `sudo`, `curl`, `wget` (agents must not reach external
services or escalate privileges — API calls go through Cleave's own adapters).

**How agents get their tools:**
1. Each agent's `.md` frontmatter lists allowed tools (e.g., `tools: [read_file, write_file, git_operation]`)
2. `ClaudeCodeAdapter` maps these to Claude Code tool names via `TOOL_MAP` (in `integrations/llm/claude_code_adapter.py`)
3. The CLI is invoked with `--allowedTools Read,Write,Edit,Bash,Glob,Grep,LS`
4. `.claude/settings.json` pre-approves these so `claude -p` runs non-interactively

**Adding new agents:** List only the tools the agent needs in its frontmatter.
Don't give write access to read-only agents (BA, scope-guard, PR-comment-responder).

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
