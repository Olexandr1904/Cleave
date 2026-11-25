# Agent reports stay out of the PR — design

**Date:** 2026-11-25
**Status:** approved by the operator on 2026-11-25

## Intent

Every ticket PR still carries the pipeline's own reports: `ai_pipeline/<ticket>/ba.md`, `developer.md`, the
`*-agent-output.md` files, `qa-gates.md` and the rest, 200 to 1100 lines a PR. The operator calls it AI slop: it
buries the change under paperwork nobody reviews.

## How they get in

The reports were made part of the branch on 2026-05-08, as a per-ticket archive in the product repo. On 2026-08-28
that was reversed: Cleave stopped staging them and new clones got `ai_pipeline/` in `.git/info/exclude`. Three things
were missed:

- `agents/dev-agent.md` still says "Stage your code changes AND `ai_pipeline/{ticket_id}/` in the same commit". The
  agent finds the directory ignored and runs `git add -f`.
- Nothing in Cleave checks the branch before a push, so whatever an agent commits is pushed.
- `reset_source` (the dashboard's Rerun) clones afresh without the exclude.

Code comments still say the reports are committed and pushed.

## Goals

- No change under `ai_pipeline/` reaches a PR that Cleave pushes, whatever an agent does.
- Agents keep reading and writing the reports where they are today.

## Non-goals

- Removing the reports that earlier merged PRs left on the base branch. Deleting them in a ticket's PR is out of the
  ticket's scope; a separate cleanup can do it.
- Moving the reports out of the clone (rejected on 2026-08-28: about 90 path references in 25 files, every agent
  prompt among them).

## Approaches

**A. Prompt, guard before push, exclude on re-clone (chosen).** The prompt stops asking for the reports in the
commit; a guard before every push takes the branch's own changes under `ai_pipeline/` back out; `reset_source` writes
the exclude. The guard makes the result independent of what an agent decides.

**B. Prompt only.** Cheapest, but an agent that stages the reports anyway (or an older session resumed with the old
instruction) still puts them in the PR.

**C. Move the reports out of the clone.** Cleanest end state, far the largest change. Rejected as above.

## Design

1. `agents/dev-agent.md`, step "Commit": stage only code and test changes; `ai_pipeline/` is kept out of git on
   purpose and is never staged, `git add -f` included.
2. `drop_pipeline_reports(workspace, repo_config)` in `orchestrator/pipeline/actions/push_and_open_pr.py`. It finds
   the paths under `ai_pipeline/` that the branch changed since its merge base with `origin/<default branch>`
   (`origin/HEAD` as fallback), and adds one commit, `chore(<ticket>): keep pipeline reports out of the PR`, that sets
   those paths back to the merge base. The commit is built on a temporary index (`read-tree`, `reset`, `write-tree`,
   `commit-tree`, `update-ref` against the old head), so nothing else staged in the clone goes into it; then those
   paths alone are reset in the clone's own index. Files on disk are not touched. Reports that the base branch
   already holds are left as the base has them. Any git failure logs a warning and the push goes on as before.
3. `action_push_and_open_pr` calls it before a push to an open PR, and on the first push after
   `ensure_branch_has_commits` and before `squash_feature_commits`, so the squash folds it into the one commit.
4. `reset_source` writes the exclude as `create` does.
5. Stale comments and docstrings that say the reports are committed are corrected.

PRs opened before this change lose their reports on the next push Cleave makes to them.

## Left to the builder

Test layout; the exact log line.
