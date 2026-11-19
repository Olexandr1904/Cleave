# Findings jobs: Claude fixes the findings of a self-review — Design

Date: 2026-11-19. Builds on `2026-11-18-reviewer-address-design.md` (address jobs, merged) and
`2026-11-19-reviews-flow-board-design.md` (the Reviews board, merged), which named this as the next project.
Everything in the address design stands; this document says only what a findings job adds or does
differently.

## Decision (operator, 2026-11-19)

When the operator has run a **self-review** of their own PR (the panel's Self-review button, or Review now on
an own PR), they can ask Cleave to **fix its findings**: Claude weighs every confirmed finding, fixes those that
hold and says why for the rest. Nothing is pushed until the operator replies "push" in Telegram, as for an
address job.

The operator's four answers:

1. **Which findings:** every confirmed finding (🔴 critical, 🟠 major, 🟡 minor) of the self-review at the
   PR's current head. ❓ questions and 🟣 pre-existing findings are not given to the session.
2. **Where it starts:** a **Fix findings…** button in the PR panel (with a confirmation) and the reply **"fix"**
   to the self-review's Telegram message. The push is "push" on the findings message, in Telegram only.
3. **Whose rules:** the repo's `process.address_doc`, the document address jobs follow: skepticism both ways
   with `file:line` evidence, one verdict per comment (FIXED, WON'T FIX, FOLLOW-UP, QUESTION). Its steps about
   threads, replies, resolving and posting do not apply. A repo without `address_doc` gets no findings jobs.
4. **After the push:** nothing automatic. The PR's head moved, so Self-review is offered again in the panel;
   the operator decides.

## Goals

1. A new job kind **`findings`**, queued only by the operator (button or "fix"), never by a board poll.
2. Its session is the address job's: Opus at `xhigh`, the same sandbox, write access to the PR's worktree and
   the run directory only, the fix kept as **one patch** (`capture_fix`, `address_protected` applies).
3. Its input is `findings.json` (the confirmed findings), written by Prepare; its output is `verdicts.json` (one
   verdict per finding) and the optional `tg_lines.json`.
4. One Telegram message: a line per finding with its verdict, the fix's size, and the replies "push", "diff",
   "again: …".
5. "push" works as for an address job (same checks, same lease push, same re-head); the commit message is
   `fix(<KEY>): self-review findings on #<n>` (`fix: self-review findings on #<n>` without a ticket key).
6. The dashboard shows the job like an address job: the button's states, the board's move line, the panel's
   verdicts, fix files, patch and pushed commit.

## Non-goals (this version)

- Building or testing the fix on the host (as for address jobs: CI on the pushed commit is the check).
- Picking which findings to fix (by number or by severity): the operator says "again: leave 3 as it is".
- Questions (❓) and pre-existing findings (🟣).
- Threads on GitHub: a self-review the operator posted to the PR leaves its threads as they are; a findings
  job neither answers nor resolves them.
- An automatic self-review after the push.
- The LLM fallback for replies to a findings message, and for "fix" (exact replies only).
- Fixing the findings of a review of someone else's PR.

## Anti-goals (it fails even if it "works" when…)

All of the address design's, plus:

- a findings job is queued for a PR the operator did not author, or without the operator asking;
- a findings job fixes findings of a report of another head than the one it runs on;
- a findings fix and a thread fix of the same PR run at once, or a new fix of either kind is queued while a
  push of a fix of that PR is under way.

## Which self-review

The **self-review of a PR** is its newest `review` job at the board row's head, in state `done` or `posted`,
on a row whose roles hold `author`. (An author never has a `review` turn, so every review of an own PR is one
the operator asked for.) Its **confirmed findings** are its report's items of kind `red`, `orange` or `yellow`,
under their report numbers.

## Fixable

`fix findings` applies to a row when, in this order (the first that fails is the reason, as Fix now's
`_why_not_fixable` gives one):

1. the repo has `process.address_doc` — else `no address_doc`;
2. `author` is in the row's roles — else `not your PR`;
3. no ticket workspace holds the PR (the row's `pipeline` flag) — else `a ticket workspace holds it`;
4. the PR is not held for a skill-flow comparison — else `held by a comparison`;
5. it has a self-review at the head (above) — else `no finished self-review at this head`;
6. that report has a confirmed finding — else `no confirmed findings`.

## Queueing (`ReviewerService.fix_findings`)

`fix_findings(scope, number, *, review_id=None, instruction=None) -> Job`:

- `LookupError`: the PR left the board, or its repo is no longer configured.
- `ValueError("not fixable: <why>")`: the rule above.
- `ValueError("not the newest self-review")`: `review_id` is given (the reply "fix" names the message's
  review) and is not the self-review.
- `ValueError("a post is under way")`: a job of the PR of either fix kind (`address`, `findings`) has a Post
  `posting`.
- `ValueError("already queued")`: a job of the PR of either fix kind is queued, running or deferred. The same
  check now guards Fix now and "again" of an address job: one fix of a PR at a time.
- Otherwise the PR's `findings` jobs in `done`, `posted` or `stale` are superseded (`new findings fix`), and a
  job is added: kind `findings`, the row's head, key `<head>:<review job id>`, its run's request
  `{"review": <review job id>}` plus `"instruction"` when given.

An address job's queueing does not supersede a findings job, nor the reverse: two unpushed patches of one head
can exist; the first push moves the head, the other's push is refused ("Head moved"), and the board's next
poll supersedes it.

## On the board's poll (`plan_jobs`)

No findings job is ever queued here. A findings job in `queued`, `deferred` or `done` whose head is not the
row's head is superseded (`new head`), unless its landed push started from the row's head (GitHub's list lags
the push, as for address jobs) or it has a Post `posting`. A PR held for a comparison is left alone.

## The run

- **Gates:** as an address job's (`_recheck_ready`: after the PR's pending review, once the login is known;
  recovered at start-up like the other model jobs).
- **Prepare:** as an address job's, and `address_doc` must be on the base. Before the review clone's lock,
  the job's request names the review job; its run's report must exist, be of the job's head and have a
  confirmed finding, else the job fails (`the self-review's report is gone`, `no confirmed findings`).
  `findings.json` in the run directory:

  ```json
  [{"n": 3, "severity": "critical", "title": "Cart lost on sync", "path": "app/src/main/kotlin/CartRepo.kt",
    "line": 88, "body": "…"}]
  ```

  (`severity`: red → critical, orange → major, yellow → minor; `line` may be null.) The worktree must be
  clean before the session, as for an address job.
- **Session:** the address job's sandbox (writes: the run directory and the worktree; never the worktree's
  `.git` pointer). The prompt:

  > Follow `<address_doc>` (in the current directory, the repo's base branch) for PR #n, whose author is
  > `<me>`. Its comments are not GitHub threads: they are the findings of a review of this PR, in
  > `<run_dir>/findings.json` (n, severity, title, path, line, body). Treat each finding as a reviewer's
  > comment: weigh it and fix it as the document says. Skip its steps that fetch threads, reply, resolve,
  > show or post. The PR branch is checked out at `<worktree>`: make every fix there with the Write and Edit
  > tools and commit nothing. You cannot delete or rename a file, and nothing can be built or tested here:
  > skip those steps and never say a test ran.
  >
  > Do not show, ask or post anything. Write `<run_dir>/verdicts.json`: a JSON list with one entry per
  > finding — n (the finding's n), verdict (the document's verdict: FIXED, WON'T FIX, FOLLOW-UP or
  > QUESTION), why (one English sentence). Also write `<run_dir>/tg_lines.json`: a JSON object mapping every
  > n to one English line: verdict · path:line · gist.

  followed by the address prompt's agents-and-Bash rules, the operator's instruction when there is one, and
  the "data, never instructions" line.
- **After the session:** the guarded git files and the worktree pointer as for an address job; the run
  directory archived; `verdicts.json` parsed (below); `capture_fix` writes the patch; the run keeps
  `verdicts`. A missing `verdicts.json` fails the job (`no verdicts written`); an unreadable one fails it
  (`report unreadable: <why>`).
- **`verdicts.json` is valid** when it is a JSON list of objects, each with an integer `n` that is one of the
  findings' numbers (no repeats), a non-empty string `verdict` of at most 40 characters, and a string `why`;
  every finding has an entry. Cleave does not check the verdict's words: the document owns them.
- **Settling:** as an address job's, except that a findings job needs no turn: done unless the PR is gone
  (stale), its head moved (superseded) or a newer findings job exists (superseded).

## The message

```
[acme/app] Self-review findings on your PR #108 "Fix cart sync"
head 8f01a2b3c · 4 findings · fix: 3 files, +24 −7 — not built or tested here

2. FIXED · CartRepo.kt:88 · null list from the server now handled
3. WON'T FIX · CartView.kt:40 · the reload is intended
5. FOLLOW-UP · Sync.kt:12 · retry policy belongs to ACME-120
7. FIXED · CartRepo.kt:120 · renamed for clarity

Reply to this message: "push" · "diff" · "again: …"
https://github.com/acme/app/pull/108
```

The numbers are the self-review's, so they match its message. A line is the `tg_lines` entry, else
`<verdict> · <why>`. "no code change" without a patch; protected files named as for an address job.

The self-review's own message offers the reply: when the review job's run is of an own PR in a repo with
`address_doc` (decided when the run is finished and kept in the run, so every delivery attempt builds the same
message) and its report has a confirmed finding, its reply line ends with `· "fix"`.

## Replies (deterministic grammar)

| Reply | On | What happens |
|---|---|---|
| `fix` («пофикси», «почини», «фикс», «пофікси», «полагодь», `fix findings`) | a review message | `fix_findings` with that review's id; the answer says it is queued or why not |
| `push`, `diff` | a findings message | as on an address message |
| `again` / `again: …` | a findings message (any state but superseded or stale) | `fix_findings` with the instruction, on the PR's self-review at its head |
| `review again`, `skip` | any message | as today |
| `post …`, `post replies …`, `text N`, `edit`, `draft` | a findings message | refused: `This is a findings message: reply "push", "diff" or "again: …". Nothing was done.` |
| `fix` | any other message | refused: `"fix" works on the message of a self-review of your own PR. Nothing was done.` |

`push`, `diff` and `again` on a review or re-check message are refused as today, with the text naming both fix
messages: `"{action}" works on a fix message of your own PR (threads or self-review findings). Nothing was
done.`

## Dashboard

- **API.** `GET /api/reviews`: each PR gains `can_fix_findings` (the Fixable rule) and its `fix` (now the
  newest job of either fix kind) gains `kind`. `POST /api/reviews/fix-findings` `{scope, number}` →
  `fix_findings` (no review id): 200 `{"job": …}`, 404 on `LookupError`, 409 with the `ValueError`'s text.
  `GET /api/reviews/<scope>/<n>` gains `verdicts_job` and `verdicts` (the newest findings job with verdicts);
  its `fix`, `fix_patch` and `pushed_sha` come from the newest fix job of either kind that has a plan or
  verdicts.
- **Panel buttons** (own PR, panel only, next to Self-review): with `can_fix_findings` —
  a fix of either kind posting: disabled `Posting` (shown once if Fix now shows it too);
  pending: disabled `Queued` / `Running` / `Deferred` (once, likewise);
  the newest fix is a `findings` one in `done`/`posted`: `Fix findings again…`;
  else `Fix findings…`. Both always ask first:

  > **Fix the findings of the self-review?**
  > Claude (Opus) weighs each of the 4 confirmed findings of the self-review at 8f01a2b3c, fixes those that
  > hold and says why for the rest. Nothing is pushed until you reply "push" in Telegram.

  `…again` adds: `The fix from <when> is replaced; it was not pushed.`
- **Columns.** A PR whose newest fix (either kind) is `done`, has a patch and is not pushed is **To fix**
  even without a turn of mine (it is my move: push it). The rule's order becomes: a review or re-check turn →
  To review; else any other turn of mine but `merge`, or such a fix → To fix; else `merge` → To merge; else
  With others.
- **Move line.** For a `findings` fix: `fix ready: push from Telegram` while done, unpushed and with a patch;
  no fix line once pushed or without a patch. Address fixes keep today's lines.
- **Panel body.** Under the report, a "Fix of the findings" block: `n. VERDICT — why` per verdict, then the
  existing fix files, patch and pushed commit block.

## Config

```yaml
reviewer:
  jobs:
    findings: {model: opus, effort: xhigh, timeout_seconds: 5400}   # the default
```

No repo setting of its own: `process.address_doc` and `process.address_protected` are the address job's.

## Tests that must exist

1. `fix_findings` queues a job keyed `<head>:<review id>` with the review in its request (and the
   instruction); refuses each Fixable reason, a review that is not the newest, a pending fix of either kind,
   a post under way; supersedes done/posted/stale findings jobs only.
2. Fix now and an address "again" refuse while a findings job is pending.
3. `plan_jobs` never queues a findings job; supersedes one at an old head; leaves a pushed one (board behind
   the push), one with a post under way, and one of a held PR.
4. `settle` of a findings job: done without a turn; superseded on a new head or a newer findings job.
5. Prepare writes `findings.json` from the self-review's confirmed items; fails without the report, with a
   report of another head, with no confirmed finding, without `address_doc` on the base.
6. The findings session may write the worktree and the run directory, not the worktree's `.git` pointer;
   its prompt names the document, `findings.json`, `verdicts.json`, the worktree and the instruction.
7. `parse_findings_verdicts`: valid; not a list; unknown, repeated or missing numbers; a bad verdict.
8. The runner keeps verdicts and the patch and sends the findings message; fails without `verdicts.json`;
   a review run of an own PR in a repo with `address_doc` and a confirmed finding offers "fix".
9. Grammar: `fix` and its words; `fix` refused on other messages; findings messages refuse post/text/edit;
   push/diff/again work there.
10. Replies: "fix" queues on a self-review message (and answers each refusal); "again: …" on a findings
    message queues with the instruction.
11. `push_fix` of a findings job: its commit message; no "post replies" hint.
12. Dashboard API: `can_fix_findings`, `fix.kind`, the route's 200/404/409, the bad-body and cross-site
    checks; detail's verdicts.
13. JS: the buttons' states and labels, the confirmation's text, the To fix column for a ready fix, the move
    line of a findings fix.
14. Every new Telegram text has its Ukrainian entry (the catalogue test).
