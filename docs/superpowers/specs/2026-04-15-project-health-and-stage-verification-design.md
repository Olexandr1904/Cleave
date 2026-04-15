# Design: Project Health Checks + Stage Verification

**Status:** Design
**Created:** 2026-04-15
**Author:** Oleksandr Brazhenko

## Problem

ACME-14595 went through Analysis → DEV → QA → AWAITING_APPROVAL silently, despite the dev-agent failing to commit (git `user.name`/`user.email` not configured). The agent correctly refused to touch git config, surfaced the error in a Telegram message, then QA ran on uncommitted work and the pipeline asked for approval of a change that was never committed anywhere.

Two gaps caused this:

1. **No mechanical verification** that each pipeline stage actually produced its expected side-effect. The orchestrator only parses agent text output for pass/fail keywords, so "Tests pass" from the dev-agent was read as success even though no commit existed.
2. **No operator-facing visibility** into per-project health (Jira auth, git identity, git remote auth). The failure mode was discoverable only by reading Telegram logs after the fact.

## Goals

- **The can't-commit error (and its cousins) must become an edge case** — if it still happens, it must land in `BLOCKED` with a clear reason, never in `AWAITING_APPROVAL`.
- **Operator can see per-project health at a glance** from the dashboard, with a fix hint for each failing check.
- **Setup is learnable** — the setup manual and the project-setup-agent both teach/verify the prerequisites.
- No project-wide gating. A broken push credential must not block BA/PM/DEV stages that only need Jira.

## Non-Goals

- Automatic remediation (the daemon never runs `git config` or writes credentials for the operator).
- Continuous background polling beyond the existing 60s dashboard refresh cycle.
- E2E test coverage for the project-setup-agent flow (out of scope; it has none today).
- Replacing the existing text-based agent-outcome parsing; mechanical verification runs alongside it.

## Architecture Overview

Three independent consumers sharing one validator library:

```
health/                         (new shared module)
├── validators.py               jira, github, gitlab, git_identity, git_remote
└── runner.py                   project-level aggregation + 60s in-process cache

orchestrator/stage_verifier.py  (new) — post-stage mechanical assertions
                                called by the orchestrator after each agent stage
                                on failure → workspace.transition("BLOCKED", error=...)

dashboard/web.py                (extended) — GET /api/projects/health
dashboard/static/js/board.js    (extended) — per-project health strip + expand

agents/project-setup-agent.md   (extended) — new validate_git_identity tool
integrations/llm/tool_sandbox.py (extended) — register validate_git_identity

docs/setup-guide.md             (extended) — Prerequisites section
```

The validators are the only shared code. Every other consumer decides independently what to do with a failing result — the orchestrator blocks a stage, the dashboard shows a red row, the setup agent refuses to declare setup complete.

## Component: Stage Verifier

**File:** `orchestrator/stage_verifier.py` (new).

**Data:**

```python
@dataclass
class VerifyResult:
    ok: bool
    stage_id: str
    reason: str           # human-readable, shown in BLOCKED state's error field
```

**Assertions per stage:**

| Stage         | Mechanical assertion                                                                            |
|---------------|--------------------------------------------------------------------------------------------------|
| `dev`         | `git rev-parse HEAD` on the feature branch differs from the commit captured at stage start.     |
| `scope_check` | `reports/scope-guard-agent-output.md` exists (already checked; promoted to stage verifier).     |
| `qa`          | `reports/qa-agent-output.md` exists (already checked; promoted).                                |
| `push`        | `git ls-remote origin <branch>` returns a ref equal to local `HEAD`.                            |
| `pr_review`   | `vcs.find_pr(branch)` returns a PR object (existing vcs adapter, already reliable).             |

Stages without a natural mechanical check (`analysis`, `project_setup`) keep the current text-parsing path unchanged.

**Orchestrator integration:**

Before launching the stage agent, the orchestrator records `stage_start_commit = git rev-parse HEAD` (or `None` if not applicable). After the agent finishes, it calls:

```python
result = stage_verifier.verify(stage_id, workspace, stage_start_commit)
if not result.ok:
    workspace.transition("BLOCKED", error=f"{stage_id}: {result.reason}")
    event_bus.emit("stage_verification_failed", ...)
    continue  # skip outcome parsing / next-state routing
```

`BLOCKED` was chosen (not `FAILED`) because missing git identity is resumable by the operator; `FAILED` implies "abandoned / archive." This matches the existing semantics in `workspace.py`'s `VALID_TRANSITIONS`.

**The existing text-based `_parse_agent_outcome` stays** — it's still used to route `scope_check` pass/fail back into the right next stage. Mechanical verification runs *before* routing, as a hard gate.

**Testing:**

- Unit tests per stage verifier (fake workspace, mocked `git rev-parse`, stubbed vcs adapter).
- One e2e test: seed a workspace, run a fake dev-agent that doesn't commit, assert the ticket lands in `BLOCKED` with an error message mentioning "no new commit" — not in `AWAITING_APPROVAL`.

## Component: Validator Library

**Files:** `health/validators.py`, `health/runner.py` (new module).

**Result type:**

```python
@dataclass
class ValidatorResult:
    ok: bool
    name: str             # "jira" | "github" | "gitlab" | "git_identity" | "git_remote"
    target: str           # "ACME project" | "acme/acme-mobile" | "/ws/acme/acme-mobile"
    reason: str           # empty if ok; human-readable error otherwise
    fix_hint: str         # empty if ok; e.g. "git config --global user.email ..."
```

**Validators:**

- `check_jira(url, email, token, project_key)` — thin wrapper over the existing `validate_jira` from `tool_sandbox`, returned as a `ValidatorResult`. Reuses all existing error mapping (401 → "Jira auth failed", 404 → "Project key not found", etc.).
- `check_github(token, owner, repo)` — wrapper over existing `validate_github`.
- `check_gitlab(url, token, project_id)` — wrapper over existing `validate_gitlab`.
- `check_git_identity(workspace_root: Path)` — runs `git -C <workspace> config user.name` and `git -C <workspace> config user.email`. Fails if either is empty (git reads workspace-local first, then global, so this matches how commits actually resolve). `fix_hint`: `git config --global user.email <you@company> && git config --global user.name "<Your Name>"`.
- `check_git_remote(workspace_root: Path, remote: str = "origin")` — runs `git -C <workspace> ls-remote <remote> HEAD`. Fails on auth denied, unreachable host, or not-a-git-dir. Distinguishes in the `reason` field (exit code + last stderr line).

**Runner:**

```python
# health/runner.py
@dataclass
class ProjectHealth:
    project_id: str
    status: str                     # "green" | "yellow" | "red"
    checks: list[ValidatorResult]
    checked_at: datetime

def check_project(project: LoadedProject) -> ProjectHealth: ...
def check_all(projects: dict[str, LoadedProject]) -> list[ProjectHealth]: ...
```

Runs: `jira` once per project, then per-repo either `github` or `gitlab` based on `repo.vcs.provider`. `git_identity` and `git_remote` run per repo against the repo's workspace path if one exists on disk, otherwise are reported as `skipped` (a fourth status in `reason`, not counted as failure).

**Status aggregation:**
- `red` if `jira` is failing, or if the project's active vcs validator (`github` or `gitlab`, whichever is configured) is failing — these prevent progress entirely.
- `yellow` if any of `git_identity`, `git_remote` is failing — these only matter at DEV / push time and don't block earlier stages.
- `green` if everything passes (skipped checks do not downgrade the status).

**Caching:** `check_all` memoizes results in-process for 60 seconds keyed by the project dict's identity. Dashboard refresh button calls `check_all(force=True)` to bypass.

**Testing:** unit tests per validator with mocked `subprocess.run` / HTTP. Integration test over the runner with a fake `LoadedProject`.

## Component: Dashboard Health Panel

**Backend endpoint:**

