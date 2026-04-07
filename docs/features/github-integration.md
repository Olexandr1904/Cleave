# Feature: GitHub Integration

**Status:** Planned
**Created:** 2026-04-07
**Updated:** 2026-04-07
**Author:** Oleksandr Brazhenko

## Description

GitHub adapter behind the VCSInterface. Handles all git and GitHub operations: branch creation, pushing, PR management, review comment reading/replying, CI status checking, and merging. Uses git CLI for local operations and GitHub REST API for remote operations.

## Requirements

- FR1: Create and checkout branch `{branch_prefix}/{ticket_id}-{slug}` in workspace repo
- FR2: Push current branch to origin
- FR3: Open PR using `pr_description_template` from repo config, return PR number and URL
- FR4: Get all review comments on a PR
- FR5: Reply to specific review comments
- FR6: Check whether all CI checks are passing
- FR7: Merge PR using configured `merge_method` (squash/merge/rebase)
- FR8: Close PR on failure/escalation
- FR9: All operations use token from repo config
- FR10: Implements abstract `VCSInterface`

## Technical Approach

- `GitHubAdapter` class implementing `VCSInterface`
- Local git operations via subprocess (git CLI) — more reliable than Python git libraries for concurrent ops
- Remote GitHub operations via httpx async client against GitHub REST API
- Branch naming: `{branch_prefix}/{ticket_id}-{slug}` where slug is derived from ticket summary
- PR description generated from configurable template with variable substitution

## Dependencies

- Git CLI (subprocess) for local operations
- httpx for GitHub REST API
- Configuration Cascade for repo settings (token, owner, repo, default_branch, branch_prefix, merge_method)
- Abstract VCSInterface from `integrations/base/`

## Acceptance Criteria

- [ ] Creates and checks out feature branches with correct naming
- [ ] Pushes branches to origin
- [ ] Opens PRs with templated descriptions
- [ ] Reads and replies to review comments
- [ ] Checks CI status
- [ ] Merges with configured method
- [ ] Closes PRs on failure

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
