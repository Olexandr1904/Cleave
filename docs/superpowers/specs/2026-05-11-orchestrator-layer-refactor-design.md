# Orchestrator Layer Refactor — Design Spec

**Date:** 2026-05-11
**Scope:** Cut `orchestrator/orchestrator.py` (2,801 lines, one class, ~50 methods) into focused modules organized by layer. Generalize the tracker abstraction so a Trello adapter slots in later without touching orchestrator code; same for GitLab on the VCS side.

---

## Background

`orchestrator/orchestrator.py` has grown to 2,801 lines on a single `Orchestrator` class. It mixes:

- daemon lifecycle (signals, poll loop, semaphore, shutdown)
- config hot-reload
- ticket ingest (poll → filter → route → prioritize → create workspace)
- state machine driver (`advance_workspace`)
- agent stage execution
- action stage execution (push+PR / fetch PR comments / finalize)
- PR review escalation loop (~350 lines inside one method)
- ticket sync to disk (`ticket.md`, `comments.md`, `history.md`, attachments)
- approval gating + auto-resume on mode switch
- escalation / BLOCKED messaging
- Telegram message construction (28 `_notifier.send_message` calls with inline formatting)
- Jira-specific transitions (peeking through `_tracker._request`, `_email`, `_token`)
- deferred sweeping / quota debouncing
- git plumbing (commit pipeline artifacts, ensure branch has commits, squash feature commits)

**The two coupling problems blocking multi-provider work:**

1. **Tracker abstraction is leaky.** Orchestrator reaches into `self._tracker._request(...)` four times for Jira-shaped JSON (`/issue/{id}?expand=changelog`, `/issue/{id}/transitions`). It also reads `tracker._email/_token` to build HTTP Basic auth for raw attachment downloads. None of this survives Trello.

2. **VCS abstraction is mostly clean** but two issues: `repo_config.vcs.provider == "github"` ternaries in orchestrator (for `default_branch`/`branch_prefix`), and `RepoConfig.jira_repo_label` is tracker-flavored when it should be tracker-neutral (`tracker_label`).

---

## Goals & non-goals

### Goals
- Reduce `Orchestrator` class to ~300–400 lines of pure wiring + active-workspace bookkeeping.
- Generalize `TrackerInterface` so Jira-shaped HTTP paths don't leak into orchestrator.
- Reorganize so files map cleanly to what they coordinate, not what infrastructure they touch.
- Existing tests pass without behavior changes (with at most import-path updates).

### Non-goals (explicit)
- ✗ No new adapters (Trello / GitLab) in this PR — just make them cheap to add later.
- ✗ No behavior changes. Same TG messages, same gates, same retry logic, same Jira fuzzy-match rules.
- ✗ No new features or "while I'm here" cleanups of unrelated code.
- ✗ No new functional tests beyond the characterization tests in Step 0.

---

## Architecture: three layers

```
Layer 1 — Runtime / application (the daemon shell)
  orchestrator/runtime.py
      Runtime class. Owns: signal handling, poll loop, wake event,
      semaphore, shutdown, active_workspaces, recent_completions.

Layer 2 — Coordination / use-cases (the verbs)
  orchestrator/ingest.py            poll → filter → route → create_workspace
                                    + analyze_ticket_ids manual queue
  orchestrator/pipeline/
      driver.py                     advance_workspace, max-iteration check, gate dispatch
      agent_stage.py                AgentStageExecutor (former _handle_agent_stage)
      action_stage.py               ActionStageDispatcher (former _handle_action_stage)
      actions/
          push_and_open_pr.py       + _commit_pipeline_artifacts
                                    + _ensure_branch_has_commits
                                    + _squash_feature_commits
          fetch_pr_comments.py      + _reinvestigate_pending
                                    + _execute_review_decisions
                                    + _send_escalated_comment_tg
          finalize.py               + _on_ticket_done
                                    + Jira "in_review" transition (via tracker port, no _request)
  orchestrator/ticket_sync.py       _refetch_ticket_data + attachment download
                                    + _attachment_is_keepable + _ticket_to_markdown
  orchestrator/approval_gate.py     _should_approval_gate + auto-resume on mode switch
  orchestrator/notify.py            One function per TG event (notify_deferred, notify_failed,
                                    notify_pr_opened, notify_qa_warnings, notify_escalation, …)
  orchestrator/git_ops.py           _git_diff_files, _git_head_sha
  orchestrator/escalation.py        _handle_escalate, _build_blocked_reason, _truncate_reason,
                                    _notify_verification_blocked

Layer 3 — Ports & adapters (already exists, just tighten)
  integrations/base/tracker.py      TrackerInterface (expanded — see below)
  integrations/base/vcs.py          unchanged
  integrations/base/notifier.py     unchanged
  integrations/jira/jira_adapter.py implements expanded TrackerInterface
  integrations/github/...           unchanged
```

