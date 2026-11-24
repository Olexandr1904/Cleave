# Reviews board: why a merge is blocked, and Re-run CI — design

Date: 2026-11-24. Small change. The operator asked for it ("да, делай 1 и 2") after a **Merge…** on a stacked PR
answered only `GitHub refused the merge (GitHub 405): checks, reviews or conflicts block it.`

## What happened

The PR was stacked on another PR's branch. While its base was that branch, no CI ran on it: the repo's workflows run
for PRs into the long-lived branches only. When the parent PR merged, GitHub retargeted the PR to the default branch,
which starts no workflow either. The default branch's rules wait for code-scanning results, so GitHub blocked the
merge (405). The board kept naming `merge`: the repo's status command makes CI no turn on purpose. The operator then
closed and reopened the PR (that started the build but not code scanning) and pushed an empty commit (that started
everything).

## Intent

1. A refused merge says what blocks it, in Cleave's own words, so the operator knows the next move.
2. When GitHub blocks the merge of my PR and no check ran on its head, the card offers **Re-run CI**: one press pushes
   an empty commit to the PR's branch, which starts every workflow, as the operator did by hand.

Non-goals: the board does not stop naming `merge` while checks run (the status command's rule stays); Cleave never
pushes the empty commit on its own; nothing changes in Telegram.

## Approaches

- **Read GitHub's merge state (chosen).** One GraphQL read of the PR: `mergeStateStatus`, its base, head and the
  head's check rollup. After a 405 it explains the refusal; at each poll, for my rows whose line is `merge`, it tells
  whether to offer Re-run CI. Precise: a stacked PR into a feature branch with no checks is `CLEAN` (nothing blocks
  it, Re-run CI would start nothing), the retargeted one is `BLOCKED`.
- **The status command's `checks` field.** Free (the snapshot has it), but it is this repo's script, not Cleave's
  contract, and "no checks" alone would offer Re-run CI on every stacked PR. Rejected.
- **GitHub's 405 message.** Cleave never shows GitHub's response text (it may echo request data). Rejected.
- **Re-run by closing and reopening the PR.** It did not start code scanning. Rejected.

## Decisions

- `merge_state(number)` reads `mergeStateStatus`, `baseRefName`, `headRefName`, `headRefOid`, `isCrossRepository`,
  `reviewDecision` and the head commit's check rollup (each check run's name, status and conclusion; each status
  context's name and state). A check is `pending` (not completed; a status `PENDING` or `EXPECTED`), `passed`
  (`SUCCESS`, `NEUTRAL`, `SKIPPED`) or `failed` (anything else). No rollup means no check ran.
- The refusal text, first that applies: conflicts (`DIRTY`), behind its base (`BEHIND`), a draft, no check ran,
  checks failed (named), checks running (named), changes requested, an approval missing, GitHub still computing
  (`UNKNOWN`), else a rule of the base that names no check. Check names are cut to 40 characters, control characters
  dropped, at most 5 named (`+N more`). A failed read keeps today's generic text.
- The poll reads the merge state of each row whose turns include my `merge`; a row is `no_checks` when the state
  is `BLOCKED`, the head is the row's, it has no rollup and its branch is in the same repo. A failed read leaves
  the flag false and the poll goes on. A row stored before the field reads false.
- The board: a `merge` card with `no_checks` says `merge: no checks ran, re-run CI` and its main button (card and
  panel) is **Re-run CI**, which runs at once (no dialog) with the head the card shows.
- `pr_move(..., "rerun_ci", head=...)` runs only while the row names `merge` and is `no_checks`, at the head the
  board showed. It reads the merge state again: a head that moved, or checks that now exist, refuse with nothing
  pushed. Then it creates a commit with the head's tree and the head as its only parent, and moves the branch to it
  without force (a branch that moved meanwhile is refused by GitHub: nothing is overwritten). The message is
  `<ticket>: re-run CI (no checks ran on <sha9>)`, or `Re-run CI (no checks ran on <sha9>)` without a ticket key.
  It answers `Pushed an empty commit <sha9> to #<n>: its checks start now; merge once they pass.` and polls soon.
  A second press before the poll is refused by the head check (the live head is the new commit).
- The dashboard's action log logs the move like the other pr-moves.

## Left to the builder

The exact GraphQL, the module the texts live in, test fakes.
