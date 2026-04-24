# Design: PR Review Pipeline Redesign

**Status:** Design
**Created:** 2026-04-24
**Author:** Oleksandr Brazhenko

## Problem

The PR review stage has fundamental issues causing infinite loops, false "Fixed" replies, and lost decisions:

1. **No single source of truth** — `pr-comment-fixes.md` gets overwritten each cycle. Previous decisions are lost. Comments get re-classified on every cycle.
2. **False "Fixed" replies** — the pipeline replies "Fixed in latest push" on GitHub without verifying the git diff. Dev-agent may not have applied the fix.
3. **Dual reports directories** — `workspace_root/reports/` (orchestrator) vs `source/reports/` (agents). The dev-agent can't find fix instructions written by the orchestrator.
4. **No verification** — nobody checks whether the code actually changed after a "fix" decision.
5. **Loop** — classify → fix instruction → dev ignores it → push unchanged code → re-classify same comments → repeat 10+ times.

## Goals

- Resolution report is the single source of truth for PR comment decisions
- Comments decided in cycle 1 are never re-asked in cycle 2
- "Fixed" replies are only posted after verifying the file was modified in the push
- Failed fixes escalate to human after 2 attempts
- One reports directory — agents and orchestrator read/write the same place
- Pipeline log references the resolution report, not duplicating decisions

## Non-Goals

- Redesigning stages before PR_REVIEW (analysis → dev → code check → QA → push work fine)
- Changing how agents are dispatched (parallel, polling, etc.)
- Auto-merging PRs

## Component: Unified Reports Directory

**Current:** Two separate directories:
- `workspace_root/reports/` — orchestrator writes agent outputs, pipeline-log, resolution report
- `source/reports/` — agents write ba.md, developer.md, qa.md, scope-guard.md via their tools

**New:** Single directory at `source/reports/`.

Change `workspace.reports_dir` to return `source/reports/`. All writes go there. Agent outputs (`dev-agent-output.md`), orchestrator data (`pipeline-log.md`, `pr-review-resolution.md`), and agent-written reports (`ba.md`, `developer.md`) all coexist in one directory.

**Files changed:**
- `workspace/workspace.py` — `reports_dir` property returns `source_dir / "reports"`
- `workspace/workspace_manager.py` — `create()` makes `source/reports/` only
- `orchestrator/agent_runtime.py` — remove reports_dir from context injection (agent reads from cwd directly)
- `orchestrator/orchestrator.py` — remove all "write to both locations" workarounds

**Migration:** On workspace discovery, if old `workspace_root/reports/` has files not in `source/reports/`, move them over.

## Component: Resolution Report (Source of Truth)

**File:** `source/reports/pr-review-resolution.md`

Each PR comment gets ONE permanent entry. Format:

```markdown
# PR Review Resolution — ACME-12079
PR: #1488

## Comment #3129881538
- File: TaskRosterCompose.kt:366
- Author: @Copilot
- Body: ZoneId.of("US") is not a valid IANA time-zone ID...
- Decision: FIX
- Decided by: auto-classifier
- Decided at: 2026-04-23 14:08
- Verified: YES
- Verified at: 2026-04-23 14:35
- Verify commit: 28ecfd3
- GitHub reply: Fixed in commit 28ecfd3
- Resolved: YES

## Comment #3129881441
- File: EventItem.kt:60
- Author: @Copilot
- Body: This file still uses bare @Preview...
- Decision: WON'T_FIX
- Decided by: auto-classifier
- Reason: Pre-existing code not modified by this PR
- GitHub reply: Won't fix: Pre-existing code not modified by this PR
- Resolved: YES
```

**Rules:**
1. Before classifying, parse the resolution report. Collect all comment IDs that already have a decision. Skip them during fetch/classify.
2. FIX decisions: `Verified: PENDING` until push. After push, check git diff.
   - File in diff → `Verified: YES`, reply "Fixed in commit {sha}", resolve on GitHub
   - File NOT in diff → `Verified: FAILED`, increment `fail_count`
   - `fail_count >= 2` → escalate to TG: "Dev-agent failed to apply this fix twice"
3. WON'T_FIX / SKIP: reply and resolve on GitHub immediately. Final.
4. AUTO_REJECT: reply and resolve immediately. Final.

**Reading/writing:**
- `_read_resolution_report(workspace) -> dict[int, dict]` — parse the file, return comment_id → entry map
- `_update_resolution_entry(workspace, comment_id, updates)` — update a single entry in place
- `_add_resolution_entry(workspace, comment_id, entry)` — add a new comment entry

## Component: Rewritten PR Review Flow

