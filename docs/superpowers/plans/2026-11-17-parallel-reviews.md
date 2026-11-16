# Parallel Review Jobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Review and re-check jobs of different PRs run at once, each in its own job clone, up to `reviewer.max_parallel_jobs`.

**Architecture:** The review clone becomes base-only; a job takes its lock only to refresh it, fetch the PR head, run the drift check and make a `git clone --local` job clone at `<reviewer dir>/work/<project_id>/<repo_id>/<job_id>/clone`. Prepare's remaining steps, the session and the archive run in the job clone, which is removed when the job ends. The runner starts one asyncio task per job under a cap; the review clone's session-phase machinery, now unused, goes.

**Tech Stack:** Python 3.12 asyncio, git (`clone --local`, worktrees), pytest (asyncio auto mode), real git in tests over local bare origins.

**Spec:** `docs/superpowers/specs/2026-11-16-parallel-reviews-design.md` (parent: `2026-11-12-reviewer-mode-design.md`).

## Global Constraints

- No employer names in the repo: placeholders `<owner>/<repo>`, `acme` in fixtures.
- Tests never start the real `claude`, never reach GitHub, Jira or Telegram; never run `pkill`/`killall` (`tests/conftest.py` records Gradle daemon stops).
- The session's isolation is unchanged: cwd at a base checkout (now the job clone), the PR only in the `--add-dir` worktree, `settings_json`/`allowed_tools` unchanged in content, the token never in a job clone's config or environment, no session in the review clone.
- `reviewer.max_parallel_jobs`: default 6, an integer ≥ 1.
- A job does not start while free space on the reviewer dir's volume is under `workspaces.min_free_disk_gb`.
- Suite: `$PY -m pytest tests/unit tests/integration -q -p no:cacheprovider` from the worktree root (`PY=~/Cleave/.venv/bin/python`, `PYTHONDONTWRITEBYTECODE=1`); exactly the 9 known failures (`test_not_a_git_dir`, 3× `test_main.py::TestMain`, 3× `test_e2e_dry_run.py`, `test_build_jql_ands_multiple_trigger_labels`, `test_bad_payload_returns_400`). The process may hang after the summary: read the summary line.
- No daemon restart, no `config-live/` or `.env` edits, no push, no merge into master during implementation.

## Review Focus

1. A job killed mid-run (daemon restart, cancellation, crash) leaves `work/<…>/<job_id>`: start-up removes every job directory under `work/` (Task 4, `test_recover_removes_leftover_job_dirs`).
2. Two jobs of one repo prepare at the same moment: the review clone's git calls never overlap and a changed preparation section is reported once (Task 3, `test_parallel_prepares_report_drift_once`).
3. The daemon stops while jobs run: no job task outlives the runner; each running job is failed by `recover()` at the next start (Task 4, `test_cancelled_loop_cancels_its_jobs`).
4. A session rewrites its job clone's `.git/config`: that job fails, the review clone and the other running job are untouched (Task 4, `test_tampered_job_clone_fails_only_its_job`).
5. The disk fills while jobs run: running jobs go on, ready jobs stay `queued`, one log line per streak (Task 4, `test_low_disk_holds_new_jobs`).

---

## File structure

- `config/schemas.py`, `config/config_loader.py`: `ReviewerGlobalConfig.max_parallel_jobs` (Task 1).
- `orchestrator/reviewer/clone.py`: `ReviewClone.make_job_clone` (Task 2); session-phase machinery removed, `reader()` simplified (Task 5).
- `orchestrator/reviewer/prepare.py`: the locked part and the job-clone part; `Prepared.clone`; `job_paths` worktree check (Task 3).
- `orchestrator/reviewer/runner.py`: parallel loop, per-job directory, cap, disk gate, streak rules, start-up cleanup (Task 4).
- `orchestrator/reviewer/service.py`, `orchestrator/reviewer/posting.py`: the poll always takes the lock; posting's guarded-file checks removed (Task 5).
- `main.py`: `RunnerConfig` wiring (Task 1).
- Docs: `docs/dashboard.md`, `docs/telegram.md`, `deploy/README.md` (Task 6).

---

### Task 1: `reviewer.max_parallel_jobs` and the runner's new settings

**Files:** Modify `config/schemas.py` (`ReviewerGlobalConfig`), `config/config_loader.py` (`_parse_reviewer_global`), `orchestrator/reviewer/runner.py` (`RunnerConfig`), `main.py` (the `RunnerConfig(...)` call). Test `tests/unit/test_config_reviewer.py`.

