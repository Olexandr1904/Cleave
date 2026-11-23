# Board: a button for every move a card names — design

Date: 2026-11-24. Small change on top of `2026-11-23-board-main-action-design.md`. Approved in conversation by the
operator (the table of moves and option A for the reviewer pick); the calls below marked *my call* were made while
the operator was away and are open to change.

## Intent

The operator wants to run every move from the board: a card whose bottom line names a move ("request review",
"merge", "finish draft", "answer threads", "review") gets a main button that makes it. Before this change only the
three moves Cleave itself makes (push a fix, post replies, post a review) had one, so a card that said
"request review" had no button at all, and pressing other buttons often looked like nothing happened.

## What the operator saw (the log check)

- Only one press reached the daemon that evening: Fix again on a PR whose base is another PR's branch. The address job
  refuses such a PR at Prepare ("PR base is …, not <base>") within seconds, so the card looked the same as before.
- Cards whose line was "request review" had no buttons: the line was text.
- The answers of Review now and Fix now went to a status line at the top of the page for 6–10 s, not to the card.
- The daemon logged no dashboard action, so a press could not be traced afterwards.

## Moves and their buttons

| Card line (my turn) | Main button | What it does |
|---|---|---|
| review, re-check | Review now (the card's existing button) | queues a review, as today |
| answer threads, resolve threads, address review (my PR, Fix now applies) | Fix now / Fix again… (existing) | queues an address job, as today |
| request review, after a reviewer asked for changes and I pushed | Re-request &lt;login&gt; | asks those reviewers again on GitHub |
| request review, nobody asked yet | Request review… | a dialog: GitHub's suggested reviewers and the people who reviewed the author's recent PRs, as checkboxes, plus a field for other logins; asks the ticked ones on GitHub |
| finish draft | Ready for review | marks the PR ready on GitHub |
| merge | Merge… | a dialog with the merge methods the repo allows; merges at the head the board shows |
| resolve conflicts, fix checks | none | stays text (*my call*: a conflict-resolving job is a separate feature) |

A line Cleave's own move names (push, replies, post review) keeps its button; the turn's button is used only when
there is none.

## Decisions

- **Main button visibility.** The existing Review now and Fix now buttons are marked main (`is-main`) instead of being
  duplicated; the always-visible rule keys on that class.
- **One click or a dialog.** Re-request and Ready for review run at one click: the label says exactly what happens,
  and both can be undone or repeated harmlessly. Request review… and Merge… open a dialog (*my call*, as proposed).
- **Merge method.** The dialog offers the methods the repo allows; merge commit is preselected when allowed, then
  squash, then rebase (*my call*: the base branch's history is merge commits).
- **The server checks the move.** A move runs only while the stored board row still names it (my turn
  `request_review`, `finish_draft` or `merge`); a merge only at the row's head, and GitHub checks the head again
  (`sha`). Logins are validated; the author is refused as a reviewer.
- **GitHub writes are single requests, never retried** (as approve): a retry after a timeout could double a
  notification, and a merge's answer must be the real one. No answer: "check the PR on GitHub".
- **The board catches up.** After a move succeeds the repo is due for a poll at once (the loop wakes within 60 s),
  so the card's line changes without waiting for the poll interval.
- **Every card button's answer shows on its card**, Review now and Fix now included (the status line stays too).
- **Every dashboard action is logged** at INFO: what, which PR, the outcome; never typed text.
- **Not in this change** (*my call*): fixing a PR stacked on another PR's branch. Prepare's base check protects the
  merge base and the trusted process commands; changing it needs its own design. The card already shows the reason.

## API

- `POST /api/reviews/pr-move` `{scope, number, move: request_review|ready|merge, reviewers?, method?, head?}` →
  `{state: done|refused|failed, text}`; 400 for a malformed body, 404 for a PR not on the board.
- `GET /api/reviews/candidates?scope&number` → `{candidates: [{login, reason}]}`; 502 when GitHub fails.
- `GET /api/reviews/merge-options?scope` → `{methods: [...], default}`; 502 when GitHub fails.
- `/api/reviews` rows gain `rerequest`: the reviewers of my `request_review` turns (the status command's `reviewer`).
