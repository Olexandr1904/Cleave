# Architecture Decisions — 2026-04-08

Consolidated decisions from RFC review session (BA + Architect analysis).

## Source Documents

- **Cleave PRD**: `docs/prd.md`
- **Cleave Architecture**: `docs/architecture.md`
- **RFC (AJDS)**: Provided in conversation (not saved as file yet)
- **Existing helpers**: production-ready scripts under `/opt/cleave-helpers/` (GitHub side) and a parallel GitLab tree.

---

## Adopted from RFC

| # | Decision | Detail |
|---|----------|--------|
| A | **PR Comment Responder** — new agent | Post-push agent that fetches PR/MR comments, classifies (fix_required / explanation), dispatches fixes or replies |
| B | **Explicit tool declarations per agent** | Each agent BMAD spec declares `tools:` — answers how agents execute side effects (via Claude tool_use / function calling) |
| C | **Reopen/diff detection** | `diff_log.json` in ticket meta for change tracking, supports reopen analysis |
| D | **Well-described ticket as explicit LLM contract** | BA agent returns `{ is_well_described, missing_fields }` — not rule-based |
| E | **Telegram threading by ticket_id** | One chat, threaded by ticket ID for better UX |
| F | **Granular state machine** | `NEW → ANALYSIS → DEV → REVIEW → PUSHED → PR_REVIEW → DONE` + `BLOCKED`, `SKIPPED` |

## Adopted from Cleave (kept)

| # | Decision | Detail |
|---|----------|--------|
| G | **Workspace isolation** (git clone per ticket) | Each ticket gets its own source copy. Safe for concurrency. |
| H | **Config cascade** (3-level YAML) | `global.yaml → project.yaml → repo.yaml`. Multi-company support. |
| I | **Daemon model** (asyncio) | Long-running process, configurable poll interval. Not cron. |
| J | **QA Pipeline** | Tests, lint, build gates — missing from RFC |
| K | **Scope Guard** (pre-push) | Diff-vs-plan validation before push |

## Explicit Overrides (differs from both)

| # | Decision | Detail |
|---|----------|--------|
| L | **No Merge Agent** | Merge is human's job. PR stays open. System stops at PR_REVIEW/DONE. |
| M | **Tickets persist forever** | Only `/source/` dir deleted after merge (triggered by CI/CD hook). Ticket meta + reports kept for history. |
| N | **All ticket artifacts in Markdown** | `ticket.md`, `parent.md`, `history.md` — not JSON. AI-friendly and human-readable. |
| O | **Multi-company hierarchy** | Company → Repo → Tickets. Not just multi-project. |
| P | **Both GitHub + GitLab support** | VCS interface with two adapters. Configured per-repo. |
| Q | **Both GitHub Actions + Jenkins CI** | CI interface with two adapters. Configured per-repo. |

---

## Updated Pipeline Flow

```
BA/PM → Developer → Scope Guard (pre-push) → QA (tests/lint/build)
→ Push/PR → [wait] → PR Comment Responder → Done

PR Comment Responder loop:
  fetch comments → classify →
    fix_required → Developer (scoped fix) → re-push → re-fetch
    explanation → reply to comment
    no comments / all resolved → Done
```

---

## Directory Structure

```
/data/
  /<Company>/                        # e.g., Acme, BetaCo
    /<Repo>/                         # e.g., Acme Mobile, BetaApp
      /tickets/
        /<ticket_id>/                # e.g., ACME-14567
          /meta/
            ticket.md                # Jira ticket content (markdown)
            parent.md                # Parent ticket context
            history.md               # Change history
            comments.md              # Jira comments
            diff_log.json            # Reopen/change tracking
          /reports/
            ba.md                    # BA agent report
            pm.md                    # PM agent report
            developer.md             # Dev agent report
            scope-guard.md           # Scope check result
            qa.md                    # QA report
            pr-comments.md           # PR comment analysis
          /state.json                # Pipeline state machine
          /source/                   # Git clone (deleted after merge)
      /rules/                        # Repo-specific arch rules, lint config
```

---

## Existing Helpers Mapping

Production-ready scripts from `/opt/cleave-helpers/`:

| Tool | File | Maps to |
|------|------|---------|
| Jira ticket fetcher | `/f/jira_tickets/fetch_jira_tickets.py` | Jira Fetcher → `meta/ticket.md` |
| Jira status updater | `/f/jira_tickets/update_jira_status.py` | State transitions |
| GitHub PR comments | `/f/pr_comments/fetch_pr_comments.py` | PR Comment Responder (GitHub) |
| GitHub PR resolve | `/f/pr_comments/resolve_pr_comments.py` | PR Comment Responder (GitHub) |
| GitHub CI failures | `/f/ci_failures/fetch_ci_failure.py` | QA Pipeline CI check (GitHub Actions) |
| GitLab MR comments | `/n/review/mr_comments/fetch.py` | PR Comment Responder (GitLab) |
| GitLab MR resolve | `/n/review/mr_comments/resolve.py` | PR Comment Responder (GitLab) |
| GitLab code review | `/n/review/code_review/review.sh` | Pre-push review (optional) |
| GitLab post comments | `/n/review/code_review/post-comments.sh` | Review publisher |
| Jenkins build logs | `/n/review/jenkins/fetch.sh` | QA Pipeline CI check (Jenkins) |
| Jira ticket creation | `/n/review/jira/create_ticket.sh` | Sub-task creation |

**Integration approach**: Wrap as subprocesses, don't rewrite. Read their markdown output.

---

## Agent Execution Model (Decision B detail)

Agents execute via **Claude API with tool_use (function calling)**:
- Runtime defines tools: `read_file`, `write_file`, `run_command`
- Each agent gets a scoped tool set (Dev gets file-write, QA gets test-run, etc.)
- Tools sandboxed to ticket's `/source/` directory
- Existing helper scripts called via `run_command` tool

---

## Deliverable Plan

Sequential, each agent reads previous output:

1. **Winston (Architect)** → `docs/architecture-v2.md` — merged architecture spec
2. **Mary (BA)** → `docs/agent-contracts.md` — BMAD agent contracts
3. **James (PM)** → `docs/implementation-plan-v2.md` — phased implementation plan

---

## Open Questions (to resolve during architecture)

1. How exactly does tool_use sandbox work? Per-agent tool allowlists?
2. PR comment fetch: polling interval or delay-based? Configurable per-repo?
3. LLM retry policy: 3 attempts, exponential backoff (1s → 2s → 4s). Confirm.
4. Parent-child tickets: just include parent.md in context, or deeper handling?
5. CI/CD hook for source cleanup: what format? Webhook from CI? Manual trigger?
