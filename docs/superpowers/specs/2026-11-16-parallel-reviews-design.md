# Parallel review jobs — design

Date: 2026-11-16. Extends `2026-11-12-reviewer-mode-design.md` (reviewer mode). Decided with the operator in
conversation on 2026-11-16.

## Intent

Today the job runner takes review and re-check jobs ("heavy jobs") one at a time, and each holds the repo's
review clone for its whole run (`JobRunner._run` holds `ctx.clone.lock` from Prepare through cleanup). With
reviews on Opus at xhigh taking up to 90 minutes, the fifth PR waiting for the operator waits hours behind
the other four. The operator's view: a review runs no build and no test, so the only things a review needs
of its own are a checkout and some disk; the claude.ai quota is no reason to serialize either, because five
reviews use the same quota one after another or all at once. Running them at once only gets the results
sooner.

So: **every heavy job that is ready starts at once**, each in its own checkout, and each result reaches
Telegram as soon as it is ready.

## Goals

1. Heavy jobs of different PRs run in parallel. A job is ready by today's rules (quiet window, Review now,
   switches, forge and login gates, usage-limit wait); readiness is unchanged, only the one-at-a-time rule
   goes.
2. Each heavy job runs in its own **job clone**: a `git clone --local` of the repo's review clone (objects
   hard-linked, not borrowed), checked out detached at the base commit that job's Prepare refreshed to. The job's run directory and PR worktree
   are under that job clone, so no two jobs share a checkout, a run directory, a worktree or a `.git/config`.
3. The review clone (`<reviewer dir>/repos/<project_id>/<repo_id>`) becomes base-only: polls refresh it and
   run readiness from it, posts run the poster from it, jobs fetch into it. **No model session runs in it
   any more.** A job holds its lock only for the short git steps that touch it (refresh, fetch of the PR
   head, creating the job clone), never across the session.
4. Within one PR nothing changes in order: at most one heavy job per PR runs at a time (review before its
   re-check, as today; a job for a newer head waits for the running one to end and then settles as today).
5. A job clone and its worktree are removed as soon as the job ends, whatever the outcome, and job clones
   left by an interrupted run are removed at start-up.
6. A safety cap on heavy jobs running at once across all repos: `reviewer.max_parallel_jobs` in
   `global.yaml`, default 6. It protects the host's memory (each job is a `claude` process plus the agents
   the process document starts), not the quota.

## Non-goals

- Parallelism inside one review: the process document's own agents are unchanged.
- Small jobs (edit, draft, command): they already run on their own and are unchanged.
- The ticket pipeline's concurrency.
- The quiet window, the usage-limit and login handling, the messages and their format.
- Cleaning the `claude` CLI's per-directory transcripts (`~/.claude/projects/<path>/`) that job clone paths
  leave behind.

## Anti-goals (a failure even if it "works")

- Any weakening of the session's isolation as Task 9 of plan 6 measured it: the session still starts with
  its working directory at a trusted base checkout, the PR head only in the `--add-dir` worktree, the repo's
  own `.claude/` from the PR never loaded, tools that need the forge token (readiness, poster) run only from
  the review clone and never inside a session, the token never in a job clone's `.git/config` or
  environment.
- Two jobs sharing a checkout, a run directory, a worktree or git config.
- Job clones or worktrees piling up after crashes, kills or restarts.
- A session's change to its job clone's `.git/config`, `.git/info/attributes` or `.git/commondir` going
  unnoticed: it still fails the job (`the session changed its job clone's git config`).

## Constraints

- The job clone is made from the local review clone with `git clone --local` after the review clone has
  fetched the PR head: no network, no token, seconds; objects are hard links (no extra disk for them), only
  the base checkout costs disk. Not `--shared`: the session runs with
  `permissions.blockReadsOutsideWorkingDirectories` and a Bash sandbox that reads only its working
  directories, so objects borrowed through `alternates` from the review clone would be unreadable to the
  session's git. With `--local` every object is inside the job clone, and the review clone may delete
  `pr-review/<n>` or prune at any time without affecting a running job.
- Hard links need the job clones on the same volume as the review clone (both under the reviewer dir). A
  hard-linked object file is shared with the review clone, so the job clone must stay unwritable to the
  session: it is, as the session's cwd it is under `sandbox.filesystem.denyWrite` (keep it so). Git itself
  never rewrites an object file in place.
