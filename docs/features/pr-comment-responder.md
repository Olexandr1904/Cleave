# PR Comment Responder

Agent that classifies and triages PR review comments. Operates with extreme skepticism — only comments that identify genuine bugs or violations trigger code changes. Categories: fix_required, explanation, out_of_scope, arch_violation.

## Key Decisions
- Read-only tools only (no write_file, no run_command)
- Output: `reports/pr-comments.md` with classified comments
- Extreme skepticism ported from existing PR review workflow rules

## Resolution Report

`orchestrator/resolution_report.py` — single source of truth for PR comment decisions. Each comment gets one permanent entry; decisions persist across review cycles.

- `read_entries(path)` — parse report into `{comment_id: {field: value}}`
- `add_entry(path, ticket_id, pr_number, comment_id, fields)` — append new entry; creates file if missing
- `update_entry(path, comment_id, updates)` — patch fields on an existing entry in place

## References
- Contracts: `docs/agent-contracts.md` (Rivera — PR Comment Responder)
- Agent file: `agents/pr-comment-responder-agent.md`