`orchestrator/orchestrator.py` after the cut: thin wiring container. Constructor takes ports + Layer-2 modules. `run()` delegates to `Runtime`. `poll_cycle()` calls `Ingest.poll`, then `PipelineDriver.advance_all`, then cleanup. Target: ~300–400 lines.

### Why this split (and not a different one)

- Files are organized by *what they coordinate*, not by *what infrastructure they touch*. `ticket_sync.py` is "keep ticket meta on disk in sync with the tracker" — it doesn't matter whether the tracker is Jira or Trello. The ports below isolate transport.
- The pipeline subfolder is justified by size (`advance_workspace` + handlers is the largest connected piece, ~600 lines today).
- No artificial DDD ceremony. No `Entity`/`Repository`/`UseCase` layers, no domain events. Existing dataclasses (`TicketData`, `Workspace`, `Stage`) stay where they are.

---

## Tracker interface changes (the only interface change in this PR)

```python
# integrations/base/tracker.py — additions

@dataclass
class TicketComment:
    id: str
    author: str
    created: str        # ISO date, "YYYY-MM-DD" sufficient
    body: str           # plain text; ADF parsing done in adapter

@dataclass
class StatusChange:
    created: str        # ISO date
    from_status: str
    to_status: str
    author: str

class TrackerInterface(ABC):
    # existing — unchanged
    async def poll_tickets(self) -> list[TicketData]: ...
    async def get_ticket(self, ticket_id: str) -> TicketData: ...
    async def transition_ticket(self, ticket_id: str, status: str) -> None: ...
    async def add_comment(self, ticket_id: str, comment: str) -> None: ...

    # NEW
    async def get_comments(self, ticket_id: str) -> list[TicketComment]: ...
    async def get_status_history(self, ticket_id: str) -> list[StatusChange]: ...
    async def download_attachment(self, url: str) -> bytes: ...
    async def list_transitions(self, ticket_id: str) -> list[str]: ...
```

| New method | Replaces in orchestrator | Trello mapping (future) |
|---|---|---|
| `get_comments` | `_tracker._request("GET", "/issue/{id}?expand=changelog&fields=comment")` + ADF parsing | `GET /cards/{id}/actions?filter=commentCard` |
| `get_status_history` | Same `_request` call, parses `changelog.histories[].items[]` | `GET /cards/{id}/actions?filter=updateCard:idList` |
| `download_attachment` | Raw `httpx.get` with `Basic base64(tracker._email:tracker._token)` headers | Trello attachments + API key/token in URL |
| `list_transitions` | `_tracker._request("GET", "/issue/{id}/transitions")` | Returns Trello list names; same shape |

**Fuzzy-match policy stays in orchestrator.** `_on_ticket_done` does "find a transition whose name contains 'review' / 'qa' / 'verification'". That's a pipeline policy, not a transport detail. It moves to `pipeline/actions/finalize.py` alongside `_action_finalize`, and calls `tracker.list_transitions()` + `tracker.transition_ticket(id, name)` — identical for Jira and Trello.

**Notifier interface:** no changes.
**VCSInterface:** no changes. GitLab adapter will be a drop-in implementation when written.

---

## Config schema changes (small, additive)

```python
# config/schemas.py
class VCSConfig:
    provider: str = "github"
    default_branch: str = "develop"      # ← hoisted from GitHubConfig/GitLabConfig
    branch_prefix: str = "feature"        # ← hoisted
    github: GitHubConfig = ...
    gitlab: GitLabConfig = ...
    skip_pre_push_hook: bool = False
```

Removes the two `provider == "github"` ternaries in `_create_workspace_for_ticket`.

```python
class RepoConfig:
    ...
    tracker_label: str = ""               # ← renamed from jira_repo_label
```

Loader accepts the old name `jira_repo_label` as a backward-compat alias (≤ 5 lines in `config_loader.py`). Alias can be removed once all live config files (`config-live/`) have migrated — out of scope for this PR.

---

## Step 0: Characterization tests (added before Step A)

