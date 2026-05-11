# Feature: Orchestrator

**Status:** Implemented
**Created:** 2026-04-07
**Updated:** 2026-04-21
**Author:** Oleksandr Brazhenko

## Description

Central daemon process that continuously polls for work, manages isolated workspaces, and dispatches BMAD-style agents via the workflow router. The orchestrator determines which agent to invoke based on ticket state and `state.json`, supports configurable workflow definitions, and enforces iteration caps with human escalation.

## Requirements

- FR1: Orchestrator determines which agent to invoke based on ticket state and `state.json`
- FR2: Default routing: unclear ticket → BA/PM Agent; clear → Dev Agent; code written → QA Agent; review comments → Fix Agent; all gates passed → Merge Agent
- FR3: Routing logic configurable via workflow definitions specifying agent sequence and transition conditions
- FR4: Supports looping with configurable iteration caps per stage
- FR5: Main loop runs on configurable `poll_interval_seconds`
- FR6: Each cycle: poll tracker → check new tickets → check slot availability → spawn workspaces → advance active workspaces
- FR7: Slot limits enforced per-repo and per-project
- FR8: `--dry-run` flag polls tickets and logs what would happen without executing
- FR9: Handles exceptions per-workspace without crashing the daemon
- FR10: SIGTERM/SIGINT triggers graceful shutdown: finish current agent calls, save state, exit
- FR11: Mode-aware behavior — in `manual` mode the orchestrator skips Jira polling and pauses workspaces at approval gates (ANALYSIS, QA, PR_REVIEW) by transitioning to `AWAITING_APPROVAL` and sending a Telegram summary; `auto` mode runs end-to-end without gates

## Technical Approach

- Single long-running asyncio process
- Workflow router reads `state.json` and applies transition rules from config
- Default workflow: PM → BA → Dev → Scope Guard → PR → Fix → QA → Merge
- Conditional transitions: scope guard fail → Dev; QA fail → Dev; max iterations → escalate
- Escalate state triggers Telegram notification and sets `status: waiting_for_human`
- Workspace advancement is per-workspace: invoke next agent, update state, handle result

## Dependencies

- Agent System for dispatching agents
- Workspace Isolation for workspace management and state
- All integration adapters (Jira, GitHub, Telegram)
- Configuration Cascade for workflow definitions and settings

## Acceptance Criteria