**Interfaces:**
- Produces: `ReviewerGlobalConfig.max_parallel_jobs: int = 6`; `RunnerConfig(jobs_dir, secret_paths, jobs, ledgers_dir=None, work_dir: Path | None = None, max_parallel: int = 6, min_free_bytes: int = 0)`; `RunnerConfig.job_root(project_id: str, repo_id: str, job_id: str) -> Path` = `(work_dir or jobs_dir.parent / "work") / project_id / repo_id / job_id`.

- [ ] **Step 1: Failing tests** in `test_config_reviewer.py`: `test_max_parallel_jobs_defaults_to_6` (a `global.yaml` without it → 6); `test_max_parallel_jobs_read` (`reviewer: {max_parallel_jobs: 2}` → 2); `test_max_parallel_jobs_below_one_refused` (0 and -1 → `ConfigError` naming `reviewer.max_parallel_jobs`).
- [ ] **Step 2:** Run them: FAIL (unknown field / no validation).
- [ ] **Step 3:** Add the field; validate `isinstance(int) and >= 1` in `_parse_reviewer_global` (a `bool` is refused). Add the `RunnerConfig` fields and `job_root`. In `main.py`: `work_dir=reviewer_data / "work"`, `max_parallel=global_config.reviewer.max_parallel_jobs`, `min_free_bytes=global_config.workspaces.min_free_disk_gb * 1024**3`.
- [ ] **Step 4:** Run `test_config_reviewer.py`, `test_main_reviewer.py`: PASS.
- [ ] **Step 5:** Commit `feat(reviewer): max_parallel_jobs setting`.

### Task 2: The job clone

**Files:** Modify `orchestrator/reviewer/clone.py`. Test `tests/unit/test_reviewer_clone.py` (its `origin`/`pr7` fixtures: a local bare origin with `develop` and `refs/pull/7/head`).

**Interfaces:**
- Produces: `async ReviewClone.make_job_clone(self, dest: Path, number: int) -> ReviewClone`. Caller holds `self.lock` and has run `refresh()` and `fetch_pr_head(number)`. Runs `git clone --local --no-checkout <self.path> <dest>` (no `--shared`), then in `dest`: `checkout --force --detach refs/remotes/origin/<base>`, `update-ref refs/heads/<base> refs/remotes/origin/<base>`, `update-ref refs/heads/pr-review/<number> refs/remotes/origin/pr-review/<number>`; all through `_git` (same env, timeout). Returns `ReviewClone(dest, str(self.path), base, "", self._timeout)`. A `dest` that already exists raises `CloneError("job clone path exists: …")`; `dest.parent` is created (mode 0700).

- [ ] **Step 1: Failing tests:** `test_job_clone_is_at_base_with_the_pr_branch` (HEAD == `refresh()`'s sha; `refs/heads/pr-review/7` == the PR head; `refs/remotes/origin/develop` == base; `merge_base(head)` on the returned clone == the base sha); `test_job_clone_needs_nothing_from_the_review_clone` (after the review clone's `delete_branch(7)` and `git gc --prune=now`, `git -C <job> cat-file -e <pr head>` succeeds — objects are the job clone's own, not borrowed: no `objects/info/alternates` file); `test_job_clone_carries_no_token` (its `.git/config` has no `extraheader`, `credential` or the token; `remote.origin.url` is the review clone's path); `test_job_clone_refuses_an_existing_path`.
- [ ] **Step 2:** Run: FAIL (`AttributeError: make_job_clone`).
- [ ] **Step 3:** Implement as specified.
- [ ] **Step 4:** Run `test_reviewer_clone.py`: PASS.
- [ ] **Step 5:** Commit `feat(reviewer): job clone made from the review clone`.

### Task 3: Prepare in the job clone

**Files:** Modify `orchestrator/reviewer/prepare.py` (`job_paths`, `prepare`, `_prepare`, `Prepared`). Test `tests/unit/test_reviewer_prepare.py`.

**Interfaces:**
- Consumes: `ReviewClone.make_job_clone` (Task 2).
- Produces: `Prepared(pr: PRFull, drift: bool, clone: ReviewClone)` — `clone` is the job clone. `prepare(ctx, job, paths, *, store, tracker, now, job_clone_dir: Path) -> Prepared`; the caller no longer holds `ctx.clone.lock`: prepare takes it itself for the review-clone part only. `job_paths(clone_dir, …)` is called with the job clone's path and now also raises `PrepareError("failed", "worktree_dir is outside the job's directory")` unless the worktree lies strictly inside `clone_dir.parent`.

