# One press for a fix and its replies — design

The operator, 2026-11-24, after pressing every button on the Reviews board: "push and post replies are one action when
this is an iteration after comments; what is the point of doing just one of them". The logs of those presses showed
two more reasons a press did nothing, and a log line that did not say why.

## What changes

1. **Push and reply are one move.** A fix of review threads (an address job) with drafted replies offers one button,
   **Push + reply all** (**Push anyway + reply all…** with protected files), in Telegram, in the PR panel and as the
   board card's main button. The plain **Push** is gone there. A findings fix (a self-review's, no replies) keeps
   **Push**. A pushed fix keeps **Reply all** for the replies left. The typed `push` command still works.
   The card line of such a fix says `fix ready: push and reply`.
2. **A thread resolved on GitHub does not block the others.** When the replies tool's dry run fails, Cleave reads
   GitHub's threads once: replies to threads no longer unresolved there are left out, and the dry run runs once
   more on the rest. The answer names the ones left out (`Left out 1: their threads were resolved on GitHub after
   the replies were drafted.`). When none is gone the refusal stays as it was; when all are gone nothing is posted
   and the answer says so.
3. **A `<` inside code is not HTML.** `body_problems` turned HTML mode on for any `<` and a letter anywhere outside a
   trusted fence, so `` `TEST-<FQN>.xml` `` made `` `@HiltAndroidTest` `` count as a mention. A `<` inside an inline
   code span of a trusted paragraph no longer turns it on. An HTML block opens only where a line's content starts,
   after container markers; a `<` inside a span has a backtick before it, so it is not there. A paragraph is trusted
   only while nothing in it may take a backtick before code spans do (a `[`: a link's destination, title or label,
   a footnote label; a `$`: math) and no run is left unpaired; a blank line is spaces and tabs only. The final
   review showed the blanket rule was also the only backstop for an `<img>` behind a backtick a link title takes;
   the trusted paragraph keeps that caught, and also catches mentions and URLs hidden that way (open on master).
4. **The log says why.** A dashboard action that ends `refused`, `failed` or `unverified` logs its last text (the
   post's, after a push and reply all; repr, cut to 300 characters) after the state. A GitHub move logs only
   GitHub's answers (failed, unverified): its refusals may name a login typed into its dialog.

## Approaches considered

- Reply resolution: read GitHub's threads before every dry run (simplest to explain, but one more GitHub read per
  post and a change to every scripted post); retry only after a failed dry run (chosen: the happy path is unchanged);
  parse the tool's stderr for "is not an unresolved thread" (rejected: couples Cleave to another repo's wording).
- Mentions: drop the HTML rule for code spans entirely (rejected: the footnote-label case reopens round 4's hole);
  the `[` guard (chosen); leave it and let the operator post by hand (rejected: Android replies name generics and
  annotations in code all the time).

## Not changing

The posting checks themselves (head, plan validation, post-condition), the typed grammar, the findings fix's buttons,
and a body problem in one reply still refusing the whole post.
