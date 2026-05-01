# Repository Decision Ledger — Design

**Date:** 2026-04-15
**Status:** Proposed
**Part of:** [Feature Plan](2026-04-15-feature-plan.md)

## Problem

Every ticket starts with total amnesia. The Dev agent working on `PROJ-123` has no idea that ticket `PROJ-118` — on the same repo, three days ago — already tried switching the test runner from `unittest` to `pytest` and the human reviewer explicitly vetoed it. So `PROJ-123` proposes the same migration, gets the same veto, and Cleave pays the tokens plus the human round-trip a second time. Multiply across hundreds of tickets per repo over months and the compounding loss is substantial.

Ticket isolation is deliberate and correct: tickets should not share workspace, scratch files, or transient state. But "repo-level rulings that the humans already made" is not transient state. It's repo knowledge that deserves to live *with the repo*, not inside a ticket's workspace and not inside Cleave's daemon.

## Goal

A single append-only markdown file committed to the target repository that records durable rulings made during previous tickets. Future tickets on that repo read the file before planning and treat each entry as a binding constraint.

## Non-goals

- Cross-repo memory ("reviewer Alice always wants type hints" across every repo) — this would break the repo-local scope boundary and is deferred
- Semantic search, embeddings, or vector stores — a flat file is sufficient for the expected data volume; search can be added later if needed
- A knowledge base for "how the repo works" — that belongs in the repo's own `CLAUDE.md` or `README.md`
- A log of agent scratch reasoning — that stays in the ticket workspace and dies with it
- Retroactive population — the ledger starts empty on each repo and grows only from new tickets forward

## The mechanism

A single file committed to the **target repository** (not to Cleave):

```
<target-repo>/.cleave/decisions.md
```

Append-only. One entry per decision. Example entries:

```markdown
## 2026-04-12 — PROJ-118 — pytest migration rejected
**Decision:** keep `unittest`.
**Scope:** all modules under `src/api/`.
**Why:** reviewer (Alice) wants to defer until Q3 after the CI rework lands.
**Source:** PR #482, review comment by @alice
**Binding until:** superseded by explicit ticket

---

## 2026-04-09 — PROJ-104 — snake_case for all new Python symbols
**Decision:** new symbols use `snake_case`; existing `camelCase` is grandfathered, not refactored.
**Scope:** all `*.py` files in the repo.
**Why:** repo was partially migrated from a legacy codebase; mass-renaming caused merge conflicts last sprint.
**Source:** PR #471, review comment by @bob
**Binding until:** superseded
```

The format is a convention, not a schema. Agents produce it from a prompt template; humans can edit freely.

## Who writes, who reads

### Writers (deliberately small set)

**`Scope Guard`** — writes when it catches a diff that exceeds scope *and the human confirms the scope boundary was intentional*. Entry records "scope X is out of bounds for this repo."

**`PR Comment Responder`** — writes when a human reviewer's comment resolves a recurring ambiguity. The responder's prompt asks: *"Is this comment a one-off answer, or a rule the repo should follow going forward?"* Only rules get logged.

**`Fix agent`** — **deferred.** Writing discipline is the make-or-break factor for this feature, and Fix agent runs the highest volume. Starting with Scope Guard + PR Comment Responder only keeps write volume low and lets us validate discipline before expanding. Revisit Fix after one month of real usage.

### Readers

**`BA agent`** — reads the ledger during ticket analysis. The BA prompt gains a read step: *"Before drafting the ticket brief, read `.cleave/decisions.md` if present. Cite any entry whose scope overlaps the current ticket in the 'Constraints' section of the brief."*

**`Dev agent`** — reads the ledger before planning. The Dev prompt gains: *"Before proposing an implementation plan, read `.cleave/decisions.md` if present. Treat each entry as a binding constraint unless the ticket explicitly supersedes it. If the plan would violate an entry, stop and escalate."*

Other agents (PM, QA, Merge) do not read the ledger — they don't need the information and giving every agent the file invites prompt bloat.

## Why in the target repo, not in Cleave

Three reasons in order of importance:

### 1. Scope boundary matches repo boundary

Decisions about repo X apply only to repo X. Storing them inside the repo means:
- No synchronization problem between Cleave and the repo
- No cross-repo contamination if two projects share agents
- No ambiguity about which set of decisions applies to a given ticket

The repo is the natural unit of ownership for these rulings. Any other location creates a mapping problem that can drift.

### 2. Git is the storage

No new database. No vector store. No separate service. The ledger is:
- Version-controlled alongside the code it governs
- Diff-able and reviewable in PRs (humans can see when a ruling was added and by whom)
- Resilient to Cleave being rebuilt, moved, or reinstalled from scratch
- Accessible to any tool that reads the repo, not just Cleave

### 3. Humans can edit it

A developer can open `.cleave/decisions.md` in their editor and:
- Fix a wrong entry
- Add context the agents missed
- Delete a ruling that no longer applies
- Write an entry manually from the start of the repo's life

If the ledger lived in Cleave's SQLite, it would be opaque, locked to Cleave's tooling, and would rot. Making it a markdown file in the repo means maintenance is a normal PR.

## Writing discipline (the fragile part)