**Why first.** Audit of the 14 existing orchestrator-relevant test files shows strong coverage on leaf helpers (`_build_blocked_reason`, `_notify_failed`, `_refetch_ticket_data`, `_squash_feature_commits`, approval gating, hot reload, mode switching, quota → deferred, stage verification) but critical gaps on the connective tissue most exposed by a layer-cut:

| Gap | Module impact | Risk if undetected |
|---|---|---|
| `_action_fetch_pr_comments` review loop (`_reinvestigate_pending`, `_execute_review_decisions`) — ~350 lines | `pipeline/actions/fetch_pr_comments.py` | Wrong replies posted to real GitHub PRs |
| `_poll_and_create_workspaces` | `ingest.py` | Tickets duplicated or silently dropped after restart |
| `advance_workspace` happy path (agent success → next; action success → next; max-iter → escalate) | `pipeline/driver.py` | State machine routes to wrong stage |
| `_handle_agent_stage` success path (outcome parsing, QA-warning detection, scope-check counter reset) | `pipeline/agent_stage.py` | Stages advance wrong, scope_check counter never resets |
| `_on_ticket_done` (Jira "in_review" transition, final notification) | `pipeline/actions/finalize.py` | Tickets finish but Jira doesn't get the status update |
| `_reconcile_disk_workspaces` | `runtime.py` | Daemon restart loses workspaces |
| `_notify_deferred / _notify_rerun / _notify_verification_blocked` | `notify.py` | Wrong messages to operators |

Add the following test files **before** any production code moves:

```
tests/unit/test_orchestrator_poll_create.py       — ingest path: route, dedupe, parallel cap, dry-run
tests/unit/test_orchestrator_advance_happy.py     — state machine: agent success, action success, max-iter
tests/unit/test_orchestrator_pr_review_loop.py    — 5–6 cases:
                                                    • empty PR comments → done
                                                    • all classified auto-fixable → routed to dev
                                                    • one comment escalated → BLOCKED + TG msg
                                                    • human reply "fix" → routes to dev
                                                    • human reply "won't fix: <reason>" → reply + resolve + advance
                                                    • _reinvestigate_pending with PENDING entry
tests/unit/test_orchestrator_finalize.py          — fuzzy transition match against multiple shapes;
                                                    Jira transition called via list_transitions + transition_ticket
tests/unit/test_orchestrator_done.py              — _on_ticket_done: tracker transition + DONE notification
tests/unit/test_orchestrator_reconcile.py         — disk → active list adoption
tests/unit/test_orchestrator_notify_misc.py       — deferred / rerun / verification_blocked messages
```

Scope estimate: ~15–20 tests, ~600–800 LOC, ~1 day of focused work.

**These tests are characterization tests, not correctness tests** — they pin down "whatever the current behavior is, that's the contract." They will need import-path updates during the move (because methods will live in different modules), and that's expected — the assertion of *no behavior change* is what they preserve.

---

## Internal ordering inside the PR

Sequence the diff so each step is small enough to reason about. Run `pytest tests/unit/test_orchestrator_*` after each step.

### Step 0 — Characterization tests (above)

### Step A — Tracker port expansion (additive, no orchestrator changes)
1. Add `TicketComment`, `StatusChange` dataclasses to `integrations/base/tracker.py`.
2. Add abstract methods (`get_comments`, `get_status_history`, `download_attachment`, `list_transitions`).
3. Implement them in `JiraAdapter`: move `_extract_adf_text`, changelog parsing, Basic-auth header building from orchestrator into the adapter.
4. ✅ Tests still pass — nothing called the new methods yet.

### Step B — Cut orchestrator's direct `_request` / `_email` / `_token` access
1. `_refetch_ticket_data` (orchestrator.py:540–691): replace the 4 `_request` calls and the raw `httpx` attachment download with the new tracker methods.
2. `_on_ticket_done` (orchestrator.py:2244–2308): replace the 3 `_request` calls with `list_transitions` + `transition_ticket`.
3. ✅ Tests still pass — `orchestrator.py` is still one file, just no longer Jira-shaped.

### Step C — Config schema tightening
1. Hoist `default_branch` / `branch_prefix` from `GitHubConfig` / `GitLabConfig` up to `VCSConfig`.
2. Rename `RepoConfig.jira_repo_label` → `tracker_label`; loader accepts the old name with one-line alias.
3. Remove the `provider == "github"` ternaries in `_create_workspace_for_ticket`.
4. ✅ Tests pass; existing config files unchanged.

