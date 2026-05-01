# Rerun Pipeline Feature

**Date:** 2026-05-01
**Status:** Approved

## Problem

Two scenarios require restarting the pipeline from scratch for a DONE ticket:

1. **Disk space reclaim** — operator used "Remove codebase" (clean_source) to free disk space; now wants to process the same ticket again.
2. **Post-manual-QA rerun** — ticket is DONE but QA found issues; operator wants to rerun the full pipeline with context about what went wrong.

The current "Retry" action resumes from a detected stage and keeps existing source. It does not support a clean restart with fresh code and updated ticket context.

## Goals

- Add a **Rerun** button (DONE state only) with a required reason dialog.
- Re-clone the codebase (handles both cases: source present or already removed).
- Refresh ticket data from the tracker (append, not overwrite — agents see what changed).
- Pass the rerun reason to BA as additional context.
- Notify operator via Telegram which branch was checked out.

## Non-goals

- Rerun from states other than DONE.
- Deleting or resetting the remote feature branch.
- Clearing existing reports (kept as context for agents).

---

## Design

### 1. WorkspaceManager.reset_source()

New method in `workspace/workspace_manager.py` alongside `cleanup_source()`.

**Signature:**
```python
def reset_source(
    self,
    workspace: Workspace,
    clone_url: str,
    default_branch: str,
) -> str:  # returns the branch that was checked out
```

**Steps:**
1. `shutil.rmtree(source_dir)` if `source_dir.exists()` — wipe existing source (may already be absent).
2. `git clone <clone_url> source/` — fresh clone.
3. Try `git checkout <branch_name>` (from `workspace.state.branch`). If the remote branch no longer exists, fall back to `git checkout <default_branch>`.
4. `mkdir source/reports/` — restore reports dir (agents expect it).
5. Return the branch name that was actually checked out.

The caller (rerun endpoint) resolves `clone_url` and `default_branch` from `global_config` using `ws.state.company_id` + `ws.state.repo_id`.

---

### 2. Orchestrator._refetch_ticket_data()

Extract the ticket-fetch block from `_create_workspace_for_ticket` into a reusable async method.

**Signature:**
```python
async def _refetch_ticket_data(self, workspace: Workspace, ticket: Ticket) -> None:
```

**Behavior — first run (file absent):** write fresh content (existing behavior, unchanged).

**Behavior — rerun (file exists):** append a timestamped refresh block:

- `meta/ticket.md` — appends `## Refresh <date>\n<latest description>`. Agent sees original + updated; can flag PM description changes in report.
- `meta/comments.md` — appends only new comments (identified by comment ID to avoid duplicates).
- `meta/history.md` — appends only new status transitions.
- `meta/attachments/` — downloads new attachments, keeps old ones.

`_create_workspace_for_ticket` is updated to call `_refetch_ticket_data` (no duplication).

---

### 3. POST /api/workspaces/{ticket_id}/rerun

New endpoint in `dashboard/actions.py`.

**Request body:** `{ "reason": "<non-empty string>" }`

**Steps:**
1. Validate state is `DONE`. Return 400 otherwise.
2. Validate `reason` is present and non-empty.
3. Append to `meta/rerun_history.md`:
   ```
   ## Rerun 2026-05-01T10:00:00Z
   <reason>
   ```
   Multiple reruns accumulate in this file. BA agent reads it as input context.
4. Fetch latest ticket from tracker using `ws.state.ticket_id`.
5. Call `await orchestrator._refetch_ticket_data(workspace, ticket)` — appends refreshed data to meta files.
6. Resolve `repo_config` from `global_config` via `ws.state.company_id` + `ws.state.repo_id`. Call `workspace_manager.reset_source(workspace, clone_url, default_branch)`.
7. Clear stale state fields: `pr_number`, `pr_url`, `last_verified_sha`, `review_cycle`, `pending_review_comments`, `error`, `stage_iterations`.
8. Transition to `ANALYSIS`.
9. Send Telegram notification: rerun started, reason, which branch was checked out (feature branch or develop fallback).
10. Emit `dashboard_rerun` event.
11. Return `{"status": "ok", "new_state": "ANALYSIS", "branch": "<branch>"}`.

---

### 4. Frontend

**`dashboard/static/js/detail.js`**
- Add Rerun button to the action bar when state is `DONE`.
- Calls `rerunWorkspace(ticketId)` on click.

**`dashboard/static/js/actions.js`**
- New `rerunWorkspace(ticketId)` function:
  1. Show dialog (reuse `showConfirmDialog` pattern) with a `<textarea>` labelled "Reason for rerun". Submit disabled if empty.
  2. `POST /api/workspaces/{ticket_id}/rerun` with `{ reason }`.
  3. On success: refresh workspace detail view, show toast with branch name from response (e.g. *"Rerun started on feature/TICKET-123-..."* or *"Branch not found — checked out develop"*).

No new modal HTML required — existing confirm dialog pattern supports custom content.

---

## Files Changed

| File | Change |
|------|--------|
| `workspace/workspace_manager.py` | Add `reset_source()` |
| `orchestrator/orchestrator.py` | Extract `_refetch_ticket_data()`, update `_create_workspace_for_ticket` to call it |
| `dashboard/actions.py` | Add `rerun` endpoint + route |
| `dashboard/static/js/detail.js` | Add Rerun button for DONE state |
| `dashboard/static/js/actions.js` | Add `rerunWorkspace()` function |

## Data Flow

```
User clicks Rerun → reason dialog → POST /rerun
  → append rerun_history.md
  → _refetch_ticket_data (append meta files)
  → reset_source (reclone, checkout branch or develop)
  → clear stale state fields
  → transition to ANALYSIS
  → Telegram notification (branch checked out)
  → orchestrator poll picks up ANALYSIS → advance_workspace
```
