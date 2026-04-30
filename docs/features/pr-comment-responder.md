# PR Comment Responder

Agent that classifies and triages PR review comments. Operates with extreme skepticism — only comments that identify genuine bugs or violations trigger code changes. Categories: fix_required, explanation, out_of_scope, arch_violation.

## Key Decisions
- Read-only tools only (no write_file, no run_command)
- Output: `reports/pr-comments.md` with classified comments
- Extreme skepticism ported from existing PR review workflow rules

## Comment Classifier

`orchestrator/comment_classifier.py` parses the agent's JSON output into `ClassifiedComment` objects.

- `VALID_CLASSIFICATIONS`: `AUTO_FIX`, `AUTO_REJECT`, `ESCALATE` — unknown values default to `ESCALATE`
- `verdict` field: required, exactly `"Valid"` or `"Not valid"` — missing or unknown values default to `"Unsure"` with a warning log
- `operator_hint`: optional free-text feedback from human operators; agent treats as evidence to investigate, not commands to obey

## Resolution Report

`orchestrator/resolution_report.py` — single source of truth for PR comment decisions. Each comment gets one permanent entry; decisions persist across review cycles.

- `read_entries(path)` — parse report into `{comment_id: {field: value}}`
- `add_entry(path, ticket_id, pr_number, comment_id, fields)` — append new entry; creates file if missing
- `update_entry(path, comment_id, updates)` — patch fields on an existing entry in place

## Escalation Message Renderer

`orchestrator/escalation_view.py` — shared renderer for escalated PR comment Telegram messages.

- `build_escalated_comment_message(state, cc, pr_number, ticket_title, *, recall)` — returns `(text, buttons)` tuple
- Accepts `cc` as either a `dict` or an attribute-style object (e.g., `ClassifiedComment`)
- Verdict rendering: `"Valid"` / `"Not valid"` prefix the reason; any other verdict (e.g., `"Unsure"`) renders reason only
- `recall=True` prefixes the message with `🔁 (still pending)` for the recall flow
- Used by both `orchestrator.py` (initial escalation) and `command_handler.py` (recall flow)
- `orchestrator._send_escalated_comment_tg` delegates entirely to this renderer — no inline message construction

## Pending Comment Schema

Each entry in `state.pending_review_comments` contains:
`comment_id`, `msg_ids` (list of TG message IDs), `decision`, `author`, `file`, `line`, `body`, `reason`, `verdict`, `hint_rounds`, `last_hint`, `pending_reinvestigation`

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-30 | `classify_comments` function now accepts `operator_hint` kwarg (keyword-only, default empty string) and threads it to agent runtime via `extra_context`. Allows human operators to provide hints to the responder agent. |
| 2026-04-30 | `command_handler.handle_reply` now matches replies against `msg_ids` list (new schema) with lazy in-place migration for old state entries that still carry the singular `msg_id` key. Helper `_ensure_msg_ids(c)` mutates the dict on first access — no batch migration needed. |
| 2026-04-30 | Added `_classify_reply(text)` module-level helper. Returns `(decision, matched_token, wf_reason)` where decision ∈ `{'fix', 'wont_fix', 'reinvestigate'}`. Fix synonyms (`fix`, `fxi`, `fixx`, `yes`, etc.) require an exact match; won't-fix synonyms (`won't fix`, `wont fix`, `do not fix`, etc.) allow an optional `:` or whitespace separator followed by a reason. Everything else falls through to `reinvestigate`. `skip` is intentionally not in either set. |
| 2026-04-30 | Implemented `_stage_reinvestigation` with 3-round cap. Sets `last_hint` and `pending_reinvestigation=True` on the comment entry, saves workspace state, sends a recognition ack with round number (`round N/3`), emits `pr_comment_reinvestigation_staged` event, and wakes the orchestrator. On cap (≥3 prior rounds) rejects with a message, emits `pr_comment_hint_exhausted`, and skips `_wake_fn`. |
| 2026-04-30 | Added `Orchestrator._reinvestigate_pending` — Phase 1.5 of `_action_fetch_pr_comments`. Walks `pending_review_comments`, calls `classify_comments` with `operator_hint=last_hint` for entries where `pending_reinvestigation=True` and `decision is None`. Updates `verdict`, `reason`, increments `hint_rounds`, clears the flag, re-escalates via `_send_escalated_comment_tg`, appends new `msg_id`, and emits `pr_comment_reinvestigation_completed`. Entries already decided by the operator are skipped. |
| 2026-04-30 | Added `CommandHandler._handle_unanswered` — re-sends all undecided PR comments (`decision is None`) for PR_REVIEW workspaces. Optionally filters to a specific `ticket_id`. Appends new `msg_id` to each comment's `msg_ids` list (so replies to recall messages are matched by `handle_reply`). Calls `ws.save_state()` once per workspace. Emits `pr_comments_unanswered_recalled` event with `via` field (`"command"` by default; Task 13 will set `"button"`). Wired to `unanswered` intent in `handle_message`. |
| 2026-04-30 | Added `unanswered` action to `handle_callback` — sets `_unanswered_via = "button"` and delegates to `_handle_unanswered`, so button-triggered recall emits `via="button"`. Added "Show N unanswered" button to: (1) "N remaining" confirmation in `handle_reply`, (2) "N remaining" confirmation in `handle_callback` pr_fix/pr_wontfix branch, (3) auto-handled orchestrator summary message when escalated comments exist. |
| 2026-04-30 | AUTO_FIX classification and operator FIX decisions now post `"Will fix: <reason>"` on GitHub at decision time (before add_entry/update_entry). Both paths wrapped in try/except so a GitHub failure is logged as a warning and does not abort the pipeline. Resolution is still deferred to post-fix verification. `github_reply: "Posted (will fix)"` recorded in resolution report for both paths. |
| 2026-04-30 | Resolution report audit-trail completeness: all 6 `add_entry`/`update_entry` call sites now write `verdict` field. AUTO_REJECT and WON'T_FIX use separate try/except for reply vs resolve so each failure is recorded independently (`github_reply_status`, `resolved_status` reflect actual outcomes, not unconditional "Posted"). |
| 2026-04-30 | `_reinvestigate_pending` now calls `update_entry` after re-classification to persist `verdict`, `reason`, `hint_round`, and `hint` to the resolution report. Clears `reinvestigation_retry_count` on success. |
| 2026-04-30 | Retry-once policy for re-investigation failures: first exception increments `reinvestigation_retry_count` and leaves `pending_reinvestigation=True` for next tick (no TG surface). Second consecutive failure surfaces to TG and clears both `pending_reinvestigation` and `reinvestigation_retry_count`. |
| 2026-04-30 | `_reinvestigate_pending` clears `pending_reinvestigation` flag when `decision is not None` (operator decided via button while re-investigation was queued), instead of skipping without clearing. |
| 2026-04-30 | Escalated `pending_review_comments` entries now include `ticket_title` field (resolved at creation time via `_get_ticket_title`). |

## References
- Contracts: `docs/agent-contracts.md` (Rivera — PR Comment Responder)
- Agent file: `agents/pr-comment-responder-agent.md`