`GET /api/projects/health` → JSON:

```json
{
  "projects": [
    {
      "project_id": "acme",
      "status": "red",
      "checks": [
        {"name": "jira", "target": "ACME", "ok": true, "reason": "", "fix_hint": ""},
        {"name": "github", "target": "acme/acme-mobile", "ok": true, "reason": "", "fix_hint": ""},
        {"name": "git_identity", "target": "/ws/acme/acme-mobile", "ok": false,
         "reason": "user.email not set", "fix_hint": "git config --global user.email ..."}
      ],
      "checked_at": "2026-04-15T11:30:00Z"
    }
  ]
}
```

Query param `?refresh=1` bypasses the 60s cache.

**Frontend:**

A "Project health" strip at the top of the **board view** (above the project grid). Two states:

- **All green:** a single small green pill saying "All projects healthy" + last-checked timestamp. Collapsed.
- **Anything yellow/red:** auto-expanded, showing one row per unhealthy project. Each row: project id, status dot (red/yellow), then a list of failing checks with their `reason` and `fix_hint` rendered as a monospace one-liner the operator can copy.

A "Refresh health" button next to the strip calls `/api/projects/health?refresh=1`.

**Refresh cadence:**

- Warm-up: `main.py` calls `check_all()` once after daemon startup so the first dashboard load is instant.
- Auto: the existing board polling loop (already 60s) fetches `/api/projects/health` alongside the existing `/api/workspaces` call.
- Manual: the refresh button.

**No pipeline gating.** The panel is advisory; Section "Stage Verifier" is what actually stops bad work from advancing.

**Testing:**

- Unit test for the status-aggregation logic (various check-result combinations → correct `status`).
- E2E test that seeds a fake project config with an intentionally broken git identity, loads the board, asserts the red strip renders with the fix hint visible.

## Component: project-setup-agent Update

Today the project-setup-agent has `validate_jira`, `validate_github`, `validate_gitlab`, `validate_jenkins` in its sandbox. Two gaps:

1. No tool to validate that git commits will succeed on the operator's machine.
2. The agent's prompt/checklist doesn't enforce running the validators before declaring setup complete.

**Changes:**

1. **New sandbox tool `validate_git_identity`** — thin wrapper over `health.validators.check_git_identity`. Input schema: `{workspace_root: str}`. Output: `{ok: bool, user_name: str, user_email: str, reason: str, fix_hint: str}`. Registered in `integrations/llm/tool_sandbox.py` next to the other validators.
2. **Agent checklist update** in `agents/project-setup-agent.md`: the agent must run, in this order, and only report success if all pass:
   - `validate_jira`
   - `validate_github` or `validate_gitlab` (per `vcs.provider`)
   - `validate_git_identity` against the planned workspace root
   - Any failure: stop, print each failing validator's `reason` and `fix_hint`, ask the operator to fix and re-run.
3. **Output contract:** the agent's final report includes a "Health checks" section with each validator's result. The orchestrator's existing `_parse_agent_outcome` path for the `project_setup` stage looks for `status: pass` in that report (same convention as `scope_check` and `qa`).

**Testing:** unit tests for `validate_git_identity` in the sandbox (set and unset identity). No e2e — the project-setup-agent flow has no e2e coverage today and adding it is out of scope.

## Component: Setup Manual Update

**File:** `docs/setup-guide.md` — add a "Prerequisites" subsection.

**Content:**

1. **Git identity** — copyable snippet:
   ```bash
   git config --global user.name "Your Name"
   git config --global user.email "you@company.com"
   ```
   Note that per-workspace overrides (`git config user.email ...` inside the workspace dir) also work — the daemon reads the effective value the same way `git commit` does.
2. **Git remote auth** — one paragraph: "If `git push` works from this shell against your configured repo, the daemon will too." Cross-link to the existing GitHub/GitLab docs for SSH/HTTPS setup. Do not duplicate their content.
3. **Jira token** — cross-reference the existing section.
4. **CLI health check** — one line: `python -m health.runner --config config-live` prints per-project health without starting the daemon. For use in CI and for verifying setup before the first run.

