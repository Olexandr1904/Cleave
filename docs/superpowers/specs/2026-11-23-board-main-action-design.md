# The main action on the board card and at the top of the PR panel — design

Approved by the operator on 2026-11-23 (a small change).

## Intent

The operator works from the dashboard's Reviews board. A card's line says what is left to do ("fix ready: push",
"fix pushed: post the replies", "post the review"). However, the button that does it is only inside the PR panel,
low in its Fix or Replies section. The operator wants that one button on the card itself, and the same button at the
top of the panel.

## Goals

- A card whose line names a move Cleave can make shows that move as its first, primary button:

  | Card line | Button | Behaviour |
  |---|---|---|
  | fix ready: push | **Push** | One tap. |
  | fix ready: push (the fix touches protected files) | **Push anyway…** | Asks first, with the panel's text. |
  | fix pushed: post the replies, replies ready: post, post the replies | **Reply all** | One tap. |
  | post the review | **Post review…** | Asks first, with a choice of Comment / Approve / Request changes (Comment preselected). |

- The button does exactly what the line says, nothing more. "fix ready: push" pushes; it does not also post the
  replies. The card then reads "fix pushed: post the replies", and its button is Reply all.
- The PR panel shows the same button in its header, beside Fix again… and the other header buttons, so it needs no
  scrolling. The bars lower in the panel stay as they are.
- The result of a tap is shown as a short line on the card, and in the panel when that PR is open. Then the board
  reloads. Telegram gets its "🖥 Dashboard: …" line as for any dashboard command.

## Non-goals

- No new action route. The button sends the same command as the panel's buttons, through `POST /api/reviews/action`.
- No change to the panel's lower bars, the Telegram buttons or the command field.

## Anti-goals

- A card label that promises something other than what happens, for example "Push" on a protected fix.
- A post to GitHub with no confirmation where the panel asks for one.

## Approach

The board row carries only what the label needs. The exact command is resolved from the PR's detail at the tap.

- The server adds one fact to the board's fix info: `protected` (the fix touches protected files). The board already
  reads that run, so the board's load time does not change.
- One pure function decides the card's move from the board row; the card's line and the button both use it.
- A second pure function turns that move and the PR's detail into the command, its job and its confirmation. It
  returns a refusal when nothing is left to do, for example when the replies were already posted.

The alternative was to have the server compute the whole button for every board row. It was rejected because it would
copy the panel's rules into Python a second time, and would read every PR's replies and posts on each board load.

## Left to the builder

The markup and CSS of the card's result line, the names of the functions, and the test layout.
