# GitLab VCS Adapter

**Status:** Design
**Created:** 2026-05-12
**Author:** Oleksandr Brazhenko

## Summary

Implement `GitLabAdapter(VCSInterface)` so the pipeline runs end-to-end on GitLab repositories, with provider selected by the user in the project-create wizard. All scaffolding around GitLab is already in place (schema, wizard UI, payload validation, live-check endpoint, health validator, atlas agent tool); the only material gap is the adapter itself plus a two-branch wiring edit in `main.py`.

## Motivation

The project-create wizard already lets a user pick `vcs.provider: gitlab` and validates GitLab credentials against the live API. But once a project is added, the daemon has no `GitLabAdapter` to instantiate, so any GitLab-routed repo silently has no VCS adapter and the pipeline can't push, open MRs, fetch review comments, or check CI for it. Closing this gap is a self-contained piece of work: one new adapter file, one helper-extraction in `main.py`, one cosmetic wizard fix, a docs rewrite, and three unit-test files.

## Non-goals

- New tracker (e.g., Trello) — out of scope; Jira remains the only tracker.
- GitLab CI as a distinct `ci.provider` — the VCS adapter handles pipeline status via the MR's pipelines endpoint; no separate CI provider is added.
- GitLab merge-request approval rules, auto-merge, or merge-when-pipeline-succeeds.
- Live-network integration tests in CI (neither gitlab.com nor self-hosted). Self-hosted URLs are already supported in the schema and exercised by the same code paths; verifying against a real instance is left to manual smoke tests.
- Removing dead `HelpersConfig` GitLab slots (`fetch_mr_comments`, `resolve_mr_comments`, `post_review_comments`) — left in place; cleanup is a separate task.

## Architecture overview

| Layer | Change |
|---|---|
| `integrations/gitlab/gitlab_adapter.py` | **New** — `GitLabAdapter(VCSInterface)`, ~400 lines, mirrors `GitHubAdapter` shape |
| `integrations/gitlab/__init__.py` | **New** — empty package marker |
| `main.py` | **Edit** — add `_build_vcs_adapter` helper; call it from initial-load loop and `_build_repo_adapters` (hot-reload). Replaces the duplicated GitHub-only branch in both call sites. |
| `dashboard/project_create_payload.py:117` | **Tiny edit** — `jira_repo_label:` → `tracker_label:` in the redacted `input.md` text |
| `docs/features/gitlab-integration.md` | **Rewrite** — describe direct-API design; mark "Implemented" |
| `tests/unit/test_gitlab_adapter_request.py` | **New** — mirror `test_github_adapter_request.py` |
| `tests/unit/test_gitlab_adapter_mr.py` | **New** — MR + discussion + pipelines operations against mocked GitLab API |
| `tests/unit/test_gitlab_adapter_git.py` | **New** — clone/branch/push subprocess behavior |
| `tests/unit/test_project_create_payload.py` | **Edit** — add regression assertion for `tracker_label:` text |

Nothing else moves. The orchestrator, pipeline drivers, action modules, ingest, escalation, notify, ticket_sync, approval_gate, and runtime are all already provider-agnostic — they take a `VCSInterface` and a `RepoConfig`, dispatched per-repo via `Orchestrator.register_repo_vcs(repo_id, vcs, repo_config)`.

**No interface change.** `VCSInterface` and `PRComment` stay as-is. `PRComment.id` remains an `int` (GitLab note IDs are integers). The `note_id → discussion_id` mapping is a private cache inside `GitLabAdapter`.

**No schema change.** `GitLabConfig` (token, project_id, url, default_branch, branch_prefix), `VCSConfig.provider`, `skip_pre_push_hook`, and `tracker_label` already exist in `config/schemas.py`.

