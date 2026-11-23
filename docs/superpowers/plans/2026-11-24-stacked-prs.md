# Stacked PRs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prepare runs for a PR whose base is another PR's branch: merge base and BASE from that branch, commands from
the configured base.

**Spec:** `docs/superpowers/specs/2026-11-24-stacked-prs-design.md`

## Global Constraints

- Nothing from the stacked base is executed: prepare steps keep `cwd` = the job clone's base checkout.
- Tests use local bare repos only; no names, logins or paths from a real repo.

## Review Focus

1. Two PRs stacked on the same branch prepared one after another: the second sees the branch's current tip.
2. A base branch deleted on the remote: Prepare fails with a clear reason, never a crash.
3. A base name like `-x`, `a..b` or `pr-review/7`: refused before any git call.

---

### Task 1: ReviewClone fetches a stacked base; merge_base takes a base

**Files:** `orchestrator/reviewer/clone.py`; test `tests/unit/test_reviewer_clone.py`.

- `fetch_branch(branch: str) -> str`: fetch `+refs/heads/<b>:refs/remotes/origin/<b>`, update the local branch `<b>`,
  return its sha; CloneError for a name `plain_branch(b)` refuses (checked before git runs).
- `merge_base(head: str, base: str = "") -> str`: against `origin/<base or the configured base>`.
- Tests: `test_fetch_branch_and_merge_base_against_it` (PR 8 on top of `task/parent`; develop moves on; merge base is
  the parent's tip; a job clone made after has `origin/task/parent`), `test_fetch_branch_follows_the_tip`,
  `test_fetch_branch_refuses_odd_names`, `test_fetch_branch_of_a_gone_branch_is_a_clone_error`.

### Task 2: Prepare runs a stacked PR

**Files:** `orchestrator/reviewer/prepare.py`; test `tests/unit/test_reviewer_prepare.py`; docs.

- Replace the base refusal: when `pr.base != base`, `fetch_branch(pr.base)` under the lock after `refresh()`; the job
  clone's merge base and meta.env's `BASE` use `pr.base`; the prepare steps still run from the base checkout.
- Tests: `test_prepare_a_stacked_pr` (meta.env `BASE=task/parent`, `MB=<parent tip>`, the step got that MB, the job
  clone is at develop), `test_prepare_refuses_a_base_it_does_not_fetch`.
