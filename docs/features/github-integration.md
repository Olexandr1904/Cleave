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
| 2026-04-27 | `_request` now captures the GitHub response body when an HTTP 4xx/5xx fails after retries. httpx's default `HTTPStatusError` message includes only the status line ("Client error '422 Unprocessable Entity' for url '...'") which left operators no clue why GitHub rejected a PR creation. The diagnostic wrap reraises as `RuntimeError` with up to 500 chars of the response body so the reason (e.g. "A pull request already exists for ...", "No commits between base and head") surfaces in the workspace `error` field and the TG failure notification. |
| 2026-04-28 | Per-repo `vcs.skip_pre_push_hook` config flag (default `false`). When `true`, `git push` runs with `--no-verify`, bypassing project-installed pre-push hooks. Designed for repos whose hooks duplicate work the QA stage already does (e.g. `./gradlew detektDebug` reinstalled by the project's `installGitHook` Gradle task) or fail for environmental reasons unrelated to the code (e.g. x86-64 aapt2 on an ARM64 host). Wired through both `pr_creation.create_pr` (initial push) and the orchestrator's force-push site for additional commits to existing PRs. |
| 2026-04-28 | Graceful push: `create_pr` now catches push failures whose error matches a known environmental signature (Gradle/AAPT2 toolchain — see `gradle_remediation.looks_like_pre_push_hook_environmental_failure`) and automatically retries with `--no-verify`. A `push_hook_bypassed` event is emitted with the original error excerpt so the operator sees the bypass happened. Real detekt/lint code-quality findings still surface as failures — only environmental hook errors are bypassed silently. The explicit `skip_pre_push_hook` config flag still works (skips on first try, no environmental check needed). |
| 2026-04-30 | `GitHubAdapter.__init__` now stores the auth token on `self._token`. `_graphql_request` reads the token from there but the assignment was missing, so `resolve_comment` (and any future GraphQL call) raised `AttributeError` on every invocation in production. The httpx REST client kept its own header copy and was unaffected, which is why other API calls worked. Added `test_init_stores_token_for_graphql` regression test. |
