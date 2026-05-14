# Feature: GitLab Integration

**Status:** Implemented
**Created:** 2026-04-07
**Updated:** 2026-05-12
**Author:** Oleksandr Brazhenko

## Description

VCS adapter for GitLab. Implements the same `VCSInterface` as the GitHub
adapter, so a project with `vcs.provider: gitlab` runs the full pipeline
end-to-end: clone → branch → push → MR → review-comment loop → CI gate
→ DONE.

## Architecture

- `GitLabAdapter` lives in `integrations/gitlab/gitlab_adapter.py` and
  follows the same shape as `GitHubAdapter`: direct GitLab REST API v4
  via `httpx`, with retries, body-on-failure error surfacing, and an
  async git CLI helper for clone/branch/push.
- Authentication: `Private-Token` header for REST; clone/push uses the
  `https://oauth2:<token>@<host>/<namespace>/<project>.git` URL form so
  workspaces remain authenticated after a token rotation (origin URL is
  rewritten on each push).
- MR ↔ PR mapping: GitLab's MR `iid` is the public identifier and is
  returned by `open_pr`. Discussions with diff-position notes are
  surfaced as `PRComment` objects; general MR notes are skipped. A
  private `_discussion_cache[pr_number]` map records each note's owning
  `discussion_id` so `reply_to_comment` and `resolve_comment` can post
  to the right thread without changing the `VCSInterface` contract.
- CI gate: `check_pr_status` calls `GET /merge_requests/:iid/pipelines`
  and returns `all_passing = (latest.status == "success")`. Latest is
  selected by pipeline `id` (monotonic per project) rather than
  `created_at`, so rapid retries that share a wall-clock second still
  pick the last-created run. No separate `ci.provider: gitlab_ci` is
  added — the VCS adapter owns pipeline status.

## Configuration

`vcs.provider: gitlab` plus a `vcs.gitlab` block — see `GitLabConfig`
in `config/schemas.py`. The dashboard's `+ New Project` wizard renders
the GitLab fields, validates them against the live API via
`validate_gitlab`, and writes the YAML for you.

## References

- Spec: `docs/superpowers/specs/2026-05-12-gitlab-vcs-adapter-design.md`
- Adapter: `integrations/gitlab/gitlab_adapter.py`
- Schema: `config/schemas.py` (`GitLabConfig`, `VCSConfig`)
- Live validator: `integrations/config/config_tools.py` (`validate_gitlab`)

## Out of scope

- GitLab CI as a distinct `ci.provider` (pipeline status is read by the
  VCS adapter).
- MR approval rules, auto-merge, merge-when-pipeline-succeeds.
- Live-network integration tests in CI; manual smoke against a real
  instance is the verification path.
