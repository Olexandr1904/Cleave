# Design: README rewrite + per-topic user docs

**Created:** 2026-04-30
**Status:** Approved (option C). In implementation.

## Goal

Make Cleave's documentation land for a first-time reader by splitting the front-page README from per-topic reference docs. The current README is 73 lines and mixes "what is this" with "how do I run it"; details (labels, TG commands, dashboard buttons) get truncated to one-liners.

After this change a reader should be able to:

1. Read the README in 60 s and understand what Cleave is, what it can do, and how to start it.
2. Drill into one specific topic (Telegram, dashboard, labels, troubleshooting) without scrolling through 500 lines of unrelated material.
3. Find every shipped feature listed once, in the right doc.

## Approach

**README** — short index (~150 lines). Pitch, walkthrough, Quick Start (using the dashboard wizard), feature list (grouped one-liners, each linking to its dedicated doc), pipeline diagram, link table to all reference docs.

**`docs/dashboard.md`** (new, user-facing) — full dashboard tour. Each view (Board, Ticket Detail, Event Log, Settings, Project Health, Take Control) gets its own subsection. Lists every button and what it does. Cross-links to `docs/features/dashboard.md` (spec).

**`docs/telegram.md`** (new, user-facing) — every command, free-text intent, inline button, and reply pattern. Tables of:
- Free-text intents from `intent_parser.py` (status, analyze, approve, reject, set_mode, retry, provide_input, reviewed, unanswered)
- Inline button callback actions from `escalation_view.py` and `orchestrator.py` (approve, reject, retry, reviewed, pr_fix, pr_wontfix, clear_gradle, unanswered)
- Allowlist behavior, BLOCKED reply unblock, escalation message types

**`docs/labels.md`** (new) — single reference for every Jira label Cleave reads:
- Trigger labels (`jira.trigger_labels`, default `["ai-pipeline"]`, AND semantics)
- Ignore labels (`jira.ignore_labels`)
- Model labels (`model-haiku`, `model-sonnet`, `model-opus`) — case-insensitive on short name, prefix lowercase
- Repo-routing label (`jira_repo_label` on each repo, used in multi-repo projects to route a ticket to one specific repo)
- Where each is configured + behavior on conflicts (multi-model labels, unknown short names → global default + Jira comment)

**`docs/troubleshooting.md`** (new) — health strip, log paths, dry-run, pre-flight health-check command, common error categories, and the explicit "ask an AI" advice the user requested.

## Non-goals

- No screenshots.
- Don't duplicate `docs/features/*` content — those stay as spec/changelog format. New docs are *user-facing* references and cross-link to features for depth.
- No changes to `docs/setup-guide.md`, `deploy/README.md`, or `CONTRIBUTING.md`.

## Cross-doc rules

- Each new user doc starts with one paragraph stating what it covers and what it does *not* (so readers can leave fast if they're in the wrong doc).
- Each new user doc has a "See also" tail linking to relevant feature docs.
- README links to all four new docs in its "Docs" section.

## Open questions

None.