The entire feature fails if agents write entries for every ticket. Three rules enforced in the agent prompts:

### Rule 1 — Only durable rulings, never observations

- **Yes:** *"We chose X over Y because reviewer rejected Y — applies to all future tickets in scope."*
- **No:** *"This file uses pytest imports."* That's reading the repo, not a ruling.

The test: would a future ticket be *wrong* if it didn't know this? If no, don't log it.

### Rule 2 — One paragraph max, with a source reference

Every entry must cite its source (PR number, review comment, commit hash, or ticket ID). No source → no entry. Long entries turn into a log file nobody reads; the cap forces synthesis.

### Rule 3 — Append only; never rewrite or delete existing entries

Agents append. Humans curate. This is a one-way boundary that keeps agents from silently eroding previous rulings. If an agent thinks an entry is wrong, it must escalate, not rewrite.

The prompts include explicit anti-patterns to reject during self-review, modeled on the watchdog's "fail fast on violation" approach.

## Lifecycle and commit flow

- The ledger file is **created lazily** by the first writer. If it doesn't exist when a reader looks, the reader treats it as empty and moves on. No errors, no warnings.
- Writes happen **in the same commit** as the ticket's code change. The agent that made the code change stages `.cleave/decisions.md` alongside its other files. This means:
  - The ledger grows in lockstep with the code
  - No orphan "ledger update" commits that look suspicious in history
  - If the ticket's PR is rejected, the ledger entry is rejected with it
- **Concurrent writes are not a problem.** Tickets are isolated per workspace, so two tickets can never write to the same `.cleave/decisions.md` at the same time. Merge conflicts at the ledger level are handled the same way as any other merge conflict on a shared file — by the human reviewer.

## Cleave-side implementation

Minimal. The bulk of the feature lives in agent prompt changes. Code changes:

- **Helper function** in `integrations/` (e.g., `ledger.py`) with two operations:
  - `read_ledger(repo_path) → str | None` — returns file contents or `None` if missing
  - `append_entry(repo_path, entry_text)` — appends a new entry with a trailing `---` separator, handles file creation
- **Agent runtime hook** that injects the ledger contents into BA and Dev prompts at dispatch time, so the agent sees it as part of its context rather than having to call a tool. Keeps the read path invisible to the agent's reasoning loop.
- **No new config** in `global.yaml`. The feature is either on or off at the prompt level; there's no operator knob.

## What the feature is not

- **Not a knowledge base** — no search, no indexing, no cross-repo queries
- **Not a replacement for `CLAUDE.md` or `README.md`** — those describe *the code*; decisions describe *the rulings*
- **Not a ticket history log** — Jira is that
- **Not an architecture document** — `arch-rules.md` (already protected by `safeguards.py`) is that
- **Not a memory store for scratch reasoning** — scratch reasoning lives and dies with the ticket workspace

## Failure modes

| Failure | Handling |
|---------|----------|
| Agents write too many entries ("observation spam") | Caught in self-review against Rule 1; if it slips, humans delete in a curation PR. Worst case: revert the feature by removing the read step from prompts. |
| Agents write too few entries | Feature is silently useless. Mitigation: weekly review of ledger growth rate; if growth is zero on active repos after two weeks, re-examine writer prompts. |
| An agent rewrites or deletes existing entries | Hard failure. Caught at review time in the PR diff; the offending PR is rejected and the prompt is tightened. Rule 3 should be stated in the strongest possible language in writer prompts. |
| The ledger grows unbounded | Expected behavior for the first year. At ~50 entries per repo, add a "recent 20 entries" windowing at read time; at ~200 entries, consider sectioning by scope. Not a day-one concern. |
| Stale entries (rulings that no longer apply) | Humans curate in a normal PR; agents never delete. Entry format includes "Binding until" field to make currency explicit. |
| Repo doesn't have `.cleave/` directory | `append_entry` creates it on first write. Readers treat missing file as empty. |

## Testing strategy

- **Unit:** `read_ledger` / `append_entry` helpers against a temp directory; cover empty, existing, missing-directory cases
- **Unit:** prompt-injection point receives correct ledger contents
- **Integration:** end-to-end ticket test that runs Scope Guard, verifies an entry is appended, runs a second ticket, verifies BA brief cites the entry
- **Discipline test (manual):** run 10 real tickets on a test repo and audit the resulting ledger for Rule 1 violations. If any entry fails Rule 1, tighten prompts before expanding to production

## Success criteria

- On active repos, at least one BA brief or Dev plan per week cites a ledger entry
- Ledger files grow monotonically; agent-authored entries never overwrite previous entries
- Zero observation-spam entries (verified by manual audit during the first month)
- Humans can successfully edit, correct, and delete entries in a normal PR without breaking reader agents

## Open questions

- Should Scope Guard write entries automatically, or propose them to the human first for confirmation? Recommendation: **propose** for the first month (via Telegram `normal` severity), then relax to automatic once discipline is proven.
- Should the ledger include a YAML frontmatter block for machine parsing (e.g., structured `scope:` field)? Recommendation: **no** — start plain markdown; add structure only if reader agents struggle with natural-language scope descriptions.
- Ledger windowing at read time: start unlimited, add when entries exceed ~50? Recommendation: yes, defer windowing.