- Every path the session sees comes from the job clone: `job_paths`, the session's permission rules and
  sandbox (`settings_json`, `allowed_tools`), Prepare's `_check_inside`, the helper scripts and prepare
  steps (`cwd`), the archive of the run directory. Paths keep today's checks (no `*`, `?`, `[`; nothing
  under `~/Documents`, `~/Desktop`, `~/Downloads` or iCloud Drive).
- `process.worktree_dir` is relative to the clone (`../<dir>/PR_{pr}` in the reference repo). Job clones
  live in a directory of their own per job, so `..` stays inside that job's directory.
- Workspace trust: plan 6 Task 9 ran a `-p` review session in an untrusted throwaway clone with no trust
  block, so new job clone paths need no GUI trust step. The first parallel job on the host confirms it.
- No employer names in the repo; tests never start the real `claude` or reach GitHub (fakes, `tmp_path`).
- Host updates follow plan 6 Procedure U; restart only while no job runs (README's `sqlite3` check).

## Decided by the operator (2026-11-16)

- No queue between PRs; quota is not a reason to run reviews one at a time.
- A per-job copy of the clone and a per-job worktree, removed right after the job.
- A cap as a host safety valve, default 6, configurable.
- Isolation stays exactly as it is.

## Decided in this design (the author's calls)

- Job clone location: `<reviewer dir>/work/<project_id>/<repo_id>/<job_id>/clone` (the reviewer dir is
  the directory of `reviewer.db`). The worktree then lands at `.../<job_id>/<worktree_dir>`.
- One heavy job per PR at a time, enforced when the runner picks jobs.
- The cap counts heavy jobs across all repos. When the cap is reached, ready jobs stay `queued` with no
  reason change.
- A job does not start while free space on the reviewer dir's volume is under
  `workspaces.min_free_disk_gb` (the ticket pipeline's existing threshold); it stays `queued` and the
  runner logs why once per streak.
- A guarded-file change in a job clone fails the job as today; the clone is then deleted, so no write-back
  is needed.
- The running usage-limit and login gates stop new starts only; jobs already running finish on their own
  outcomes. A wait (usage limit or login) is lifted only by a session that **started after the wait
  began**: a session that was already running when the limit hit and then ends well does not lift it, so
  new jobs do not start into an exhausted quota.
- Deferrals count per job as today (`_MAX_DEFERRALS`, the third fails the job): each job counts only its
  own session's outcomes, so the anti-loop rule keeps its meaning. Known consequence: when the limit hits
  mid-run, every job running then is deferred and that run's work is lost (per job, as today with one).
- At start-up, `recover()` (running jobs fail as interrupted, as today) is followed by removing every job
  clone directory under `work/`.

## Left to the builder

- The runner's loop shape (for example a task per job under a semaphore), and the cadence of
  `resend_unsent` and the retention sweep in it; the sweep must not race a job of the same PR.
- What becomes of `ReviewClone.session_phase`, `session_lock`, `in_session`, `changed_git_files` and
  `reader()` once no session runs in the review clone: simplify them, keeping the rule that a post's dry
  run and confirm run see the same checkout of the review clone.
- How the poll reads the review clone while jobs hold its lock only briefly (today it skips or reads in a
  session phase).
- Where the drift check of the preparation section runs so that two parallel Prepares of one repo store
  and report a changed section once.
- How the job clone is checked out at the refreshed base and gets the PR head (for example
  `git clone --local --no-checkout` after the review clone's fetch of `pr-review/<n>`, then a detached
  checkout of the base commit), with the existing git environment (no host config, no hooks, no credential
  helper); the job clone's `origin` must not point at GitHub with a token, and no job git call reaches the
  network.
- Dashboard and API: several `running` jobs at once must show correctly.

## How we know it works (on the host)

- With three PRs ready, three jobs start within one runner cycle, each in its own job clone; their
  sessions' init events show the job clone as `cwd`.
- A poll runs while the jobs run: `last_poll_at` advances every poll interval.
- A post can run while jobs run.
- After each job, its `work/<...>/<job_id>` directory is gone; after a restart during a job, too.
- The Task 9 isolation probe, pointed at a job clone, gives the same results as on 2026-11-16, and the
  session's `git show`/`git diff` read the job clone's objects (no read denials).
- A session that writes its job clone's `.git/config` fails its job and leaves the review clone and the
  other jobs untouched.
