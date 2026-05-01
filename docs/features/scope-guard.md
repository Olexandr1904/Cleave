# Feature: Scope Guard

**Status:** Implemented
**Created:** 2026-04-07
**Updated:** 2026-04-07
**Author:** Oleksandr Brazhenko

## Description

Agent that validates the developer's diff against the implementation plan and architecture rules. Ensures no out-of-scope changes slip through: every changed file must be in the plan's allowed list and every change must map to a ticket requirement. Issues a scope certificate on pass or a scope violation report on failure.

## Requirements

- FR1: Scope Guard Agent prompt file at `agents/scope-guard-agent.md`
- FR2: Receives: `git diff origin/{default_branch}...HEAD`, `implementation-plan.md`, `ticket.json`, `arch-rules.md`
- FR3: Checks every changed file: is it in the plan's allowed list? Does each change map to a ticket requirement?
- FR4: Detects violations: unauthorized files, formatting-only changes, new unused imports, layer boundary violations, missing ticket ID in commits
- FR5: If violations → writes `context/scope-report.md` with violation list and fix instructions, state returns to Dev Agent
- FR6: If clean → writes `context/scope-certificate.md`, state advances to PR creation
- FR7: Iteration count incremented on each pass; max from config triggers escalation

## Technical Approach

- Scope Guard is a BMAD-style prompt file with diff analysis instructions
- Agent receives the full diff and the implementation plan, performs line-by-line analysis
- File allowlist derived from implementation plan's "files to create/modify" section
- Architecture rules checked: no modifications to protected files (arch-rules.md, lint config, CI config)
- Scope certificate is a simple markdown file confirming all checks passed
- Scope report lists each violation with file, line, type, and suggested fix

## Dependencies

- Agent System for prompt loading and execution
- Workspace Isolation for accessing diff and context files
- Configuration Cascade for iteration caps and protected file paths
- Telegram Notifications for escalation on max iterations

## Acceptance Criteria

- [ ] Validates all changed files against implementation plan's allowed list
- [ ] Detects all violation types (unauthorized files, formatting-only, unused imports, etc.)
- [ ] Writes scope report with actionable fix instructions on failure
- [ ] Writes scope certificate on success
- [ ] Iteration count tracked and max triggers escalation
- [ ] State correctly returns to Dev Agent on violations

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