Order in `_prepare`: `pr_full` checks (open, head, base) with no lock → under `ctx.clone.lock`: `refresh()`, `has_file(doc)`, `fetch_pr_head(n) == head`, the drift check (section read from the review clone; `section is None` fails), `make_job_clone(job_clone_dir, n)` → lock released → in the job clone: `_check_inside` again, `merge_base`, run directory, `add_worktree`, the artifacts, `ctx.readiness.threads(job_clone.path, n)`, `ticket.json`, the prepare steps with `cwd=job_clone.path`. A `CloneError` anywhere stays `PrepareError("failed", …)`; a drift found before a later failure still raises with `drift=True`.

- [ ] **Step 1: Failing tests** (real git over the module's `origin`; a fake review clone wrapper may record `lock.locked()` per call): `test_review_clone_is_locked_only_for_its_own_part` (`refresh`, `fetch_pr_head`, `make_job_clone` run under the lock; `merge_base`, `add_worktree`, readiness `threads` and the steps run without it); `test_steps_and_threads_run_in_the_job_clone` (the step's `cwd` and the `threads` clone dir are `job_clone_dir`; `anchors.json` lands in the job clone's run dir); `test_parallel_prepares_report_drift_once` (two jobs of one repo, section changed since the stored hash, `asyncio.gather` of both prepares: exactly one `Prepared.drift` is True); `test_worktree_outside_the_job_dir_is_refused` (`worktree_dir="../../elsewhere/PR_{pr}"` → `PrepareError` failed); update the existing tests to pass `job_clone_dir` and read results from the job clone.
- [ ] **Step 2:** Run: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `test_reviewer_prepare.py`: PASS.
- [ ] **Step 5:** Commit `feat(reviewer): prepare in a job clone; the review clone locked only for its own git`.

### Task 4: The parallel runner

**Files:** Modify `orchestrator/reviewer/runner.py`. Tests `tests/unit/test_reviewer_runner.py` (Rig: drop the `lock.locked()` asserts in `_prepare`/`_session`; the fake prepare makes `job_clone_dir` and returns `Prepared(PR, drift, <FakeClone at job_clone_dir>)`).

**Interfaces:**
- Consumes: Task 1's `RunnerConfig` fields and `job_root`; Task 3's `prepare(..., job_clone_dir=)` and `Prepared.clone`.
- Produces: `JobRunner.loop(stop)` runs up to `max_parallel` jobs as tasks; `JobRunner.next_jobs(limit: int) -> list[Job]` replaces `next_job()` (same gates, plus: no job of a PR that has a job in flight in this runner, no job when free space under `min_free_bytes`); `recover()` also removes every directory under `work_dir`.

Rules:
- `_run`: `root = config.job_root(...)`, `paths = job_paths(root / "clone", …)`; prepare with `job_clone_dir=root / "clone"`; the session gets `clone_dir=prepared.clone.path`; guarded files saved from the job clone and compared after the session (a change fails the job with `_CONFIG_CHANGED`; no write-back); no `session_phase`, no `remove_worktree`; `finally: shutil.rmtree(root)` after `_finish` (archive and message), a removal error logged and noted on the run.
- Loop: each cycle `resend_unsent()`, then start `next_jobs(max_parallel - in_flight)` as tasks, the retention sweep only when nothing is in flight and nothing started, then wait for a task to end, `stop`, or `idle_seconds`. On `stop`: start nothing more, await the tasks in flight. If the loop itself is cancelled, cancel and await its tasks (`finally`). A job that ended `queued` (forge token failing) is not restarted in the same cycle.
- Streaks: record each job's start time. A quota outcome sets `_quota_until = max(current, retry_at)` and `_quota_since` (first in the streak); a session that ends otherwise lifts the wait and resets `_quota_line_sent` only when it started at or after `_quota_since`. Login: same with `_login_failed_at` — lifted only by a session started at or after it. Deferral counting unchanged.
- Disk: `shutil.disk_usage(work_dir or its nearest existing parent).free < min_free_bytes` → start nothing; log `Reviewer jobs held: <n> GB free, under <m> GB` once per streak.

- [ ] **Step 1: Failing tests:** `test_jobs_of_different_prs_run_together` (three PRs, a session that waits on an event: three sessions in flight at once); `test_cap_limits_jobs_in_flight` (`max_parallel=2`, three jobs: two in flight, the third starts when one ends); `test_one_job_per_pr_at_a_time` (a second review of the same PR waits for the first); `test_job_dir_removed_whatever_the_outcome` (parametrized: done, session failed, prepare error, unexpected exception: `job_root` gone, the job copy in `jobs/<id>` kept); `test_recover_removes_leftover_job_dirs`; `test_tampered_job_clone_fails_only_its_job` (two jobs in flight, one session rewrites its job clone's `.git/config`: that job `failed` with `_CONFIG_CHANGED`, the other `done`, the review clone's config unchanged); `test_low_disk_holds_new_jobs` (patched `shutil.disk_usage`: nothing starts, one log line for two cycles, starts once space is back); `test_quota_wait_not_lifted_by_a_job_started_before_it` and `test_quota_wait_lifted_by_a_job_started_after_it`; the login equivalents; `test_cancelled_loop_cancels_its_jobs` (cancel the loop with two jobs in flight: both tasks done, no task left, job dirs removed); update the existing runner tests to the new Rig.
- [ ] **Step 2:** Run: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `test_reviewer_runner.py`, `tests/integration/test_reviewer_job.py` (adapt it: the session's cwd is the job clone; `work/` empty after the job): PASS.
- [ ] **Step 5:** Commit `feat(reviewer): review jobs of different PRs run in parallel`.

### Task 5: No session in the review clone — remove the session phase

**Files:** Modify `orchestrator/reviewer/clone.py` (remove `session_phase`, `session_lock`, `in_session`, `_saved`, `changed_git_files`; `reader(*, wait=600.0)` = the lock within `wait`, else `CloneError("the review clone is in use; try again in a minute")`), `orchestrator/reviewer/service.py` (the poll takes the lock with `_acquire` and refreshes/reads; the session branch goes), `orchestrator/reviewer/posting.py` (the three `changed_git_files()` checks and `_CHANGED` go; the lease stays across dry run and confirm). Tests: `test_reviewer_clone.py` (session-phase and reader tests replaced), `test_reviewer_service.py`, `test_reviewer_posting.py`, `test_reviewer_posting_replies.py`, `tests/integration/test_reviewer_poll.py`.

**Interfaces:**
- Consumes: Task 4 (nothing calls `session_phase` any more).
- Produces: `ReviewClone.reader(*, wait: float = 600.0)` (the `step` argument goes; update its callers).

- [ ] **Step 1: Failing tests:** `test_reader_waits_for_a_short_lock` (lock held 0.05 s: the reader enters after it); `test_reader_busy_past_its_wait` (lock held past `wait` → `CloneError`); `test_poll_waits_for_a_job_preparing` (the lock held briefly by a job: the poll refreshes and reads after it, instead of skipping); delete the tests of the removed behaviour.
- [ ] **Step 2:** Run: FAIL.
- [ ] **Step 3:** Implement; `grep -n "session_phase\|session_lock\|in_session\|changed_git_files" -r orchestrator tests` prints nothing.
- [ ] **Step 4:** Run the touched test files: PASS.
- [ ] **Step 5:** Commit `refactor(reviewer): no session in the review clone; drop the session phase`.

### Task 6: Docs and the full check

**Files:** `docs/dashboard.md` ("Review clone": add job clones under `<reviewer data dir>/work/`, removed after each job; lines saying "one job at a time" → up to `reviewer.max_parallel_jobs` at once, one per PR), `docs/telegram.md` (the "one job at a time" line), `deploy/README.md` (macOS section: job clones under `~/CleaveData/reviewer/work/`, same volume as the review clone; free space gate).

- [ ] **Step 1:** Edit the docs.
- [ ] **Step 2:** Full suite: only the 9 known failures.
- [ ] **Step 3:** Leak check: `git diff master | grep '^+' | grep -c -i -E '<employer/host names>'` prints 0.
- [ ] **Step 4:** Commit `docs: parallel review jobs`.

### Task 7: Host check (after the operator's merge and a restart while no job runs)

- [ ] **Step 1:** With two or more PRs ready, jobs start in one runner cycle, each session's init `cwd` a `work/<project_id>/<repo_id>/<job_id>/clone`.
- [ ] **Step 2:** During the jobs, `/api/reviews` shows `last_poll_at` advancing every poll interval.
- [ ] **Step 3:** After each job, its `work/…/<job_id>` is gone; `~/CleaveData/reviewer/work` is empty when nothing runs.
- [ ] **Step 4:** A job's `permission_denials` hold no `Read`/`Bash` denial of the job clone's own objects (the session's `git show`/`git diff` read them).
