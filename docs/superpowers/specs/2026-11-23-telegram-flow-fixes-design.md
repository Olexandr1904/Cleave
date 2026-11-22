# Telegram flow fixes — Design

Date: 2026-11-23. Source: a week of the operator's Telegram traffic, read from the daemon log's `TG in` / `TG out`
lines. Of 35 typed commands since full logging started, 21 ended in "Nothing was done" or "Not understood"; of 11
button taps, 9 ran (the other 2 were lost to "Query is too old", fixed since).

## Decisions (operator, 2026-11-23)

1. **Buttons over grammar** on a fix message (threads, self-review findings): only the actions that can run.
2. **Protected files**: a fix that touches `reviewer.process.address_protected` is pushed on **"push anyway"**,
   typed or by a button that asks for a confirmation first.
3. **Messages say what happened, whole**: the tracker status line only when the move happened; refusal reasons
   not cut. Tracker transitions never step blindly through the workflow.
4. **Quota is its own class**: no "Retry now" on a usage-limit deferral, one message per limit window, the same
   transient error three times in a row stops being deferred.
5. **/status is a control panel**: per ticket the reason and what Cleave waits for, with buttons; fixed commands
   are parsed without the LLM.
6. **No approval gate after QA**: QA passed → push and open the PR. The plan gate stays.
7. Separating Cleave's development from the live daemon: later.

## Part 0 — Button rows (foundation)

`Button` gains `row: int = 0`. The Telegram adapter lays the buttons out one keyboard row per distinct `row`, in
order of first appearance; buttons that leave `row` at 0 keep today's single row.

## Part A — Reviewer fix messages (decisions 1, 2, part of 3)

### Grammar

- `post all`, `post replies`, `post replies all` (and `опублікуй всі` / `опубликуй все`) on a re-check or address
  message: every drafted reply not yet posted at the head. On a review message they are refused with the
  review message's own hint.
- `push anyway` (`пуш все одно`, `пуш все равно`): the push of a fix with protected files.
- `push and post` (+ `anyway`): push, then post every drafted reply (see "Push and post").

### Buttons

Address message, by its state:

| Fix | Protected | Replies | Buttons |
|---|---|---|---|
| yes | no | yes | `Push + reply all` · `Push` · `Diff` |
| yes | yes | yes | `Push anyway + reply all…` · `Diff` |
| yes | no / yes | no | `Push` or `Push anyway…` · `Diff` |
| no | — | yes | `Reply all` |

A findings message: `Push` (or `Push anyway…`) · `Diff` when it has a fix. A re-check message: `Reply all`.
Callback data is `rv_<verb>:<job id>` (`rv_push`, `rv_pushpost`, `rv_post`, `rv_diff`, `rv_pushx`). A tap runs the
same `_execute` path as the typed command, answered as a reply to the tapped message; the tapped message's anchor
must name the same job, else the tap runs nothing. `rv_pushx` (protected) answers with the existing Yes / No echo
(`add_confirmation`, `rv_ok` / `rv_no`) naming the protected paths; Yes runs `push anyway` + post all.

### No dead end

- A protected fix's line says: `Protected files in the fix (…): Cleave pushes it only on "push anyway".`
- The hint line offers `"push anyway"` where it offered nothing.
- `#n reports a fix that is not pushed` names `"push anyway"` when the fix is protected.

### Push and post

One action: the push under the posting lock; when it lands, a post of every drafted reply not posted at the new
head, as `post_replies` runs it. A push that is refused or fails posts nothing.

### Post after Cleave's own push (bug)

After Cleave pushed a fix, GitHub moves a thread's `line` when the fix inserted lines above it (seen: 971 → 973).
The dry run aims each reply at `path:line` as GitHub shows it now, while `_aims_at` compares with Prepare's snapshot
of the head before the push, so the post was refused ("the reply for #2 is aimed at …"). The aim check uses the
threads command's snapshot at the current head (taken before the dry run) to find a thread's `path:line`; Prepare's
snapshot stays the source of what the plan may post.

### Whole reasons

Refusal reasons are not cut (`step['target'][:80]`, the protected paths). A model-written gist cut at `_MAX_TEXT`
ends with `…`.

## Part B — Tracker transitions and the done message (decision 3)

- `JiraAdapter.transition_ticket(ticket, status)`: a no-op when the ticket is already in `status`; a transition
  whose name or target is `status` is taken; else it steps only through the statuses the project lists in
  `tracker.jira.transition_via` (ordered, default empty), never into a status it already visited. Nothing else is
  ever taken. Today it takes the first transition offered: from an "Open" status that was "Resolve Issue", and one
  ticket went Open → Resolved → Closed → Reopened in two seconds.
- `on_ticket_done` moves the ticket with `transition_ticket` to the configured review status (no keyword
  matching: it matched a "verification" status that means "merged and on stage") and its message says what
  happened: `Tracker: moved to {status}.` or `Tracker: not moved to {status}: {reason}.`
- `in_review` in `tracker.jira.statuses` names whatever status the project wants after the PR opens; a project
  whose workflow has no review status names another (or the in-progress one, which then is a no-op).

## Part C — Quota and repeated transient failures (decision 4)

- `AgentResult.failure_kind`: `"quota"` only for `QuotaExhaustedError`; the CLI errors classified transient get
  `"transient"`, deferred the same way. `notify_deferred` takes the kind instead of matching "usage limit" in the
  text (it missed "You've hit your 5-hour limit").
- A quota deferral's message has no "Retry now". Within one limit window there is one message: a later ticket
  hitting the limit edits it to add its key ("Usage limit until 13:00 UTC. Waiting: ACME-1, ACME-2."). The
  window's tickets resume on their own at `retry_at`.
- The same transient error (its first line) three times in a row at one stage blocks the ticket with that error
  and the usual Retry button, instead of a fourth deferral. Any success at the stage clears the count.

## Part D — /status as a control panel (decision 5)

- `status`, `/status`, `status <key>`, `retry <key>`, `retry <key> from <stage>` (key: full or its number) are
  parsed without the LLM; anything else still goes to the intent parser.
- The summary lists per active ticket: its stage, one line of why it is there (the error, the question, the gate,
  `retry_at`), and what Cleave waits for (your reply to a message, an approval, an automatic retry at hh:mm UTC).
- Each ticket that is BLOCKED, FAILED or DEFERRED gets a button row: `Retry` · `From stage…` · `Skip`.
  `From stage…` answers with the stages as buttons (`retry_from:<key>:<stage>`); `Skip` does what the "skip"
  reply to an escalation does.

## Part E — No approval gate after QA (decision 6)

`APPROVAL_GATE_STATES` is `{PLAN_REVIEW}`: a ticket whose QA passed goes on to push and opens its PR.