```
_action_fetch_pr_comments:

  Phase 1: Check pending verifications
    Read resolution report
    For each entry with Verified=PENDING:
      Check git diff (file in latest commit?)
      YES → update to Verified=YES, reply on GitHub, resolve
      NO → increment fail_count
        fail_count >= 2 → escalate to TG
        fail_count < 2 → leave as PENDING

  Phase 2: Wait for "reviewed" signal (if no pending decisions/escalations)
    If human_input_reply != "reviewed" → skipped

  Phase 3: Fetch + filter comments
    Fetch all PR comments from GitHub
    Filter: root only (no replies)
    Filter: skip comment IDs already in resolution report
    If 0 new comments AND 0 PENDING verifications → DONE

  Phase 4: Classify new comments via agent
    Send only NEW comments to classifier
    For each classification:
      AUTO_REJECT → add to report (Decision: WON'T_FIX), reply on GitHub, resolve
      AUTO_FIX → add to report (Decision: FIX, Verified: PENDING)
      ESCALATE → send to TG with Fix/Skip/Won't Fix buttons

  Phase 5: Wait for escalated decisions
    Store in pending_review_comments
    Return skipped until all decisions in

  Phase 6: Execute decisions
    For WON'T_FIX/SKIP: update report, reply on GitHub (if won't fix), resolve
    Collect all FIX decisions → write pr-comment-fixes.md
    Update report entries (Decision: FIX, Verified: PENDING)
    → DEV

  After DEV → Code Check → QA → Push:

  Phase 7 (in push handler): Verify fixes
    Read resolution report
    For each PENDING entry:
      git diff includes the file? → Verified: YES, reply, resolve
      Not in diff? → Verified: FAILED, fail_count++
    → PR_REVIEW (loop for remaining)
```

## Component: Push Handler Verification

In `_action_push_and_open_pr`, after successful push to existing PR:

```python
# Read resolution report
entries = _read_resolution_report(workspace)
diff_files = git_diff_files(workspace)  # set of changed file paths

for comment_id, entry in entries.items():
    if entry.get("verified") != "PENDING":
        continue
    target_file = entry.get("file", "")
    if any(target_file.endswith(f) or f.endswith(target_file) for f in diff_files):
        # File was modified — fix applied
        sha = git_head_sha(workspace)
        _update_resolution_entry(workspace, comment_id, {
            "verified": "YES",
            "verified_at": now(),
            "verify_commit": sha,
            "github_reply": f"Fixed in commit {sha[:8]}",
            "resolved": "YES",
        })
        await vcs.reply_to_comment(pr_number, comment_id, f"Fixed in commit {sha[:8]}.")
        await vcs.resolve_comment(pr_number, comment_id)
    else:
        fail_count = entry.get("fail_count", 0) + 1
        _update_resolution_entry(workspace, comment_id, {
            "verified": "FAILED",
            "fail_count": fail_count,
        })
        if fail_count >= 2:
            # Escalate — dev-agent failed twice
            await notify_fix_failed(workspace, entry)
```

## Component: Pipeline Log Enrichment

`pipeline-log.md` entries become richer:

```python
# Stage completion
self._log_pipeline(workspace, f"dev completed. Commit: {sha}. → SCOPE_CHECK")

# PR review
self._log_pipeline(workspace, f"PR review: 3 new comments, 2 auto-rejected, 1 fix. See pr-review-resolution.md")

# Verification
self._log_pipeline(workspace, f"Push verified: ZoneId fix applied (TaskRosterCompose.kt in diff). 1 pending.")

# Done
self._log_pipeline(workspace, f"✅ DONE. PR: #1488. All comments resolved.")
```

## Component: State Changes

**Remove:** `comments_to_resolve: list[int]` from WorkspaceState — replaced by resolution report entries with `Verified: PENDING`.

**Keep:** `pending_review_comments`, `review_cycle` — still needed for TG escalation flow.

## Testing

- Unit: `_read_resolution_report` / `_update_resolution_entry` / `_add_resolution_entry` parse and write correctly
- Unit: verification logic — file in diff → YES, not in diff → FAILED, fail_count >= 2 → escalate
- Unit: skip already-decided comments from resolution report
- Unit: 0 new comments + 0 pending → DONE
- Integration: full cycle — classify → fix → push → verify → DONE

## Acceptance Criteria

- [ ] Single `source/reports/` directory for all report files
- [ ] Resolution report parsed before every classify — decided comments skipped
- [ ] FIX decisions start as Verified: PENDING
- [ ] After push, git diff verified per PENDING entry
- [ ] "Fixed" reply only posted when file confirmed in diff
- [ ] Failed verification after 2 attempts escalates to TG
- [ ] WON'T_FIX/SKIP/AUTO_REJECT reply and resolve immediately
- [ ] Pipeline log includes commit hashes and references resolution report
- [ ] No "write to both locations" workarounds remain
- [ ] No re-classification of already-decided comments
