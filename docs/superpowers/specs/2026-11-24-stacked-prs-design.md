# Reviewer: PRs stacked on another PR's branch — design

Date: 2026-11-24. Small change. The operator asked for it ("сделай фикс для стекнутых PR") after a Fix now on a PR
whose base is another PR's branch failed at once with "PR base is <branch>, not <base_branch>".

## Intent

A PR whose base is another PR's branch (a stacked PR) gets the same review, re-check and fix jobs as a PR into the
configured base branch. Before, Prepare refused it: the review clone held the base branch only, so the merge base and
the diff would have been wrong.

## Approaches

- **Diff against the PR's own base, commands from the configured base (chosen).** Prepare fetches the PR's base
  branch as git objects and takes the merge base against it. `MB`, the diff anchors and `BASE` in meta.env follow
  GitHub's diff of the PR, so findings and replies anchor where GitHub shows them. The process document, the part
  commands and the prepare steps still run from the configured base branch's checkout: nothing from the stacked
  base (an unmerged branch) is executed.
- **Diff against the configured base.** No fetch, but the diff would hold the parent PR's changes too: the review
  would judge code that is not this PR's, and anchors would miss GitHub's diff. Rejected.
- **Run the process from the PR's base branch.** Its commands would come from an unmerged branch, which the
  reviewer's trust rule forbids ("from the repo's base branch, trusted after merge"). Rejected.

## Decisions

- The stacked base is fetched into the review clone as a local branch of its own name, under the clone's lock,
  right before the job clone is made, so the job clone has `origin/<base>` (the process document's
  `origin/$BASE` resolves too).
- A base name that is not a plain branch name (`[A-Za-z0-9._/-]+`, not starting with `-`, no `..`, not ending with
  `/` or `.lock`), or one in Cleave's own `pr-review/` namespace, is refused at Prepare: `'<name>' is not a branch
  Cleave fetches`.
- A stacked base that is gone from the remote (its PR merged and the branch deleted, before GitHub retargets the PR)
  fails Prepare with the fetch's error; the next job after the retarget runs as usual.
- Pushing a fix is unchanged: it goes to the PR's head branch.
- Any base other than the configured one is fetched the same way, not only another PR's branch: a PR into a release
  branch or main is reviewed with the configured base's process too (*review call*: the old refusal is gone; a fix is
  still never pushed to the configured base, and auto reviews stay bounded by the size limit).
- A name equal to the configured base, `HEAD` or `pr-review…` without regard to case is refused: the clone may sit on
  a case-insensitive disk, where `Develop`'s loose ref is `develop`'s file. As defense in depth, Prepare checks that
  the job clone's `origin/<base>` is the commit `refresh()` just fetched before any step runs.
- meta.env's values are shell-quoted: the session's shell sources it, and `BASE` is a name GitHub gives.
- The review prompt tells the session the PR's changes are `MB..HEAD`, so a skill that picks its own base reviews
  only the PR's commits, not the parent PR's.
