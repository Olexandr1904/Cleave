# Telegram: buttons on the review message — design

Date: 2026-11-24. Small change. The operator saw a finished review's message arrive "without buttons at all" and
picked three event buttons over one button or a confirmation step.

## Intent

A review message is posted with one tap, as a threads message is: the operator picks the review's event, and every
confirmed finding is posted with it. Typed replies (`post 1 2 4 approve`, `text 2`, `edit 2: …`) stay as they are
for anything finer.

## Approaches

- **Three event buttons (chosen by the operator).** `Comment` · `Approve` · `Request changes`, the report's suggested
  event first; a tap posts every confirmed finding with that event at once, as the dashboard's **Post review…**
  does (`post`, `all_confirmed`).
- **One button with the suggested event.** Fewer buttons, but another event needs typing. Not chosen.
- **A button that asks first** (event, then Yes / No), as **Push anyway…** does. One more tap on every review. Not
  chosen.

## Decisions

- The buttons go on the message's last part, as a fix message's do. A tap runs the command the typed reply would
  (`post` + the event, all confirmed findings), through the same checks: the head, a second approval, a review of
  the operator's own since the run, the posting's dry run. Its progress line and result answer the tapped message.
- On a review of the operator's own PR only **Comment** is offered: GitHub refuses approving or requesting changes
  on one's own PR. The run records whether it is the operator's own PR (`JobRun.own_pr`, false for runs stored
  before).
- Once the job is no longer `done` (a review was posted, or it is superseded or stale) its message has no buttons:
  every post refreshes them, a tap's and the dashboard's.
- Labels, in the message's language: `Comment` / `Коментувати`, `Approve` / `Схвалити` (as the pipeline's),
  `Request changes` / `Запросити зміни`. The reply hint line stays.

## Left to the builder

The callback names (`rv_<word>:<job id>`, within Telegram's 64 bytes), tests.
