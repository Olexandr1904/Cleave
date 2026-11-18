# Address jobs: review threads on the operator's own PRs — Design

Date: 2026-11-18. Amends `2026-11-12-reviewer-mode-design.md`: its non-goals "Answering threads on the
operator's own PRs" and "any action for the author-side turns" no longer hold for the three turns named
below. Everything else in that spec stands, its anti-goals first of all.

## Decision (operator, 2026-11-18)

When a reviewer leaves comments on the operator's PR, Cleave does the author's work: it **tries to refute
each comment**, **fixes with Opus** what holds, and drafts a reply for every thread. Nothing is pushed and
nothing is posted until the operator says so in Telegram.

## Where the process comes from

The repo, as for reviews. The reference repo has an author-side document (answering threads on my own PR)
with the rules this feature needs: every thread gets a reply; "skepticism both ways" with `file:line`
evidence; one verdict per thread (FIXED, WON'T FIX, FOLLOW-UP, QUESTION); the author resolves only what
was fixed, and only once the fix commit is on the PR branch; replies go out through the threads tool's
`post` with a plan whose FIXED entries carry `fix_sha`. Cleave follows that document and carries no copy
of its rules. A repo names its document in `reviewer.process.address_doc`; a repo without one keeps
today's behaviour (a turn line).

## Goals

1. A new job kind **`address`**: it runs when it is the operator's turn to `answer_threads`,
   `resolve_threads` or `address_review` on a PR they authored.
2. The session is Opus at `xhigh` in the review session's sandbox, plus write access to the PR's worktree
   and nothing else: no forge token, no network, no build tools, no git that moves a ref.
3. The session's changes are kept as **one patch** in the job's copy. The job clone and worktree are
   removed with the job, as today.
4. The result is one Telegram message: a numbered line per thread with its verdict, and the fix's size.
5. **"push"** makes Cleave's code commit the patch on the PR head and push it, plainly, to the PR branch.
   **"post replies N…"** posts the named replies through the repo's replies poster; a reply that reports
   a fix gets the pushed commit's SHA from Cleave, never from the model.
6. **"again: …"** re-runs the job with the operator's instruction; **"diff"** shows the patch;
   **"text N"** shows a reply.

## Non-goals (this version)

- **Building or testing the fix on the host.** The session has no Gradle and no network; CI on the pushed
  commit is the check. The message says "not built or tested here". This departs from the reference
  document's "run the tests the fix touches" and is the main limit of this version.
- Pushing a subset of the fixes: the patch is one unit ("again: leave 2 as it is" re-runs).
- Deleting or renaming files in a fix: the session has only the Write and Edit tools.
- Creating follow-up tickets; editing one drafted reply in place; the LLM fallback for replies to an
  address message (exact replies only).
- The other author-side turns: `merge`, `resolve_conflicts`, `request_review`, `finish_draft` stay lines.
- A PR whose branch lives in a fork: its replies work, "push" refuses.

## Anti-goals (it fails even if it "works" when…)

- a commit reaches GitHub that the operator did not ask for with "push", or reaches a branch whose head is
  not the head the job ran on;
- Cleave ever force-pushes, or pushes to the repo's base branch;
- a fix is pushed to a branch a ticket workspace still holds (the ticket pipeline force-pushes it);
- the session reaches the forge token, the network, or a write outside its run directory, the replies
  plan's directory and the worktree;
- a `fix_sha` the model wrote reaches the poster;
- Cleave's own git runs under a git config or a worktree `.git` pointer the session changed.

## The job

**Trigger** (in `plan_jobs`, per board row): the repo has `process.address_doc`; `"author"` is in the
row's roles; no ticket workspace holds the PR (the row's `pipeline` flag is not set, see "PRs the ticket
pipeline opened"); the operator's turns include one of the three actions; reviews are running; the row's
thread turns have not changed for `quiet_minutes`. The job's key is
`<head>:<sorted thread ids of those turns>`, as for a re-check.

**No new job while one covers the turn.** Nothing is queued while a job of this PR at this head is
pending (queued, running, deferred), or is done or posted and still covers the turn: every thread of the
turn is one of that job's threads whose reply has not landed. So posting some of the replies, or pushing,
queues nothing and keeps the unpushed fix; a thread that comes back after its reply landed, a new thread,
or a new head does queue a job, which supersedes the PR's older ones (queued, deferred, done, posted).
Two failures at one key stop automatic retries, as for a review. A second "changes requested" review that
adds no thread is not seen (its turn names no thread): the operator replies "again".

When none of the three actions is left, a queued or deferred job is cancelled and a done one goes stale
("turn gone"); a turn that comes back makes the newest stale job current again, as today. With a job kind
for them, these three actions send no turn line in such a repo.

**Prepare** is the existing one (job clone at the base, worktree at the head, `threads.json`, …). It also
fails the job when `address_doc` is not on the base branch.

**Session.** Prompt: follow `address_doc` for this PR; the threads are in `threads.json`; the PR branch is
checked out at the worktree — make every fix there and commit nothing; nothing can be built or tested;
instead of showing or posting, write the replies plan and `tg_lines.json`. The operator's instruction from
"again: …" is appended as the operator's own words.

**Replies plan** — the re-check's file and shape, plus `fixed`:

```json
[{"n": 1, "thread_id": "PRRT_…", "body": "Fixed in {fix_sha}: null check added.", "resolve": true, "fixed": true},
 {"n": 2, "thread_id": "PRRT_…", "body": "This is intended: …", "resolve": false},
 {"n": 3, "review_id": 3456789, "body": "> Please split this.\n\nSplit into …"}]
```

`fixed: true` means the reply reports a fix; its body holds the literal `{fix_sha}` where the SHA goes. An
entry never carries `fix_sha`. A plan that breaks either rule is unreadable and fails the job.
`tg_lines.json` maps each `n` to `VERDICT · path:line · gist`.

**After the session**, in order: the guarded git files of the job clone and the worktree's `.git` pointer
must be byte-identical to before the session (else the job fails, as today for the config); the run
directory is archived; the plan is parsed; Cleave's git stages everything in the worktree and writes
`fix.patch` (binary-safe, no external diff, no textconv, hooks and fsmonitor off, no host config) into
the job's copy with a per-file `+/−` list. No change → no patch. A patch over 1 MB fails the job. Files
matching `process.address_protected` (default `.github/**`) are listed as protected: such a patch is
never pushed.

**A fixed reply with no patch** is valid: the fix is already on the branch (an earlier push, the
operator's own commit). Its SHA is the job's head.

## PRs the ticket pipeline opened

Since `2026-11-18-reviewer-owns-prs-design.md` (merged) the ticket pipeline hands its PR over to reviewer
mode and closes the ticket. Such a PR is authored with the operator's token, so its threads get address
jobs like any PR of the operator's.

One case is different. The ticket pipeline pushes to a PR it already opened with `--force`
(`orchestrator/pipeline/actions/push_and_open_pr.py`, the "PR already exists" path). While a ticket
workspace still holds the PR, that push would remove a pushed fix. The board row's `pipeline` flag says
exactly this: a workspace that is not done or archived records the PR's number (a fix loop that started
before the handover, a blocked or failed ticket, the one poll cycle between the push and the handover).
While the flag is set:

- no address job is queued for the PR;
- the three actions send their turn line, as in a repo without `address_doc`;
- "push" is refused: `A ticket of the pipeline still holds #<n>'s branch and may overwrite it: nothing
  pushed. Reply "push" again when the ticket is done.`

When the flag drops and the turn is still there, the next poll queues the job. A job that ran before the
flag was set stays as it is; only its push waits.

The two PRs of a ticket that is being compared with the skill flow (`jobs.held_prs`) are left alone
until the verdict, as for reviews: no address job, and their address actions send turn lines.

Accepted: the flag is as fresh as the last board poll, so a ticket brought back between a poll and "push"
is not seen. A rerun from the dashboard is not this case: it clears the workspace's PR number, and the
first push of a branch is not forced (`orchestrator/pr_creation.py`), so it cannot remove a commit.

## The message

```
[acme/app] Threads on your PR #1712 "Fix cart sync"
head 8f01a2b3c · 4 drafted replies · fix: 3 files, +24 −7 — not built or tested here

1. FIXED · CartRepo.kt:88 · null list from the server now handled
2. WON'T FIX · CartView.kt:40 · the reload is intended, see ACME-1201
3. QUESTION · Sync.kt:12 · which retry policy is meant
4. FIXED · CartRepo.kt:120 · renamed as asked

Reply to this message: "push" · "post replies 2 3" · "text 1" · "diff" · "again: …"
https://github.com/acme-org/acme-app/pull/1712
```

The fix part reads "no code change" without a patch, and names the protected files when there are any.

## Replies to the message (deterministic grammar)

| Reply | What happens |
|---|---|
| `push` («пуш», «запушь», `commit and push`) | The push, below |
| `post replies 1 3` | The existing replies posting. A fixed reply of a job with an unpushed patch is refused: "push first" |
| `text 2` | The reply's target and full text |
| `diff` («дифф», «патч») | The patch, in at most 3 message parts; cut with a note when longer |
| `again` / `again: <instruction>` («заново: …», «переделай: …») | A new address job at the row's head; the old one is superseded. Works on a failed job's line too (any state but superseded or stale) |
| `review again`, `skip` | As on any message |
| anything else | "Not understood" with the list above; no LLM fallback |

`push`, `diff` and `again` on a review or re-check message are refused with one line.

## Push

Cleave's code, under the posting lock and a lease of the review clone:

1. The job is done or posted, has a patch without protected files, and has no landed push; the PR's
   stored board row does not have `pipeline` set.
2. GitHub: the PR is open, its head is the job's head, its branch is in this repo, the branch is not the
   base branch and its name is plain (`[A-Za-z0-9._/-]`, no leading `-`).
3. A `push` post is recorded as under way.
4. In a fresh worktree of the review clone at the head: `git apply --index --binary` the patch, commit as
   the repo's `git.commit_author_*` with the message `fix(<TICKET>): address review comments on #<n>`
   (`fix: …` without a ticket key), then `git push origin <sha>:refs/heads/<branch>` — no force. The
   worktree's directory is removed whatever happened.
5. GitHub is read again: its head must be the new SHA (else the post is `unverified`).
6. The job moves to the new head (`head_sha` and the head part of its key), so the board's next poll
   finds the same job at the same key and queues nothing. The post keeps the old head and the new SHA.
7. Answer: `Pushed <sha9> to #<n>: <commit url>` and the reply that posts the fixed threads.

A restart during a push leaves the post `interrupted`: its line asks to check the PR's commits. If the
commit did land, the board sees a new head, the old job is superseded and a new one finds the code fixed.

## Posting replies of an address job

`Posting.post_replies` with three differences: the prepared threads snapshot is checked against the head
the job was prepared at (the push post's old head when there is one); `{fix_sha}` in a fixed reply's body
becomes the first 9 characters of the job's head, and the written plan entry gets `fix_sha` (full SHA)
from Cleave; a fixed reply of a job with a patch needs a landed push. The model's entries are validated
before that, with "carries fix_sha" still a refusal.

## Config

```yaml
reviewer:
  process:
    address_doc: ".github/pr_comments/400-pr-review-workflow.mdc"   # "" = no address jobs
    address_protected: [".github/**"]
```

Global `reviewer.jobs.address` defaults to `opus`, `xhigh`, 5400 s.

## Dashboard

A PR's detail shows the newest address job's replies with their states (as for a re-check), the fix's
file list, the patch text and the pushed SHA. No button pushes or posts.

## Tests that must exist

1. An author row with `answer_threads` queues an address job after the quiet window, keyed by head and
   threads; not without `address_doc`; not when the operator is not the author; not while a ticket
   workspace holds the PR; not for a PR held for a comparison with the skill flow.
2. The three actions send no turn line where address jobs are on, and do where they are off or a ticket
   workspace holds the PR.
3. A gone address turn cancels a queued job and makes a done one stale.
4. The address session may write the worktree, not its `.git`, not the clone; its prompt names the
   document, the worktree and the instruction.
5. A changed worktree `.git` pointer fails the job.
6. `capture_fix` on a real git worktree: a modified and a new file give a patch that `git apply --index`
   reproduces; no change gives no patch; a protected path is listed; hooks planted in the clone do not run.
7. A plan with `fix_sha`, or `fixed` without `{fix_sha}`, fails the job.
8. Grammar: `push`, `diff`, `again`, `again: …` and their Russian/Ukrainian words; refused on review and
   re-check messages.
9. `push_patch` on a real origin: the branch gets one commit with the patch and the configured author; a
   moved head and a non-fast-forward are refused; nothing is ever pushed with `--force`.
10. `push_fix`: refusals (no patch, protected, fork, base branch, odd branch name, already pushed, head
    moved, a ticket workspace holds the PR); success records the post, moves the job to the new head, and a board poll at the new head
    queues nothing.
11. `post replies` on an address job: fixed-before-push refused; after the push the written plan has
    `fix_sha` and the SHA in the body; a model-written `fix_sha` is refused.
12. `again: …` supersedes the old job and the new session's prompt carries the instruction.
