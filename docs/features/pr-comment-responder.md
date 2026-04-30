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

## References
- Contracts: `docs/agent-contracts.md` (Rivera — PR Comment Responder)
- Agent file: `agents/pr-comment-responder-agent.md`
