# Feature: Merge Agent

**Status:** Planned
**Created:** 2026-04-07
**Updated:** 2026-04-07
**Author:** Oleksandr Brazhenko

## Description

Final agent in the pipeline that performs gate checks and merges the PR. Verifies scope certificate, resolved review comments, passing tests/lint/build, and no merge conflicts before merging. Handles merge conflicts intelligently: auto-resolves conflicts in non-plan files, escalates conflicts in plan files.

## Requirements

- FR1: Merge Agent prompt file at `agents/merge-agent.md`
- FR2: Gate checklist: scope certificate exists, all review comments resolved, tests passing, lint passing, build passing, no merge conflicts
- FR3: Merge conflict in files NOT in implementation plan → resolve by taking base branch version
- FR4: Merge conflict in files IN implementation plan → immediate Telegram escalation, no auto-resolution
- FR5: On success: merge PR using configured `merge_method`, transition Jira to Done, post Jira comment, send Telegram success notification
- FR6: On failure at any gate: log which gate failed, notify via Telegram with specifics
- FR7: Workspace status set to `completed` on merge, `failed` on unresolvable failure

## Technical Approach

- Merge Agent is a BMAD-style prompt file with a gate checklist
- Agent verifies each gate sequentially — first failure stops the process
- Merge conflict detection via `git merge --no-commit` dry run against default branch
- Conflict categorization by comparing conflicted files against implementation plan's file list
- Uses GitHub adapter for actual merge operation
- Uses Jira adapter for ticket transition and comment
- Uses Telegram adapter for success/failure notifications

## Dependencies

- Agent System for prompt loading and execution
- GitHub Integration for PR merge operation
- Jira Integration for ticket transition
- Telegram Notifications for success/failure notifications
- Scope Guard (scope certificate as gate input)
- QA Pipeline (test/lint/build results as gate input)

## Acceptance Criteria

- [ ] All gate checks verified before merge
- [ ] Non-plan merge conflicts auto-resolved
- [ ] Plan-file merge conflicts trigger Telegram escalation
- [ ] Successful merge transitions Jira to Done and sends notification
- [ ] Gate failure logged and reported via Telegram
- [ ] Workspace status updated correctly on success/failure

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