**No wizard change.** The wizard already renders the GitLab provider tab, validates the payload shape, and live-checks credentials via `check_gitlab` ([dashboard/web.py:415-422](../../dashboard/web.py#L415)) / `validate_gitlab` ([integrations/config/config_tools.py:302](../../integrations/config/config_tools.py#L302)).

## GitLab adapter contract

The adapter implements all 10 `VCSInterface` methods. Authentication uses the `Private-Token` header on REST calls (same header `validate_gitlab` uses). Base URL is `{gitlab.url}/api/v4`. Project ID is URL-encoded so it accepts both numeric IDs and namespaced `group/project` paths.

| Method | GitLab REST mapping | Notes |
|---|---|---|
| `clone_repo(url, dest, depth)` | `git clone` subprocess | Same as GitHub. Clone URL injected with `oauth2:<token>@host` for auth. |
| `create_branch(repo_dir, branch_name)` | `git checkout -b` subprocess | Identical to GitHub. |
| `push(repo_dir, branch, force, skip_hooks)` | `git remote set-url origin <oauth2-url>` then `git push -u origin <branch>` | Rewrites origin URL with the current token (same pattern as [github_adapter.py:142-145](../../integrations/github/github_adapter.py#L142-L145)) so workspaces cloned with an older token still authenticate. `--force` on `force=True`; `--no-verify` on `skip_hooks=True`. |
| `open_pr(title, body, head, base)` | `POST /projects/:id/merge_requests` with `source_branch`, `target_branch`, `title`, `description` | Returns `(iid, web_url)`. Uses `iid` (project-scoped) not `id` (global) because URLs and downstream API paths take `iid`. |
| `find_pr_by_branch(branch)` | `GET /projects/:id/merge_requests?source_branch=<branch>&state=opened` | Returns first match's `(iid, web_url)` or `None`. |
| `get_pr_comments(mr_iid)` | `GET /projects/:id/merge_requests/:iid/discussions` (paginated, 100/page) | Walks `discussions[].notes[]` and emits only diff-position notes (notes with `position` set) — the GitLab equivalent of GitHub's review comments. General MR notes are skipped. **Side effect:** populates the private `_discussion_cache[mr_iid] = {note_id: discussion_id}` map. |
| `reply_to_comment(mr_iid, note_id, body)` | `POST /projects/:id/merge_requests/:iid/discussions/:discussion_id/notes` with `body` | Looks up `discussion_id` from `_discussion_cache`; on miss, refetches discussions for that MR once. Raises if still not found. |
| `resolve_comment(mr_iid, note_id)` | `PUT /projects/:id/merge_requests/:iid/discussions/:discussion_id?resolved=true` | Same cache lookup. Idempotent — GitLab returns 200 even if already resolved. |
| `check_pr_status(mr_iid)` | `GET /projects/:id/merge_requests/:iid/pipelines` → take latest by `created_at` | `all_passing = (latest.status == "success")`. No pipeline → `all_passing=False, checks=[]`. Each pipeline serialized as `{name, status, conclusion}` for parity with the GitHub shape. |
| `close_pr(mr_iid)` | `PUT /projects/:id/merge_requests/:iid` with `state_event: "close"` | Same semantics as GitHub close. |

**Shared HTTP plumbing (private):**

- `_request(method, path, **kwargs)` — three retries with 1/2/4s backoff, no-retry on 401/403, surfaces response body in the error message on final failure (same shape as [github_adapter.py:43-81](../../integrations/github/github_adapter.py#L43-L81)).
- `_run_git(repo_dir, *args)` — async subprocess with 300s timeout, identical to GitHub's helper.
- `_discussion_cache: dict[int, dict[int, str]]` — per-MR `note_id → discussion_id` map. Populated lazily; never invalidated (discussion IDs are stable for the life of a note; daemon restart clears the cache, which is harmless).

**Constructor:**

```python
GitLabAdapter(token: str, project_id: str, url: str = "https://gitlab.com")
```

Reads the three fields from `GitLabConfig`. `default_branch` and `branch_prefix` are read by callers from `repo_config.vcs` (provider-agnostic post-refactor), not by the adapter.

## `main.py` wiring

Two call sites change; both share a new `_build_vcs_adapter` helper to remove the existing GitHub/GitLab dispatch duplication.

**Helper (new):**

```python
def _build_vcs_adapter(repo_cfg: RepoConfig) -> VCSInterface | None:
    provider = repo_cfg.vcs.provider
    if provider == "github" and repo_cfg.vcs.github.token:
        from integrations.github.github_adapter import GitHubAdapter
        return GitHubAdapter(
            token=repo_cfg.vcs.github.token,
            owner=repo_cfg.vcs.github.owner,
            repo=repo_cfg.vcs.github.repo,
        )
    if provider == "gitlab" and repo_cfg.vcs.gitlab.token:
        from integrations.gitlab.gitlab_adapter import GitLabAdapter
        return GitLabAdapter(
            token=repo_cfg.vcs.gitlab.token,
            project_id=repo_cfg.vcs.gitlab.project_id,
            url=repo_cfg.vcs.gitlab.url or "https://gitlab.com",
        )
    return None
```

**Site 1: Initial-load loop** ([main.py:259-272](../../main.py#L259)) replaces the GitHub-specific block with a call to `_build_vcs_adapter`, accumulating into a provider-neutral `vcs_adapters: dict[str, tuple[VCSInterface, RepoConfig]]`.

**Site 2: Hot-reload `_build_repo_adapters`** ([main.py:295-303](../../main.py#L295)) is reduced to the same one-liner per repo, then `orchestrator.register_repo_vcs(repo_id, adapter, repo_cfg)`. The log line names the provider generically (`"Hot-reload: registered %s adapter for %s"`).

**Tracker wiring is untouched.** Jira remains the only tracker; `on_project_added`'s Jira-attach block is unchanged.

## Wizard cleanup

[dashboard/project_create_payload.py:117](../../dashboard/project_create_payload.py#L117) emits a human-readable hint in the redacted `input.md` the atlas agent reads. Currently it says `jira_repo_label:` even though the schema field is `tracker_label`. One-line change: replace both occurrences of `jira_repo_label` with `tracker_label` in that string.

This is the only wizard-side change. The wizard JS, payload validator, and live-check endpoint all already handle the GitLab branch.

## Docs

[docs/features/gitlab-integration.md](../../docs/features/gitlab-integration.md) currently describes the legacy helper-script wrapping plan, which predates the current architecture and is inconsistent with how GitHubAdapter ships. Rewrite to ~30 lines covering:

- **Status:** Implemented.
- **Architecture:** `GitLabAdapter(VCSInterface)` in `integrations/gitlab/gitlab_adapter.py`, direct GitLab REST v4 over httpx.
- **Auth:** `Private-Token` header for API; `oauth2:<token>@host` URL form for `git clone`/`push`.
- **MR ↔ PR mapping:** `iid` is the public identifier; discussions with diff-position notes are surfaced as review comments; general MR notes are skipped.
- **Pipeline status:** the latest MR pipeline determines the CI gate.
- **Configuration:** point to `GitLabConfig` in `config/schemas.py` and the wizard.
- **Out-of-scope:** separate `ci.provider: gitlab_ci`; approval rules; merge automation.

## Testing

Four files in `tests/unit/`. No live network in CI.

1. **`test_gitlab_adapter_request.py`** — covers `_request`: retry-on-timeout, no-retry-on-401/403, exponential backoff, response-body surfacing on final failure. Pattern-for-pattern copy of [tests/unit/test_github_adapter_request.py](../../tests/unit/test_github_adapter_request.py).

2. **`test_gitlab_adapter_mr.py`** — covers MR operations against an `respx`-mocked GitLab API:
   - `open_pr` posts the right payload and returns `(iid, web_url)`.
   - `find_pr_by_branch` filters by `source_branch` and `state=opened`.
   - `get_pr_comments` paginates discussions, skips general notes, surfaces only diff-position notes, populates `_discussion_cache`.
   - `reply_to_comment` reads from cache; refetches on cache miss; raises on hard miss.
   - `resolve_comment` uses the discussion-resolve endpoint with cache.
   - `check_pr_status` returns the latest pipeline; `all_passing=False` when no pipelines exist.
   - `close_pr` sends `state_event: "close"`.

3. **`test_gitlab_adapter_git.py`** — covers `clone_repo`, `create_branch`, `push` (subprocess mocking). Mirrors [tests/unit/test_github_adapter_push.py](../../tests/unit/test_github_adapter_push.py): asserts `remote set-url` runs before `push`, `--no-verify` appended on `skip_hooks=True`, `--force` on `force=True`.

4. **Regression in `tests/unit/test_project_create_payload.py`** — assert `redact_to_input_md` emits `tracker_label:` not `jira_repo_label:`.

No integration test added against gitlab.com. No e2e changes — the e2e suite runs against fixtures, not real VCS.

## Acceptance criteria

- A project created via the dashboard wizard with `vcs.provider: gitlab` runs the full pipeline end-to-end against gitlab.com: BA → Dev → push → MR → PR-Comment-Responder loop → CI gate → DONE.
- `GitLabAdapter` implements all 10 `VCSInterface` methods with the REST mappings above.
- `main.py` initial-load and hot-reload paths both instantiate `GitLabAdapter` for `provider == "gitlab"` repos and register them via `register_repo_vcs`.
- Existing GitHub-routed projects continue to work unchanged.
- `redact_to_input_md` emits `tracker_label:` not `jira_repo_label:`.
- All new unit tests pass; no live network in CI.
- `docs/features/gitlab-integration.md` describes the direct-API architecture and is marked Implemented.

## Open questions

None at this point.

## Change log

| Date | Description |
|---|---|
| 2026-05-12 | Initial design — direct-API GitLabAdapter mirroring GitHubAdapter; per-repo dispatch already in place post-refactor. |