### Step D — Extract leaf modules (no orchestrator class changes yet)
- `orchestrator/git_ops.py` ← `_git_diff_files`, `_git_head_sha`
- `orchestrator/ticket_sync.py` ← `_refetch_ticket_data` + `_attachment_is_keepable` + `_ticket_to_markdown`
- `orchestrator/notify.py` ← each `notifier.send_message(...)` formatting block becomes a named function
- `orchestrator/approval_gate.py` ← `_should_approval_gate` + auto-resume block from `advance_workspace`
- `orchestrator/escalation.py` ← `_handle_escalate`, `_build_blocked_reason`, `_truncate_reason`, `_notify_verification_blocked`
- `orchestrator/pipeline/actions/push_and_open_pr.py` ← `_action_push_and_open_pr` + commit artifacts + ensure-branch + squash
- `orchestrator/pipeline/actions/fetch_pr_comments.py` ← `_action_fetch_pr_comments` + `_reinvestigate_pending` + `_execute_review_decisions` + `_send_escalated_comment_tg`
- `orchestrator/pipeline/actions/finalize.py` ← `_action_finalize` + `_on_ticket_done`
- ✅ Orchestrator class now delegates; tests pass.

### Step E — Extract pipeline driver and runtime
- `orchestrator/pipeline/agent_stage.py` ← `_handle_agent_stage` as `AgentStageExecutor`
- `orchestrator/pipeline/action_stage.py` ← `_handle_action_stage` dispatcher
- `orchestrator/pipeline/driver.py` ← `advance_workspace`, iteration cap check, gate dispatch
- `orchestrator/ingest.py` ← `_poll_and_create_workspaces` + `_create_workspace_for_ticket` + `_route_manual_ticket` + `analyze_ticket_ids`
- `orchestrator/runtime.py` ← `run` + `poll_cycle` outer loop + `_reconcile_disk_workspaces` + `_sweep_deferred` + signal handling
- ✅ `Orchestrator` now ~300 lines, mostly DI wiring.

### Step F — Test imports
- Update test imports where they grab internals. Where the test exercises a method *as a method on `Orchestrator`*, keep that method as a thin facade.

### Step G — Cleanup
- Remove backward-compat shims.
- Run full suite + a dry-run cycle end-to-end.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Hidden state shared between methods (`_quota_window_end`, `_recent_completions`, `_agent_semaphore`, `_active_workspaces`) | Assign owners upfront: `_quota_window_end` → `Notify`; `_recent_completions` & `_active_workspaces` → `Runtime`; semaphore → `Runtime`. Pass references explicitly. |
| 45 test files reach into orchestrator internals — moving methods breaks imports | Keep `orchestrator/orchestrator.py` as a facade for one cycle: re-export moved functions as method shims on `Orchestrator`. Drop shims in Step G when tests migrate. |
| Tail-call recursion in `advance_workspace` (`_resume_depth`) | Keep `advance_workspace` as a method on `PipelineDriver`; recursion stays internal to that class. No cross-module recursion. |
| Jira ADF parsing currently imported lazily inside `_refetch_ticket_data` | Move into `JiraAdapter` as private helper. Orchestrator never sees ADF — `TicketComment.body` is plain text by contract. |
| Big-bang = no rollback half-way | Step 0 characterization tests + run `pytest tests/unit/test_orchestrator_*` after each step in the order above. Stop and fix on any red. |
| Tests written against the leaky tracker (mocking `_request`) | Audit needed: `grep -r "tracker._request\|_email\|_token" tests/`. Likely a handful; update mocks to the new public methods. |
| `pr_creation.py` and `helpers.*` GitHub-isms | Out of scope. Listed in follow-ups below. |

**Highest-risk area:** `_action_fetch_pr_comments` (~350 lines). Characterization tests in Step 0 must cover the 5–6 branches listed above. Code review should read this section twice.

---

## Out of scope (follow-ups)

- Trello adapter implementation
- GitLab adapter implementation
- `pr_creation.py` GitHub-isms (separate refactor)
- Telegram message text changes (intentionally preserved verbatim)
- Bug fixes opportunistically discovered during the move (file separate issues)
- Generalizing CI integration (Jenkins is already a sub-config under `CIConfig`; not exercised in this refactor)

---

## Success criteria

- `orchestrator/orchestrator.py` ≤ 400 LOC, only DI wiring + active-workspace bookkeeping.
- No `_request`, `_email`, `_token` references on `tracker` outside `integrations/jira/`.
- No `provider == "github"` ternaries in `orchestrator/`.
- All existing tests pass (with at most import-path updates).
- All Step 0 characterization tests pass before and after the refactor.
- One full dry-run cycle passes end-to-end on a real workspace.
