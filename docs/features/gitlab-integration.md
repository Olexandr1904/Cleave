# GitLab Integration

VCS adapter for GitLab. Implements the same `VCSInterface` as the GitHub adapter so a project configured with `vcs.provider: gitlab` runs the full pipeline end-to-end. Single-file adapter using direct httpx REST calls against GitLab API v4 plus async git CLI subprocesses (mirrors the GitHub adapter shape).

## Status

- **Task 1 (in progress):** package scaffolded — `GitLabAdapter` constructor with URL normalization, project-id URL-encoding, and shared `httpx.AsyncClient`; all `VCSInterface` methods are `NotImplementedError` stubs filled in by later tasks.
- **Task 2 (in progress):** `_request` wired — retries with backoff, 401/403 no-retry, response body surfaced on final failure.
- **Task 3 (in progress):** git CLI wrappers wired — `_run_git` staticmethod plus `clone_repo` and `create_branch` via `asyncio.create_subprocess_exec` with `SUBPROCESS_TIMEOUT`.
- **Task 4 (in progress):** `push` wired — rewrites `origin` to GitLab `oauth2:<token>` form before pushing; supports `force` and `skip_hooks`.

## Key Decisions
- Configured via `vcs.provider: gitlab` in repo config
- Direct httpx REST (no shelling out to `glab` / helper scripts) — same shape as GitHub adapter
- `project_id` accepts either numeric id or `group/sub/project` path; URL-encoded once at init for use in `/api/v4/projects/{id}/...` routes

## References
- Spec: `docs/superpowers/specs/2026-05-12-gitlab-vcs-adapter-design.md`
- Plan: `docs/superpowers/plans/2026-05-12-gitlab-vcs-adapter.md`
- Architecture: `docs/architecture-v2.md` §8.2 (VCS Abstraction)
- Implementation: `integrations/gitlab/gitlab_adapter.py`