**Cross-references:**
- `docs/features/dashboard.md` — one-line changelog entry for the health panel.
- `docs/features/index.md` — entry for the new health check feature.

## Data Flow

```
   ┌─────────────────┐
   │  dashboard UI   │
   │ (board strip)   │
   └────────┬────────┘
            │ GET /api/projects/health
            ▼
   ┌──────────────────┐
   │ dashboard/web.py │
   └────────┬─────────┘
            │
            ▼
   ┌──────────────────┐      ┌──────────────────────┐
   │ health/runner.py ├─────►│ health/validators.py │
   └──────────────────┘      └──────────────────────┘
                                      ▲
   ┌──────────────────────────┐       │
   │ orchestrator/            │       │
   │ stage_verifier.py        │       │  (imports)
   └──────────┬───────────────┘       │
              │                       │
              │                       │
   ┌──────────▼───────────────┐       │
   │ orchestrator/            │       │
   │ orchestrator.py          │       │
   │ (calls verify per stage) │       │
   └──────────────────────────┘       │
                                      │
   ┌──────────────────────────┐       │
   │ integrations/llm/        │       │
   │ tool_sandbox.py          │       │
   │ (validate_git_identity)  ├───────┘
   └──────────────────────────┘
```

## Failure Modes + Error Handling

| Failure                                   | What the operator sees                                                                      |
|-------------------------------------------|-----------------------------------------------------------------------------------------------|
| Jira 401                                  | Red strip on board; `check_jira` reports auth failed + fix hint. Stage verifier would also block BA at the Jira step if it got that far. |
| GitHub token revoked                      | Red strip; `check_github` fails. Push stage would BLOCK via `check_git_remote`.              |
| git user.email missing                    | Yellow strip; dev stage BLOCKs with `dev: no new commit since stage start (agent reported: git identity not configured)`. |
| Remote `origin` unreachable               | Yellow strip; push stage BLOCKs with `push: remote branch does not match local`.             |
| Validator itself errors (e.g. subprocess timeout) | `ok=False`, `reason="validator internal error: <exc>"`, no fix hint. Logged as warning; not treated as a pipeline blocker. |

Validators must not raise exceptions — all failures are returned as `ValidatorResult(ok=False, ...)`. This is tested explicitly.

## Rollout

No migration required. The new `health/` module is additive; the stage verifier adds assertions but does not change any existing stage's success path. Existing workspaces continue to work without change; the next time a stage runs, mechanical verification kicks in.

## Open Questions

None outstanding — all design decisions confirmed during brainstorming.

## Acceptance Criteria

- [ ] `health/validators.py` with 5 validators, all returning `ValidatorResult`, no exceptions.
- [ ] `health/runner.py` with 60s in-process cache and status aggregation.
- [ ] `orchestrator/stage_verifier.py` with verifiers for dev, scope_check, qa, push, pr_review.
- [ ] Orchestrator captures `stage_start_commit` before each stage and calls `verify` after.
- [ ] Failing verification transitions workspace to `BLOCKED` with the reason in `state.error`.
- [ ] `GET /api/projects/health` endpoint with `?refresh=1` cache bypass.
- [ ] Board view renders a health strip; auto-expand when anything is yellow/red; refresh button works.
- [ ] `validate_git_identity` registered in the tool sandbox with unit tests.
- [ ] `project-setup-agent.md` checklist enforces all validators before declaring setup complete.
- [ ] `docs/setup-guide.md` has a Prerequisites section with git identity, remote auth, and CLI health check.
- [ ] Unit tests per validator, unit tests per stage verifier, one e2e that reproduces ACME-14595 (dev stage that doesn't commit → BLOCKED, not AWAITING_APPROVAL), one e2e that renders a red health strip.
- [ ] `docs/features/dashboard.md` and `docs/features/index.md` updated with the new feature.
