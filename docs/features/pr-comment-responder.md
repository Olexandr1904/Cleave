# PR Comment Responder

Agent that classifies and triages PR review comments. Operates with extreme skepticism — only comments that identify genuine bugs or violations trigger code changes. Categories: fix_required, explanation, out_of_scope, arch_violation.

## Key Decisions
- Read-only tools only (no write_file, no run_command)
- Output: `reports/pr-comments.md` with classified comments
- Extreme skepticism ported from existing PR review workflow rules

## References
- Contracts: `docs/agent-contracts.md` (Rivera — PR Comment Responder)
- Agent file: `agents/pr-comment-responder-agent.md`