- [ ] Main loop polls for tickets and advances workspaces on each cycle
- [ ] Workflow router correctly sequences agents based on state
- [ ] Conditional transitions work (scope guard loop, QA loop)
- [ ] Iteration caps trigger escalation at configured max
- [ ] Dry-run mode logs actions without executing
- [ ] Graceful shutdown on SIGTERM/SIGINT
- [ ] One workspace failure does not crash the daemon
- [ ] Orchestrator honors `pipeline.mode` (auto/manual): skips polling in manual, pauses at approval gates, and does not advance workspaces in `AWAITING_APPROVAL`
- [ ] Orchestrator skips workspaces in `MANUAL_CONTROL` state entirely (operator has taken direct control)

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
| 2026-04-09 | Added mode-aware behavior: manual-mode skips polling, inserts approval gates after ANALYSIS/QA/PR_REVIEW, skips advancing AWAITING_APPROVAL workspaces. New `set_mode_handler` setter and `_should_approval_gate` check. |
| 2026-04-09 | Instrumented with optional event_bus: emits daemon_started, poll_cycle, workspace_created, agent_dispatched, agent_completed, agent_failed, approval_requested, stage_transition, escalation_sent, and pr_created events. |
| 2026-04-12 | Added explicit MANUAL_CONTROL skip in advance_workspace: orchestrator no longer advances workspaces under operator direct control. |
| 2026-04-14 | Narrowed terminal-state filter to `{DONE, ARCHIVED}`: FAILED is now retained in the active list so it can be retried or manually recovered. DEFERRED (added in same release) is likewise active. |
| 2026-04-14 | Split agent failure routing: quota (`result.failure_kind == "quota"`) now routes to DEFERRED with `retry_at` and iteration rollback; permanent failures continue to FAILED. Added `_quota_window_end` in-memory debounce so multiple concurrent quota hits produce a single Telegram notification per window. New `_rollback_iteration`, `_notify_deferred`, `_notify_failed` helpers. |
| 2026-04-14 | Quota notification debounce now only marks `_quota_window_end` after a successful Telegram send, so transient send failures don't silence an entire retry window. |
| 2026-04-14 | Added `_sweep_deferred`: poll_cycle now resumes DEFERRED workspaces whose `retry_at` has elapsed (transitioning to `previous_state`), and clears the in-memory `_quota_window_end` debounce once its window has passed. Emits `deferred_resumed` events. |
| 2026-04-15 | Added `stage_verifier` module: mechanical post-stage verification captures git HEAD before each verifiable stage and transitions workspace to BLOCKED if HEAD is unchanged after the agent finishes. Prevents silent commit failures (e.g. missing git identity) from going undetected. |
| 2026-04-15 | Extended `stage_verifier` with verifiers for `scope_check` (checks scope-guard-agent-output.md), `qa` (checks qa-agent-output.md), `push` (git ls-remote confirms branch pushed), and `pr_review` (workspace state has pr_number). |
| 2026-04-15 | Wired `stage_verifier` into `_handle_agent_stage`: captures git HEAD before each stage, calls `verify` after successful agent run, transitions to BLOCKED and emits `stage_verification_failed` on failure. |
| 2026-04-16 | Added hot-reload: `config_dir` and `on_project_added` kwargs; `set_tracker`, `rescan_projects`, `_rescan_projects_from_disk` methods; `poll_cycle` calls `_rescan_projects_from_disk` at start so wizard-created projects become live without restart. |
| 2026-04-16 | `_rescan_projects_from_disk` also swallows non-ConfigError exceptions (e.g. PermissionError, OSError) so poll_cycle is not interrupted by unexpected config-dir IO issues; logs at ERROR with stack trace. |
| 2026-04-17 | Wired `stage_verifier` into `_handle_action_stage`: action stages (`push`, `pr_review`, `finalize`) now follow the same capture → execute → verify → transition → emit flow as agent stages. Action methods return `ActionResult` instead of transitioning state internally. Fixes regression where push/pr_review bypassed verification (ACME-14595). |
| 2026-04-21 | PR review comment resolution: VCS `resolve_comment` via GitHub GraphQL, comment classifier with extreme skepticism, auto-fix/reject/escalate flow, TG integration for ambiguous comments, resolution report, review cycle loop. |
| 2026-04-24 | Rewrite PR review flow with `resolution_report` module as single source of truth. Push verification: PENDING entries verified via `git diff` after push, with fail_count tracking and TG warning on 2nd failure. Removed old `_write_resolution_report` function and `comments_to_resolve` field from WorkspaceState. Added `_git_diff_files`, `_git_head_sha`, `_now` helpers to Orchestrator. |
| 2026-04-24 | Added `_build_blocked_reason` helper: reads reports/ba-questions.md for analysis stage or latest *-output.md for other stages, strips boilerplate headers, truncates to 800 chars. Used to surface real escalation context in Telegram notifications. |
| 2026-04-24 | Removed redundant `workspace.state.human_input_question = combined` direct assignment from `_notify_verification_blocked`; `update_state` already sets the attribute. |
| 2026-04-16 | Enriched pipeline log: dev stage completion now includes the short commit SHA (first 8 chars) in the log entry so the log can be correlated with git history. |
| 2026-04-27 | Added `Stage.PAUSED` to the poll-cycle `_SKIP` set so operator-paused workspaces are not advanced. PAUSED tickets stay frozen until manual unpause from the dashboard; `_sweep_deferred` is unchanged and only acts on DEFERRED. |
| 2026-04-27 | `_handle_agent_stage` now captures `current_state` before agent execution and aborts post-agent transitions if the state changed mid-flight (operator paused / took control / etc.). Fixes a race where pausing a ticket while an agent was running would let the agent silently complete and auto-transition the workspace out of PAUSED. |
| 2026-04-27 | Real subprocess kill on agent cancel: `ClaudeCodeAdapter._run_cli` now spawns the `claude` CLI with `start_new_session=True` and reports `proc.pid` to `agent_runtime` via a `pid_callback`. New `AgentRuntime.update_pid` writes the pid into `_running`. `AgentRuntime.cancel` uses `os.killpg(os.getpgid(pid), SIGTERM)` to terminate the whole process tree (CLI plus tools it spawned), with a fallback to `os.kill` on PermissionError. Previously `cancel` was a no-op for Claude Code agents because `pid` was hardcoded to 0. |
| 2026-04-27 | Dashboard ticket card redesign (Task 3): Orchestrator now passes `pt.ticket.summary` (Jira summary string) as the `title` parameter to `_workspace_manager.create()` in `_create_workspace_for_ticket()`. This wires the ticket summary through to `state.json` for display in the dashboard. |
| 2026-04-28 | New `orchestrator.gradle_remediation` module: `looks_like_gradle_cache_corruption(error)` matches the AAPT2 daemon-startup-failed / "Syntax error: \"(\" unexpected" signature; `clear_gradle_transforms()` wipes every `<gradle_home>/caches/*/transforms` dir and returns bytes freed. `Orchestrator._notify_failed` now adds a "🧹 Clear cache & retry" inline button (mapped to `clear_gradle:<ticket>`) when the failure error matches this signature, so an operator can recover from corrupt-binary errors with one click. |
| 2026-04-28 | Broadened `looks_like_gradle_cache_corruption` to catch a second symptom of the same root cause: `AarResourcesCompilerTransform` failures with paths inside `caches/.../transforms/` (e.g. half-extracted `.aar` directories with missing `AndroidManifest.xml`). Same remediation. The dashboard's mirror regex in `board.js` was extended in lockstep. |
| 2026-04-28 | `clear_gradle_transforms` now also stops running Gradle daemons (`pkill -TERM -f org.gradle.launcher.daemon.bootstrap.GradleDaemon`) and wipes `daemon/<version>/registry.bin` before deleting the transforms tree. Without this, a stale daemon kept in-memory references to the (deleted) transforms paths and reused them on the next push, producing an apparent fresh corruption with a misleading 3-second `AarResourcesCompilerTransform` failure. The kill is best-effort: a missing `pkill` or non-zero exit is logged but does not block the cache wipe. Daemon log files are preserved so post-mortem analysis is still possible. |
| 2026-05-01 | Added `orchestrator/constants.py` as single source of truth for all agent report filenames (`REPORT_*` for agent-written structured reports, `RUNTIME_OUTPUT_*` for agent_runtime output files, `STAGE_REPORT_FILE` and `STAGE_RUNTIME_OUTPUT` dicts). Eliminated every hardcoded filename string across orchestrator, stage_verifier, pr_creation, dashboard, command_handler, and all tests. |
| 2026-05-01 | Fixed `_build_blocked_reason` to read the stage-specific runtime output file (e.g. `qa-agent-output.md`) via `STAGE_RUNTIME_OUTPUT` instead of whichever file was most recently modified — prevents scope-guard's PASS output appearing as the QA block reason. Returns a clear "agent produced no output" message when the file is absent. |
| 2026-05-01 | `_handle_escalate` now accepts `is_max_iterations=True`: header reads "Stage: QA — iteration limit reached (2/2)" and hint includes explicit `retry <ticket> from <stage>` instructions. Passed from the iteration-cap check in `advance_workspace`. |
| 2026-05-01 | `push_and_open_pr` action failures now escalate to BLOCKED (with Telegram notification) instead of transitioning silently to FAILED, so the operator is notified and can recover with a retry command. |
| 2026-05-01 | Fixed scope check in `pr_creation.py` to read `scope-guard.md` (agent report) via `REPORT_SCOPE_GUARD` constant instead of the old wrong runtime-output filename. |
| 2026-04-28 | Distinguish aapt2 architecture mismatch (x86-64 binary on a non-x86 host, e.g. an ARM64 Mac VM) from cache corruption. New `looks_like_aapt2_arch_mismatch` matches the canonical path `aapt2-<ver>-linux/aapt2: ... Syntax error: "(" unexpected` (the `-linux` suffix without `_aarch64` is the giveaway). `looks_like_gradle_cache_corruption` now defers to it, so the 🧹 Clear-cache button is suppressed when the cache wipe wouldn't help (the next download produces an equally-incompatible binary). `_notify_failed` sends a distinct message with `ARCH_MISMATCH_HELP` (operator-facing fix options: install qemu-user-static, set `org.gradle.jvmargs=-Dos.arch=aarch64`, or use an x86-64 host). The dashboard mirrors the detection and renders the same help block on the ticket card instead of the truncated raw error. |
| 2026-04-29 | `_squash_feature_commits` is now atomic + uses repo-config author. Previously: ran `git reset --soft HEAD~N` then `git commit -m ...` without explicit author. If the global gitconfig was missing `user.email` (or any other reason commit failed), git refused to record the author and the function caught the exception, logged a warning, and returned — leaving the branch reset but with no squashed commit. Result: the feature branch had 0 commits ahead of develop and the next push opened an empty PR ("422: No commits between develop and feature/..."). Fix: capture HEAD before reset, pass `commit_author_name` / `commit_author_email` from `repo_config.git` via `git -c user.email=... -c user.name=...`, and rollback with `git reset --hard <old_head>` if the commit step fails. |
| 2026-04-29 | Skip on PR review now routes to `AWAITING_APPROVAL`, not silently to `DONE`. Earlier today's "Skip = advance to DONE" change replaced one bug (30-min nag loop on skipped comments) with another: a ticket could be marked DONE while review comments were still open on the PR, which hides incomplete work. The new flow: `_execute_review_decisions` returns `next_state=AWAITING_APPROVAL` when there are skipped comments, and posts one TG message listing them with `[Approve]` `[Reject]` buttons. Approve → DONE (PR_REVIEW → DONE via `APPROVAL_NEXT_STATE`); Reject → FAILED (operator can retry from there). No re-escalation loop, no silent DONE — operator explicitly decides. |
| 2026-04-30 | Defensive `_ensure_branch_has_commits` runs in `_action_push_and_open_pr` before squash + create_pr. If the feature branch has 0 commits ahead of remotes but the index has staged tracked changes, commit them with the repo-config author so the upcoming push has something real to send. This rescues branches damaged by the pre-`ea8819c` squash bug (and any future git step that leaves the branch in a similar orphan state) without operators having to commit by hand. Conservative: only commits what is already in the index — does not run `git add`, so untracked clutter stays out. Emits `branch_recovered_from_orphan_state` event with the file list so each rescue is auditable in events.db. |
| 2026-04-30 | `advance_workspace` no longer auto-resets `stage_iterations[stage_id]` when the counter hits `max_iterations`. The reset zeroed the counter immediately before `should_escalate` was called, so the cap silently became a no-op — escalation was never triggered and a stuck stage looped forever. Removed the reset entirely. Workflows that intentionally loop back into an earlier stage must clear `state.stage_iterations[<stage_id>]` at transition time, otherwise the cap fires on re-entry. |
| 2026-04-30 | `Orchestrator.run` poll loop now cancels and reaps the loser of the `asyncio.wait` race between shutdown_event, wake_event, and the poll-interval timeout. Previously the pending task was abandoned each cycle, leaking one Future per poll for the lifetime of the daemon. |
| 2026-04-30 | `advance_workspace` auto-resume now caps recursion depth at 5. The `AWAITING_APPROVAL → next_state` branch tail-calls `advance_workspace` so the resumed workspace doesn't wait a full poll cycle, but cascading gates could in principle stack-overflow the event loop. Added a `_resume_depth` parameter; once at the cap, the function logs and returns, and the next poll cycle picks the workspace up. |
| 2026-05-01 | Extracted `_refetch_ticket_data(workspace)`: writes ticket.md, comments.md, and history.md on first run; appends a timestamped refresh block / only new comments / only new history lines on reruns. `_create_workspace_for_ticket` now delegates to it. Added `_notify_rerun(workspace, branch, reason)` notification helper for dashboard-triggered reruns. |
| 2026-05-01 | Moved attachment downloading into `_refetch_ticket_data` so reruns also refresh images. Uses the ticket object fetched at the top of the method; skips files already present on disk. The duplicate block in `_create_workspace_for_ticket` was removed. |
| 2026-05-01 | Fixed `strip_markdown` regex to prevent snake_case corruption: underscore italic pattern now uses `(?<!\w)..._([^_\n]+)_...(?!\w)` (non-word-char lookbehind/lookahead) instead of `(?<!_)...(?!_)` (non-underscore only), so `my_private_field` is not corrupted by italic removal. |
| 2026-05-01 | Fixed `_get_ticket_summary` in `pr_creation.py`: was reading `meta/ticket.json` (never written); now parses first line of `meta/ticket.md` in `# TICKET-ID: summary` format. |
| 2026-05-04 | `stage_verifier.verify()` and `_verify_dev()` now accept an optional `duration_seconds` parameter. When the dev-agent exits in under 60 seconds with no commit, the BLOCKED reason is replaced with a specific diagnostic ("completed in Xs … likely could not map plan to code") to distinguish fast-fail exits from normal no-commit failures. |
| 2026-05-04 | `_handle_agent_stage` now threads `result.duration_seconds` into the `stage_verifier.verify()` call, enabling the fast-exit detection path in `_verify_dev`. |
| 2026-05-08 | Smart retry from BLOCKED/FAILED now routes through `AWAITING_APPROVAL` when `qa.md` exists, instead of jumping straight to `PUSHED` — same approval gate the normal QA flow uses. `Workspace.transition` clears `human_input_reply` when entering `PR_REVIEW`, so a stale "proceed"/"reviewed" string from a prior run can't bypass the wait. PR-review escalation also clears `human_input_reply` when posting the message. `VALID_TRANSITIONS` extended: BLOCKED and FAILED can now reach `AWAITING_APPROVAL` (required for the smart-retry routing). |
| 2026-05-08 | `workflows/default-workflow.yaml` `dev` stage gains `max_iterations: 3` / `on_max_iterations: "escalate"`. Bounces from `scope_check` and `qa` re-enter `dev` and previously had no cap, so fix → bounce → fix loops could burn unbounded budget. `tests/unit/test_workflow_router.py` no-cap assertion moved off `dev` (now capped) onto `push` (still uncapped). |
| 2026-05-09 | `_refetch_ticket_data` attachment download now keeps text (crash logs, JSON, source) alongside images, not just images — Jira often serves logs as `application/octet-stream`, so the filter falls back to a text-extension allowlist (`.txt`, `.log`, `.kt`, `.json`, `.stacktrace`, …). Video/audio still rejected; downloads capped at 1 MB. `_ticket_to_markdown` gained an `## Attachments` section so agents see the file list even when content is too large to inline. `agent_runtime.assemble_prompt` now recurses one level into `meta/attachments/`, applying the same per-file 5 KB and total 100 KB context budgets — binaries (images) raise `UnicodeDecodeError` on `read_text` and are silently skipped. |

## Tracker port deleakage (2026-05-11)

`_refetch_ticket_data` no longer reaches into `tracker._request(...)`; it now uses the public `tracker.get_comments` and `tracker.get_status_history` methods, returning `TicketComment` and `StatusChange` dataclasses. The deliberate side effect: newly-created `comments.md` files are headed `# Ticket Comments` instead of `# Jira Comments` (existing files keep their heading).

- Attachment download now goes through `tracker.download_attachment` instead of raw `httpx` + `tracker._email/_token`.




















































