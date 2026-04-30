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
| 2026-04-30 | `command_handler.handle_reply` now matches replies against `msg_ids` list (new schema) with lazy in-place migration for old state entries that still carry the singular `msg_id` key. Helper `_ensure_msg_ids(c)` mutates the dict on first access — no batch migration needed. |
| 2026-04-30 | Added `_classify_reply(text)` module-level helper. Returns `(decision, matched_token, wf_reason)` where decision ∈ `{'fix', 'wont_fix', 'reinvestigate'}`. Fix synonyms (`fix`, `fxi`, `fixx`, `yes`, etc.) require an exact match; won't-fix synonyms (`won't fix`, `wont fix`, `do not fix`, etc.) allow an optional `:` or whitespace separator followed by a reason. Everything else falls through to `reinvestigate`. `skip` is intentionally not in either set. |

## References
- Contracts: `docs/agent-contracts.md` (Rivera — PR Comment Responder)
- Agent file: `agents/pr-comment-responder-agent.md`
