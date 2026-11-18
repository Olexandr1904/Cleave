# Reviewer mode owns every PR — Design

Date: 2026-11-18. Amends `2026-11-12-reviewer-mode-design.md` ("The board", the bullet on the ticket
pipeline's own PRs, and test 15).

## Decision (operator, 2026-11-18)

In a repo where reviewer mode is on, **every** PR belongs to reviewer mode, the PRs the ticket pipeline
opened included. The ticket pipeline's own PR flow (the `pr_review` stage: wait for "Review Complete",
classify comments, escalate, loop back to `dev`) runs **only in a repo where reviewer mode is off**.

Before this, reviewer mode skipped a PR while a ticket workspace had it (`row.pipeline`): no job, no turn
line. The two modes could not both act on one PR, but a pipeline PR was handled by the weaker flow.

## Terms

- **Reviewer mode is on for a repo** — the daemon built a reviewer context for it at start
  (`main._reviewer_contexts`): `reviewer.enabled` in its repo config, a GitHub repo, a forge token, not a
  dry run. The set is fixed for the life of the process (a repo added by hot reload waits for a restart,
  as today).
- The **reviews running / paused switch does not count**. A paused repo is still reviewer mode's: its PRs
  wait, the ticket pipeline does not pick them back up.

## Behaviour

### Ticket pipeline (repo with reviewer mode on)

1. `push` works as today: the branch is pushed, the PR is opened, the workspace goes `PUSHED → PR_REVIEW`.
2. The "PR opened … tap Review Complete" message is **not** sent.
3. On the workspace's next advance the `pr_review` stage **hands the PR over** instead of running
   `fetch_pr_comments`: it clears `pending_review_comments` and `human_input_reply`, writes one line to
   the ticket's `pipeline-log.md`, and returns `DONE`. Nothing is read from or written to GitHub.
4. `DONE` runs the existing `on_ticket_done`: the tracker ticket moves to its in-review status, and one
   Telegram message goes out with the handover text (below) instead of "Pipeline complete. PR ready for
   merge".
5. A workspace that was already in `PR_REVIEW` when the daemon started with this change (waiting for
   "Review Complete", or holding escalated comments without a decision) is handed over the same way on
   its next advance. Its undecided escalations are dropped; the count goes into the log line.

Handover text (English template, through `tr()`):

```
PR opened: {pr_url}

Reviewer mode takes it from here: its turns and review threads come to the reviewer chat.

Ticket moved to review status.
```

### Ticket pipeline (repo with reviewer mode off)

Unchanged, to the letter.

### Reviewer mode

1. `plan_jobs` no longer skips a row with `pipeline` set: it gets review and re-check jobs and turn lines
   like any other PR of the operator's.
2. The dashboard puts such a row under "My turn" when it has a turn of the operator's. The `pipeline`
   flag stays as a label only; it means what it meant: a ticket workspace known to the orchestrator
   records this PR number.

## What does not change

- The pipeline's PR flow code stays: repos without reviewer mode use it.
- The workflow YAML, the state machine's transitions, the `reviewed` / `unanswered` Telegram commands.
- Reviewer mode still merges nothing.

## Known gap (accepted)

A pipeline PR is authored by the operator's token, so its turns are author-side (`answer_threads`,
`address_review`, `merge`, …). Until the address jobs of `2026-11-18-reviewer-address-design.md` ship and
the repo sets `process.address_doc`, a review comment on a pipeline PR in a reviewer-mode repo produces a
turn line only: nothing classifies or fixes it automatically.

Closed where `process.address_doc` is set: see `2026-11-18-reviewer-address-design.md`, "PRs the ticket pipeline opened".

## Tests that must exist

1. A row with `pipeline` set and a `review` turn queues a review; with an author-side turn it sends a turn
   line.
2. The dashboard lists a pipeline row with a turn of the operator's under "My turn".
3. `pr_review` in a reviewer-mode repo returns `DONE` without calling `fetch_pr_comments`, and clears the
   pending escalations.
4. `pr_review` in any other repo calls `fetch_pr_comments` as before.
5. After `push` in a reviewer-mode repo no "Review Complete" message is sent; in any other repo it is.
6. `on_ticket_done` sends the handover text when the PR was handed over, the old text otherwise.
7. `main` gives the orchestrator exactly the scopes of the reviewer contexts (none in a dry run).
