# Address Jobs (Fixes on the Operator's Own PRs) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When reviewers comment on the operator's PR, reviewer mode runs an Opus session that tries to refute each comment, fixes what holds in the PR's worktree and drafts every reply; the operator pushes the fix and posts the replies by replying in Telegram.

**Architecture:** A third job kind, `address`, beside `review` and `recheck`, on the same queue, runner, Prepare and sandbox. The session follows the repo's own author-side document and may write the PR worktree; Cleave's code turns the worktree's changes into one patch kept in the job's copy. "push" applies that patch in a fresh worktree of the review clone (the only place with the token), commits and pushes without force; "post replies" reuses the replies posting, with Cleave filling `fix_sha`.

**Tech Stack:** Python 3, asyncio, git CLI, SQLite (`reviewer.db`), `claude -p` through `ClaudeCodeAdapter` (always faked in tests), pytest + pytest-asyncio, Starlette dashboard with plain JS.

**Spec:** `docs/superpowers/specs/2026-11-18-reviewer-address-design.md` (amends `docs/superpowers/specs/2026-11-12-reviewer-mode-design.md`; read "Security", "Part 2" and "Posting" there before Tasks 3, 4, 8, 9).

## Global Constraints

- Run tests with `.venv/bin/python -m pytest <path> -q` from the repo root. Tests never start the real `claude`; git tests use real git in `tmp_path` (see `tests/unit/test_reviewer_clone.py`).
- Reviewer code uses async subprocesses only (`orchestrator.reviewer.commands.run_command`), never `subprocess.run`.
- Every text bound for Telegram is an English template through `tr()`, with its Ukrainian text added to `orchestrator/tg_uk/reviewer.py` in the same task (`tests/unit/test_tg_i18n.py::test_every_text_has_ukrainian`).
- The session never gets the forge token, the network, or a git subcommand outside `session.READ_ONLY_GIT`. Cleave's git after a session runs with `NO_HOST_GIT_ENV`, `core.hooksPath=/dev/null`, `core.fsmonitor=false`, `core.symlinks=false`.
- Never `--force` (or `+refspec`) in a push. Never push to `ctx.cfg.base_branch`.
- A `fix_sha` comes from Cleave only: `posting._not_postable`'s "carries fix_sha" refusal stays.
- Stored dataclasses (`BoardRow`, `JobRun`, `Post`, `PRFull`) get new fields with defaults only.
- A repo without `process.address_doc` behaves exactly as before.
- The plan `2026-11-18-reviewer-owns-prs.md` is merged (`4ac25fd`): `plan_jobs` no longer skips a row with `pipeline` set, and the ticket pipeline hands its PR over at `pr_review`. `BoardRow.pipeline` now means only "a ticket workspace that is not done or archived records this PR". Review and re-check jobs ignore it; address jobs and "push" do not (spec, "PRs the ticket pipeline opened"). Do not change the ticket pipeline's code in this plan.
- The comparison with the skill flow is merged too (`48ad8eb`): `plan_jobs` has a `held: frozenset[int]` parameter (`jobs.held_prs`: the two PRs of a ticket being compared) and skips those rows at the top of its row loop. The address rule sits inside that loop, so a held PR gets no address job; its address turns send their turn line. Do not change `held_prs` or the skill flow's code.
- Line numbers in this plan may be off by a few lines after those merges: the names are what counts.
- Commit after each task, in the repo's style (`feat(reviewer): …`).

## Review Focus

1. The PR has symlinks, which the worktree holds as plain files: the captured patch must not list them (Task 4, `test_capture_ignores_plain_file_symlinks`).
2. A fix that adds a binary or non-UTF-8 file: the patch must survive capture and apply byte for byte, so git writes and reads the patch file itself (Task 4, `test_capture_and_apply_roundtrip_with_a_binary_file`).
3. The operator pushed a commit from their laptop between the job and "push": refused, nothing pushed (Task 9, `test_push_refused_when_the_head_moved`).
4. Replies posted before the push, then "push", then the fixed replies: all three work on the same message and the board queues no second job in between (Task 2 `test_address_not_queued_while_a_job_covers_the_turn`, Task 10 `test_fixed_reply_posts_after_the_push`).
5. "push" sent twice in a row: one commit (Task 9, `test_second_push_is_refused`).
6. A ticket of the pipeline still holds the PR (a fix loop from before the handover, a blocked ticket) and would force-push over a fix: no address job, the turn line is sent, "push" is refused (Task 2 `test_address_waits_while_a_ticket_workspace_holds_the_pr`, Task 9 `test_push_refused_while_a_ticket_workspace_holds_the_pr`).

---

### Task 1: Config and data shapes

**Files:**
- Modify: `config/schemas.py` (`_default_reviewer_jobs`, `ProcessPartConfig`)
- Modify: `config/config_loader.py::_parse_reviewer_section` (~lines 661-752)
- Modify: `orchestrator/reviewer/models.py` (`PRFull`, `JobRun`, `Post`)
- Modify: `orchestrator/reviewer/forge_github.py::pr_full`
- Modify: `orchestrator/reviewer/service.py` (`RepoContext`), `main.py::_reviewer_contexts`
- Modify: `orchestrator/reviewer/store.py`
- Test: `tests/unit/test_config_reviewer.py`, `tests/unit/test_reviewer_forge_github.py`, `tests/unit/test_reviewer_store.py`, `tests/unit/test_main_reviewer.py`

**Interfaces — Produces:**
- `_default_reviewer_jobs()["address"] == ReviewerJobConfig("opus", "xhigh", 5400)`.
- `ProcessPartConfig.address_doc: str = ""`, `ProcessPartConfig.address_protected: list[str]` defaulting to `[".github/**"]`.
- Loader: `address_doc` must be a string and repo-relative (the checks `doc` gets); `address_protected` a list of strings. Errors use the field names `reviewer.process.address_doc` / `reviewer.process.address_protected`.
- `PRFull.author: str = ""`, `PRFull.head_ref: str = ""`, `PRFull.head_repo: str = ""` (`pr["head"]["repo"]["full_name"]`, `""` when `repo` is null).
- `JobRun.fix: dict` (default `{}`), `Post.sha: str = ""` (a push's new commit; `Post.head_sha` stays the head before it). `Post.kind` gains the value `push`.
- `RepoContext.commit_author: tuple[str, str] = ("", "")`, filled from `repo_cfg.git.commit_author_name` / `commit_author_email`.
- `ReviewerStore`:
  - `runnable_jobs` and `done_unsent` select `kind IN ('review', 'recheck', 'address')`;
  - `posted_numbers` treats `address` like `recheck`;
  - `push_of(self, job: Job) -> Post | None` — the newest post with `job_id == job.id`, `kind == "push"`, `state == "posted"`;
  - `rehead_job(self, job_id: str, head_sha: str, key: str, now: str) -> None` — sets `head_sha`, `key`, `updated_at`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_config_reviewer.py — with the file's own loader helper
def test_address_defaults_and_job():
    assert ProcessPartConfig().address_doc == "" and ProcessPartConfig().address_protected == [".github/**"]
    assert ReviewerGlobalConfig().jobs["address"] == ReviewerJobConfig("opus", "xhigh", 5400)

def test_address_doc_must_be_repo_relative():   # "/abs/doc.md" and "../doc.md" each raise ConfigError
    ...                                         # with field == "reviewer.process.address_doc"

# tests/unit/test_reviewer_forge_github.py — extend the pr_full test's payload with
#   "user": {"login": "octo"}, "head": {"sha": HEAD, "ref": "feature/x", "repo": {"full_name": "acme-org/acme-app"}}
    assert (pr.author, pr.head_ref, pr.head_repo) == ("octo", "feature/x", "acme-org/acme-app")
# and one with "repo": None  ->  pr.head_repo == ""

# tests/unit/test_reviewer_store.py
def test_address_jobs_are_runnable_and_reheaded(store):
    job = store.add_job("acme/app", 7, "address", "a" * 40, f"{'a' * 40}:T1", "2026-11-20T10:00:00+00:00")
    assert [j.id for j in store.runnable_jobs("2026-11-20T10:00:01+00:00")] == [job.id]
    store.rehead_job(job.id, "b" * 40, f"{'b' * 40}:T1", "2026-11-20T11:00:00+00:00")
    moved = store.job(job.id)
    assert (moved.head_sha, moved.key, moved.updated_at) == ("b" * 40, f"{'b' * 40}:T1", "2026-11-20T11:00:00+00:00")

def test_push_of_is_the_newest_landed_push(store):
    # save Post(kind="push", state="failed"), Post(kind="push", state="posted", sha="b"*40), Post(kind="replies", state="posted")
    # for one job: store.push_of(job).sha == "b" * 40; for a job with none: None

# tests/unit/test_main_reviewer.py — in test_contexts_have_poster's arrange
    assert ctx.commit_author == ("Cleave Bot", "cleave@pipeline.local")
```

- [ ] **Step 2: Run the four files, expect FAIL**
- [ ] **Step 3: Implement the fields, the loader checks and the store methods**
- [ ] **Step 4: Run the four files and `tests/unit -k "reviewer or config" -q`, expect PASS**
- [ ] **Step 5: Commit** — `feat(reviewer): config and stored shapes for address jobs`

---

### Task 2: Planning address jobs from the board

**Files:**
- Modify: `orchestrator/reviewer/board.py`
- Modify: `orchestrator/reviewer/jobs.py`
- Modify: `orchestrator/reviewer/runner.py::settle`
- Modify: `orchestrator/reviewer/service.py::_poll` (the `plan_jobs` call, ~line 320: add `answered=` beside `posting=` and `held=`) plus one private method
- Test: `tests/unit/test_reviewer_jobs.py`, `tests/unit/test_reviewer_runner.py`, `tests/unit/test_reviewer_service.py`

**Interfaces:**
- Consumes: `ProcessPartConfig.address_doc`, `ReviewerStore.posted_replies`, `JobRun.replies`.
- Produces:
  - `board.ADDRESS_ACTIONS = frozenset({"answer_threads", "resolve_threads", "address_review"})`
  - `board.job_kind(action: str, address: bool) -> str | None` — `"review"`, `"recheck"`, `"address"` (only when `address`), else `None`.
  - `plan_jobs(..., posting=frozenset(), answered: Callable[[Job], frozenset[str]] = lambda job: frozenset())` — `answered(job)`: the thread ids of the job's replies that landed.
  - `jobs._turn_back(same, states) -> bool` — now returns whether it revived a stale job.
  - `ReviewerService._answered_threads(self, job: Job) -> frozenset[str]` — `thread_id` (when a `str`) of each `run.replies` entry whose `n` is in `store.posted_replies(job)`; empty without a run. Passed as `answered=`.

The address rule, in the row loop after the re-check block:

```python
        mine = [t for t in row.my_turns if t["action"] in ADDRESS_ACTIONS]
        if cfg.process.address_doc and not row.pipeline and "author" in row.roles and mine:
            addresses = [j for j in jobs if j.kind == "address"]
            current = {tid for t in mine for tid in t["threads"]}
            key = f"{head}:{','.join(sorted(current))}"
            same_key = [j for j in addresses if j.key == key]
            revived = _turn_back(same_key, states)
            covered = any(
                j.head_sha == head and j.state in _COVERING and current <= _threads_of(j) - answered(j)
                for j in addresses)
            failures = sum(j.state == "failed" for j in same_key)
            settled = datetime.fromisoformat(row.thread_turns_change or row.last_change)
            if (reviews_running and now - settled >= quiet
                    and not (covered or revived or failures >= _MAX_FAILURES)):
                for j in addresses:
                    if j.state in _RECHECK_SUPERSEDED:
                        states.setdefault(j.id, ("superseded", "new threads"))
                plan.queue.append((number, "address", head, key))
```

with `_COVERING = ("queued", "running", "deferred", "done", "posted")` and `_threads_of(job) -> set[str]` = the non-empty comma-separated ids after the first `:` of `job.key`.

Gone turns: `kind = job_kind(change.action, True)`; skip when `None`; for `address`, skip also when the PR's row still has an action in `ADDRESS_ACTIONS`; then the existing loop with `j.kind != kind`.
Turn lines: a new turn sends a line unless `job_kind(change.action, bool(cfg.process.address_doc) and not row.pipeline and change.number not in held)` is not `None` (a row a ticket workspace holds, or a held one, gets its address turn lines, as in a repo without `address_doc`). `plan_jobs`'s docstring: a PR in `held` gets no review, no re-check and no address job.
`settle`: an `address` job's turn is there when `ADDRESS_ACTIONS & set(row.my_actions)`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_reviewer_jobs.py` — give the `plan()` helper `cfg=CFG` and `answered=lambda j: frozenset()` keywords passed through, then:

```python
ADDR = dataclasses.replace(CFG, process=ProcessPartConfig(address_doc="docs/threads.md"))

def author_row(actions=(("answer_threads", ("T2", "T1")),), **kw):
    return dataclasses.replace(row(actions=actions, **kw), roles=["author"])

def test_address_waits_while_a_ticket_workspace_holds_the_pr():
    held = author_row(pipeline=True)
    p = plan([held], cfg=ADDR, new=[TurnChange(1, "answer_threads", ("T1", "T2"))])
    assert p.queue == [] and len(p.lines) == 1                       # no job; the turn line is sent
    done = job("address", key=f"{A}:T1,T2", state="done")
    assert plan([held], {1: [done]}, cfg=ADDR).states == []          # a job that ran before stays as it is
    assert plan([author_row()], cfg=ADDR).queue == [(1, "address", A, f"{A}:T1,T2")]   # the flag dropped

def test_address_skips_a_held_pr_and_sends_its_turn_line():      # one side of a comparison with the skill flow
    p = plan([author_row()], cfg=ADDR, held=frozenset({1}), new=[TurnChange(1, "answer_threads", ("T1", "T2"))])
    assert p.queue == [] and len(p.lines) == 1

def test_address_queued_for_the_author_after_the_quiet_window():
    assert plan([author_row()], cfg=ADDR).queue == [(1, "address", A, f"{A}:T1,T2")]
    assert plan([author_row()]).queue == []                                           # no address_doc
    assert plan([row(actions=(("answer_threads", ("T1",)),))], cfg=ADDR).queue == []  # not the author
    assert plan([dataclasses.replace(author_row(), thread_turns_change=RECENT)], cfg=ADDR).queue == []
    assert plan([author_row()], cfg=ADDR, running=False).queue == []

def test_address_key_joins_the_threads_of_all_three_actions():
    r = author_row(actions=(("answer_threads", ("T2",)), ("resolve_threads", ("T1",)), ("address_review", ())))
    assert plan([r], cfg=ADDR).queue == [(1, "address", A, f"{A}:T1,T2")]
    assert plan([author_row(actions=(("address_review", ()),))], cfg=ADDR).queue == [(1, "address", A, f"{A}:")]

def test_address_not_queued_while_a_job_covers_the_turn():
    done = job("address", key=f"{A}:T1,T2,T3", state="done")
    for state in ("queued", "running", "deferred", "done", "posted"):
        assert plan([author_row()], {1: [dataclasses.replace(done, state=state)]}, cfg=ADDR).queue == []
    back = plan([author_row()], {1: [dataclasses.replace(done, state="posted")]}, cfg=ADDR,
                answered=lambda j: frozenset({"T2"}))      # T2's reply landed and T2 is my turn again
    assert back.queue == [(1, "address", A, f"{A}:T1,T2")] and back.states == [("j1", "superseded", "new threads")]
    new_thread = author_row(actions=(("answer_threads", ("T9",)),))
    assert plan([new_thread], {1: [done]}, cfg=ADDR).queue == [(1, "address", A, f"{A}:T9")]
    assert plan([author_row(head=B)], {1: [done]}, cfg=ADDR).queue == [(1, "address", B, f"{B}:T1,T2")]

def test_address_two_failures_stop():
    f = job("address", key=f"{A}:T1,T2", state="failed")
    assert plan([author_row()], {1: [f]}, cfg=ADDR).queue == [(1, "address", A, f"{A}:T1,T2")]
    assert plan([author_row()], {1: [f, dataclasses.replace(f, id="j2")]}, cfg=ADDR).queue == []

def test_address_turn_back_revives_instead_of_queueing():
    stale = job("address", key=f"{A}:T1,T2", state="stale")
    p = plan([author_row()], {1: [stale]}, cfg=ADDR)
    assert p.queue == [] and p.states == [("j1", "done", "turn back")]

def test_gone_address_turn_cancels_and_stales():
    q = job("address", key=f"{A}:T1", state="queued", jid="q")
    d = job("address", key=f"{A}:T1", state="done", jid="d")
    gone = [TurnChange(1, "answer_threads", ("T1",))]
    p = plan([author_row(actions=(("merge", ()),))], {1: [q, d]}, cfg=ADDR, gone=gone)
    assert sorted(p.states) == [("d", "stale", "turn gone"), ("q", "cancelled", "turn gone")]
    still = author_row(actions=(("resolve_threads", ("T1",)),))      # another address action is left
    assert plan([still], {1: [q, d]}, cfg=ADDR, gone=gone).states == []

def test_address_actions_send_no_turn_line_where_address_is_on():
    r = author_row(actions=(("merge", ()), ("answer_threads", ("T1",))))
    new = [TurnChange(1, "merge", ()), TurnChange(1, "answer_threads", ("T1",))]
    assert plan([r], new=new, cfg=ADDR).lines == [
        "[acme/app] #1: your turn — merge · https://github.com/acme-org/acme-app/pull/1"]
    assert len(plan([r], new=new).lines) == 2
```

`tests/unit/test_reviewer_runner.py`:

```python
def test_settle_address_needs_one_of_its_turns():
    j = Job("a1", SCOPE, 1712, "address", HEAD, f"{HEAD}:T1", "running", "", "t", "t")
    assert settle(j, board_row(actions=("resolve_threads",)), [j]) == ("done", "")
    assert settle(j, board_row(actions=("merge",)), [j]) == ("stale", "turn gone")
```

`tests/unit/test_reviewer_service.py` (its rig): a job with `run.replies = [{"n": 1, "thread_id": "T1", "body": "x"}, {"n": 2, "thread_id": "T2", "body": "y"}]` and a landed replies post for `n == 1` gives `service._answered_threads(job) == frozenset({"T1"})`.

- [ ] **Step 2: Run the three files, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the three files, expect PASS; every pre-existing test in them passes unchanged**
- [ ] **Step 5: Commit** — `feat(reviewer): queue address jobs for the operator's own PRs`

---

### Task 3: The address session — prompt and sandbox

**Files:**
- Modify: `orchestrator/reviewer/session.py`
- Test: `tests/unit/test_reviewer_session.py`

**Interfaces — Produces:**
- `_writable_dirs("address", paths) == [paths.run_dir, paths.replies_plan.parent, paths.worktree]`.
- `settings_json(..., kind="address")`: `sandbox.filesystem.denyWrite` is the clone's `/**` rule and the rule path of `paths.worktree / ".git"` (the worktree's `/**` rule is not in it); `allowWrite` holds the three writable dirs; `permissions.deny` also holds `Edit(<rule path of worktree/.git>)`. For `review` and `recheck` the output is byte-identical to today's.
- `address_prompt(doc: str, number: int, paths: JobPaths, me: str, instruction: str) -> str`:

```python
def address_prompt(doc: str, number: int, paths: JobPaths, me: str, instruction: str) -> str:
    return (
        f"{_PREAMBLE}\n"
        f"Follow {doc} (in the current directory, the repo's base branch) for PR #{number}, whose author is {me}.\n"
        f"Its step that fetches the threads is done: they are in {paths.run_dir}/threads.json. Its step that picks "
        f"where to fix is done: the PR branch is checked out at {paths.worktree}. Make every fix there with the "
        "Write and Edit tools and commit nothing. You cannot delete or rename a file, and nothing can be built or "
        "tested here (no build tool, no network): skip those steps and never say a test ran.\n"
        "\n"
        "Do not show, ask or post anything. Instead of the document's steps that show results and post, write the "
        f"replies plan to {paths.replies_plan}: a JSON list with one entry per thread or review body that waits on "
        f"{me} — n (1, 2, …), thread_id or review_id, body (English), resolve — and \"fixed\": true on every entry "
        "whose reply reports a fix, with the literal text {fix_sha} in its body where the commit SHA goes. Never "
        "put fix_sha in an entry.\n"
        f"Also write {paths.run_dir}/tg_lines.json: a JSON object mapping every entry's n to one English line: "
        "the document's verdict · path:line · gist.\n"
        f"{_AGENTS_AND_BASH}"
        + (f"The operator's instruction for this run: {instruction}\n" if instruction else "")
        + f"{_DATA}"
    )
```

- [ ] **Step 1: Write the failing tests** (fixtures `clone`, `paths`, helper `rule_path` exist in the file)

```python
def test_address_session_may_write_the_worktree_but_not_its_git_pointer(clone, paths):
    tools = allowed_tools("address", clone, paths, [])
    assert f"Edit({rule_path(paths.worktree)}/**)" in tools
    s = settings_json([], clone, paths, "address")
    fs = s["sandbox"]["filesystem"]
    assert f"{rule_path(paths.worktree)}/**" in fs["allowWrite"]
    assert fs["denyWrite"] == [f"{rule_path(clone.resolve())}/**", rule_path(paths.worktree / ".git")]
    assert f"Edit({rule_path(paths.worktree / '.git')})" in s["permissions"]["deny"]
    assert s["sandbox"]["network"] == {"allowedDomains": [], "strictAllowlist": True}

def test_review_and_recheck_settings_are_unchanged(clone, paths):
    for kind in ("review", "recheck"):
        fs = settings_json([], clone, paths, kind)["sandbox"]["filesystem"]
        assert fs["denyWrite"] == [f"{rule_path(d)}/**" for d in (clone.resolve(), paths.worktree)]

def test_address_prompt(paths):
    p = address_prompt("docs/threads.md", 7, paths, "octo", "leave 2 as it is")
    assert "Follow docs/threads.md" in p and str(paths.worktree) in p and str(paths.replies_plan) in p
    assert "{fix_sha}" in p and "Never put fix_sha in an entry." in p and "commit nothing" in p
    assert "The operator's instruction for this run: leave 2 as it is\n" in p
    assert "operator's instruction" not in address_prompt("docs/threads.md", 7, paths, "octo", "")
```

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run `tests/unit/test_reviewer_session.py -q`, expect PASS**
- [ ] **Step 5: Commit** — `feat(reviewer): the address session's prompt and writable worktree`

---

### Task 4: Capturing the fix as a patch

**Files:**
- Create: `orchestrator/reviewer/fix.py`
- Test: `tests/unit/test_reviewer_fix.py`

**Interfaces — Produces:**

```python
PATCH_NAME = "fix.patch"
MAX_PATCH_BYTES = 1_000_000

class FixError(Exception): ...          # its text is the job's failure reason

async def capture_fix(worktree: Path, patch: Path, protected: list[str], *, timeout: float = 300.0) -> dict:
    """{} when the worktree is unchanged (no file written), else
    {"files": [{"path": str, "added": int, "deleted": int}], "protected": [str], "bytes": int}."""

def fix_summary(fix: dict) -> tuple[int, int, int]:      # files, added, deleted
```

`capture_fix` runs three git commands through `run_command` with `cwd=worktree.parent` and `env={**base_env(), **NO_HOST_GIT_ENV}`, each as
`["git", "-C", str(worktree), "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.symlinks=false", <subcommand…>]`:
1. `add -A`
2. `diff --cached --numstat -z --no-renames HEAD` → the file list (a binary file's `-` counts are 0);
3. only when the list is not empty: `diff --cached --binary --no-ext-diff --no-textconv --no-color --output=<patch> HEAD`.
A failed or timed-out command raises `FixError("git <subcommand> failed: <stderr tail>")`. A patch over `MAX_PATCH_BYTES` is deleted and raises `FixError("the fix is too large: <n> bytes")`. `protected` patterns are matched with `fnmatch.fnmatchcase` against each path.

- [ ] **Step 1: Write the failing tests** (a module-level `git(*args, cwd)` helper and a `repo` fixture: a repo in `tmp_path` with one commit holding `a.txt`, `.github/ci.yml` and a symlink `link -> a.txt`; `wt` = `git -c core.symlinks=false worktree add --detach <tmp>/wt HEAD`)

```python
async def test_capture_and_apply_roundtrip_with_a_binary_file(repo, wt, tmp_path):
    (wt / "a.txt").write_text("changed\n"); (wt / "new.bin").write_bytes(b"\x00\xff\xfe binary")
    fix = await capture_fix(wt, tmp_path / "fix.patch", [".github/**"])
    assert sorted(f["path"] for f in fix["files"]) == ["a.txt", "new.bin"] and fix["protected"] == []
    assert fix["bytes"] == (tmp_path / "fix.patch").stat().st_size > 0
    other = tmp_path / "other"; git("worktree", "add", "--detach", str(other), "HEAD", cwd=repo)
    git("apply", "--index", "--binary", str(tmp_path / "fix.patch"), cwd=other)
    assert (other / "new.bin").read_bytes() == b"\x00\xff\xfe binary" and (other / "a.txt").read_text() == "changed\n"

async def test_capture_without_changes_writes_nothing(wt, tmp_path):
    assert await capture_fix(wt, tmp_path / "fix.patch", []) == {} and not (tmp_path / "fix.patch").exists()

async def test_capture_ignores_plain_file_symlinks(wt, tmp_path):   # `link` is a plain file in wt
    (wt / "a.txt").write_text("changed\n")
    assert [f["path"] for f in (await capture_fix(wt, tmp_path / "p", []))["files"]] == ["a.txt"]

async def test_capture_lists_protected_paths(wt, tmp_path):
    (wt / ".github" / "ci.yml").write_text("evil\n")
    assert (await capture_fix(wt, tmp_path / "p", [".github/**"]))["protected"] == [".github/ci.yml"]

async def test_capture_runs_no_planted_hook(repo, wt, tmp_path):
    hook = repo / ".git" / "hooks" / "post-index-change"
    hook.write_text(f"#!/bin/sh\ntouch {tmp_path / 'ran'}\n"); hook.chmod(0o755)
    (wt / "a.txt").write_text("changed\n")
    await capture_fix(wt, tmp_path / "p", [])
    assert not (tmp_path / "ran").exists()

async def test_capture_refuses_a_huge_patch(wt, tmp_path):
    (wt / "big.txt").write_text("x\n" * 600_000)
    with pytest.raises(FixError, match="too large"):
        await capture_fix(wt, tmp_path / "p", [])
    assert not (tmp_path / "p").exists()

def test_fix_summary():
    assert fix_summary({"files": [{"path": "a", "added": 3, "deleted": 1}, {"path": "b", "added": 0, "deleted": 0}]}) == (2, 3, 1)
    assert fix_summary({}) == (0, 0, 0)
```

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError`)
- [ ] **Step 3: Implement `fix.py`**
- [ ] **Step 4: Run `tests/unit/test_reviewer_fix.py -q`, expect PASS**
- [ ] **Step 5: Commit** — `feat(reviewer): capture an address session's changes as one patch`

---

### Task 5: The runner runs address jobs

**Files:**
- Modify: `orchestrator/reviewer/report.py` (new `parse_address_plan`)
- Modify: `orchestrator/reviewer/prepare.py::_prepare` (one check)
- Modify: `orchestrator/reviewer/runner.py` (`_RESERVED`, `recover`, `next_job`, `_run`, `_session`, `_finish`, `_message`)
- Modify: `orchestrator/reviewer/messages.py` (new `address_message`)
- Modify: `orchestrator/reviewer/lines.py` (`_NAMES` gets `N_("address")`)
- Test: `tests/unit/test_reviewer_report.py`, `tests/unit/test_reviewer_prepare.py`, `tests/unit/test_reviewer_runner.py`, `tests/unit/test_reviewer_messages.py`

**Interfaces:**
- Consumes: `address_prompt` (Task 3), `capture_fix`, `PATCH_NAME`, `FixError`, `fix_summary` (Task 4), `JobRun.fix`, `JobRun.request` (Task 1; `request["instruction"]` is written by Task 7).
- Produces:
  - `report.parse_address_plan(text: str) -> list[dict]` — `parse_replies_plan`, then `ReportError` for: an entry with the key `fix_sha` (`"#<n> carries fix_sha"`); `fixed` present and not a bool (`"#<n>: fixed is not true or false"`); `fixed` true without `{fix_sha}` in `body` (`"#<n> is fixed without {fix_sha} in its body"`); `{fix_sha}` in a body that is not `fixed` (`"#<n> names {fix_sha} but is not fixed"`).
  - `prepare._prepare`: for `job.kind == "address"`, right after the existing `has_file(doc)` check: `if not ctx.clone.has_file(process.address_doc): raise PrepareError("failed", f"{process.address_doc} not on {base}")`.
  - `messages.address_message(scope, number, title, url, head_sha, replies, tg_lines, fix) -> str` — exactly the spec's message: line 1 `tr('[{scope}] Threads on your PR #{number} "{title}"')`; line 2 `head {head9} · {replies} · ` (`count(len(replies), "drafted reply", "drafted replies")`, as `recheck_message`) + one of `fix: {files}, +{added} −{deleted} — not built or tested here` (`files` = `count(n, "file")`; add the noun to `tg_uk.NOUNS`), `no code change`; when `fix["protected"]`, an extra line `Protected files in the fix ({paths}): Cleave will not push it.`; one line per entry as in `recheck_message`; the hint `Reply to this message: "push" · "post replies {numbers}" · "text {first}" · "diff" · "again: …"` (without `"push" · ` and `"diff" · ` when there is no patch); the URL.
  - Runner: `_run` carries `request=previous.request` into the new `JobRun`; `_session` builds `address_prompt(process.address_doc, job.number, paths, me, run.request.get("instruction", ""))` for an address job and adds `paths.worktree / ".git"` (its bytes, `None` when missing) to the `saved` guarded files; `_finish` for an address job copies the plan as for a re-check, parses it with `parse_address_plan`, then `run.fix = await capture_fix(paths.worktree, paths.job_dir / PATCH_NAME, ctx.cfg.process.address_protected)` (a `FixError` fails the job with its text), and `_message` uses `address_message`. `_RESERVED` gains `PATCH_NAME`. `recover()` and the "review first / login recorded" gate (`_recheck_ready`) cover `address` as they cover `recheck`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_reviewer_report.py
def test_parse_address_plan():
    ok = '[{"n": 1, "thread_id": "T1", "body": "Fixed in {fix_sha}: x.", "resolve": true, "fixed": true},' \
         ' {"n": 2, "thread_id": "T2", "body": "Intended.", "resolve": false}]'
    assert [e["n"] for e in parse_address_plan(ok)] == [1, 2]
    for bad, why in (
        ('[{"n": 1, "thread_id": "T1", "body": "x", "fix_sha": "abc"}]', "#1 carries fix_sha"),
        ('[{"n": 1, "thread_id": "T1", "body": "Fixed.", "fixed": true}]', "#1 is fixed without {fix_sha} in its body"),
        ('[{"n": 1, "thread_id": "T1", "body": "In {fix_sha}."}]', "#1 names {fix_sha} but is not fixed"),
        ('[{"n": 1, "thread_id": "T1", "body": "x", "fixed": "yes"}]', "#1: fixed is not true or false"),
    ):
        with pytest.raises(ReportError, match=re.escape(why)):
            parse_address_plan(bad)

# tests/unit/test_reviewer_messages.py
def test_address_message():
    replies = [{"n": 1, "thread_id": "T1", "body": "Fixed in {fix_sha}: x.", "resolve": True, "fixed": True},
               {"n": 2, "thread_id": "T2", "body": "Intended.", "resolve": False}]
    fix = {"files": [{"path": "a.kt", "added": 24, "deleted": 7}], "protected": [], "bytes": 900}
    text = address_message("acme/app", 1712, "Fix cart sync", URL, "8f01a2b3c" + "0" * 31, replies,
                           {1: "FIXED · CartRepo.kt:88 · null list handled"}, fix)
    assert text.splitlines()[:2] == ['[acme/app] Threads on your PR #1712 "Fix cart sync"',
        "head 8f01a2b3c · 2 drafted replies · fix: 1 file, +24 −7 — not built or tested here"]
    assert "1. FIXED · CartRepo.kt:88 · null list handled" in text and "2. Intended." in text
    assert 'Reply to this message: "push" · "post replies 1 2" · "text 1" · "diff" · "again: …"' in text
    none = address_message("acme/app", 1712, "t", URL, "8f01a2b3c" + "0" * 31, replies, {}, {})
    assert "no code change" in none and '"push"' not in none and '"diff"' not in none
    guarded = address_message("acme/app", 1712, "t", URL, "8" * 40, replies, {}, {**fix, "protected": [".github/ci.yml"]})
    assert "Protected files in the fix (.github/ci.yml): Cleave will not push it." in guarded
```

`tests/unit/test_reviewer_runner.py` (the file's `Rig`; give `CFG`'s process `address_doc="docs/threads.md"`, `JOBS["address"]`, and let the fake session write the plan and edit a file in `paths.worktree`; `capture_fix` is monkeypatched to a fake returning a fix dict, since `FakeClone` has no real git):

```python
async def test_address_job_keeps_plan_fix_and_sends_the_message(rig):
    # board row with roles ["author"], actions ("answer_threads",); job = rig.add_job("address", key=f"{HEAD}:T1")
    # after rig.runner.run_job(job): rig.state(job) == "done"; run.replies == the plan; run.fix == the fake's dict;
    # (rig.job_dir(job) / "replies_plan.json").is_file(); the chat's last text starts with
    # '[acme/app] Threads on your PR #1712'; the prompt the fake session got contains "docs/threads.md"

async def test_address_prompt_carries_the_operators_instruction(rig):
    # store.save_run(JobRun(job.id, request={"instruction": "leave 2 as it is"})) before run_job:
    # the session's prompt contains "The operator's instruction for this run: leave 2 as it is"

async def test_address_job_fails_when_the_worktree_git_pointer_changed(rig):
    # the fake session rewrites paths.worktree / ".git": state "failed", reason == runner._CONFIG_CHANGED,
    # and capture_fix was never called

async def test_address_job_fails_on_an_unreadable_plan_or_a_fix_error(rig):
    # plan with "fix_sha" -> failed, reason "report unreadable: #1 carries fix_sha"
    # capture_fix raising FixError("the fix is too large: 2000000 bytes") -> failed with that reason
```

`tests/unit/test_reviewer_prepare.py`: an address job whose `address_doc` is missing on the base raises `PrepareError` with `outcome == "failed"` and `reason == "docs/threads.md not on develop"`; a review job in the same repo prepares as before.

- [ ] **Step 2: Run the four files, expect FAIL**
- [ ] **Step 3: Implement.** In `_session`, the comment "no git runs in the job clone after its session" is no longer true for an address job: state the exception and its guards (the unchanged guarded files, the worktree's `.git` pointer, hooks and fsmonitor off, no host config).
- [ ] **Step 4: Run the four files and `tests/integration/test_reviewer_job.py -q`, expect PASS**
- [ ] **Step 5: Commit** — `feat(reviewer): run address jobs and send their message`

---

### Task 6: Grammar — `push`, `diff`, `again`

**Files:**
- Modify: `orchestrator/reviewer/grammar.py`
- Modify: `orchestrator/reviewer/lines.py::not_understood_line`
- Test: `tests/unit/test_reviewer_grammar.py`

**Interfaces — Produces:**
- `ReplyCommand.action` gains `push`, `diff`, `again` (`again` carries `instruction`, possibly `""`).
- `_PHRASES["push"] = ("push", "commit and push", "пуш", "пушь", "запушь", "закоммить и запушь", "запуш")`, `_PHRASES["diff"] = ("diff", "patch", "дифф", "диф", "патч")`; each parses only as the whole reply.
- `_AGAIN = re.compile(r"^(again|redo|заново|переделай|наново)\s*(?::\s*(\S.*))?$", re.DOTALL | re.IGNORECASE)`, matched on the raw text after `_EDIT`.
- `check_command(cmd, kind, …)`:
  - `kind == "address"` and action in `post`, `edit`, `draft` → `tr('This is a threads message: reply "push", "post replies 1 3", "text 2", "diff" or "again: …". Nothing was done.')`;
  - action in `push`, `diff`, `again` and `kind != "address"` → `tr('"{action}" works on a threads message of your own PR. Nothing was done.', action=cmd.action)`;
  - everywhere the function branches on `kind == "recheck"` for the plan (`post_replies` allowed, numbers known from `replies`), `address` goes the same way.
- `describe`: `push` → `"push"`, `diff` → `"diff"`, `again` → `"again"` or `'again: "<instruction>"'`.
- `not_understood_line("address")` → `tr('Not understood; nothing was done. Reply like "push", "post replies 1 3", "text 2", "diff" or "again: …".')`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("text, command", [
    ("push", ReplyCommand("push")), ("Пуш.", ReplyCommand("push")), ("commit and push", ReplyCommand("push")),
    ("запушь", ReplyCommand("push")), ("diff", ReplyCommand("diff")), ("дифф", ReplyCommand("diff")),
    ("again", ReplyCommand("again")), ("заново: не трогай 2", ReplyCommand("again", instruction="не трогай 2")),
    ("Again: leave 2 as it is", ReplyCommand("again", instruction="leave 2 as it is")),
])
def test_address_grammar(text, command):
    assert parse_reply(text) == command

@pytest.mark.parametrize("text", ["push 1 2", "push anyway", "diff 3", "again 2"])
def test_address_grammar_unknown(text):
    assert parse_reply(text) is None

def test_check_command_on_an_address_message():
    replies = [{"n": 1, "thread_id": "T1", "body": "x"}]
    for cmd in (ReplyCommand("push"), ReplyCommand("diff"), ReplyCommand("again"), ReplyCommand("text", (1,)),
                ReplyCommand("post_replies", (1,))):
        assert check_command(cmd, "address", None, replies, set(), set()) == cmd
    assert "threads message" in check_command(ReplyCommand("post", (1,)), "address", None, replies, set(), set())
    for kind in ("review", "recheck"):
        assert check_command(ReplyCommand("push"), kind, None, replies, set(), set()) == \
            '"push" works on a threads message of your own PR. Nothing was done.'
```

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run `tests/unit/test_reviewer_grammar.py tests/unit/test_tg_i18n.py -q`, expect PASS**
- [ ] **Step 5: Commit** — `feat(reviewer): push, diff and again in the reply grammar`

---

### Task 7: Reply routing — `text`, `diff`, `again`

**Files:**
- Modify: `orchestrator/reviewer/service.py` (new `address_again`)
- Modify: `orchestrator/reviewer/replies.py::_execute` (and `_job_reply`'s fallback line)
- Test: `tests/unit/test_reviewer_service.py`, `tests/unit/test_reviewer_replies.py`

**Interfaces:**
- Consumes: `ReplyCommand` actions (Task 6), `PATCH_NAME` (Task 4), `ReviewerStore.push_of` (Task 1), `Posting.push_fix` (Task 9 — until then `_execute` may call it through the `self._posting` attribute; the fake posting of the tests provides it).
- Produces:
  - `ReviewerService.address_again(self, scope: str, number: int, instruction: str) -> Job` — the row must exist (`LookupError` otherwise) and hold an action in `ADDRESS_ACTIONS` (`LookupError("no address turn")` otherwise); raises `ValueError("already queued")` when an address job of the PR is queued, running or deferred; marks the PR's address jobs in `done`, `posted`, `stale` as `superseded` / `"new address"`; adds a job with the key `plan_jobs` would give the row; saves `JobRun(job.id, request={"instruction": instruction})`.
  - `_execute`:
    - `text` on an address job → as on a re-check (`_reply_text`);
    - `diff` → the job copy's `fix.patch` read as UTF-8 with `errors="replace"`, sent in at most `_MAX_PARTS` parts of `MESSAGE_LIMIT`; when cut, the last part ends with `tr("Patch cut: the dashboard shows all of it.")`; no patch → `tr("This job changed no code: there is no patch.")`;
    - `again` → `tr("Queued a new run on #{number} at {head}.")`, or `tr("A run on #{number} is already queued.")` (`ValueError`), or `tr("#{number} has no threads waiting on you any more; nothing was done.")` (`LookupError`); refused with `_NO_LOGIN` when the claude login is failing;
    - `push` → refused with `_FORGE_FAILING` when the forge token is failing; else answers `tr("Checking before pushing.")` and tracks `self._posting.push_fix(ctx, job)`, answering its outcome's text.
  - `again` is checked in `ReviewerReplies._check` before `check_command`, like `review_again`: on an address job in any state but `superseded` or `stale` it runs (so a failed job's line takes it); in those two states it answers `outdated_line`; on a job of another kind it answers Task 6's `"again" works on a threads message…` line.
  - `_job_reply`: no LLM fallback for an address job (`not_understood_line("address")`).

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_reviewer_replies.py` `Rig`: `rig.add_job("address", 100)` with `run.replies = REPLIES`, a `fix.patch` in the job dir; `FakeService` gets `address_again`; `FakePosting` gets `push_fix`)

```python
async def test_diff_sends_the_patch(tmp_path):      # rig.reply("diff") -> rig.last() == the patch text
async def test_diff_is_cut_after_three_parts(tmp_path):   # a 20 000-char patch: 3 answers, the last ends with the cut note
async def test_diff_without_a_patch(tmp_path):      # "This job changed no code: there is no patch."
async def test_again_queues_with_the_instruction(tmp_path):
    # rig.reply("again: leave 2 as it is") -> service.calls == [("address_again", SCOPE, N, "leave 2 as it is")]
    # and rig.last() == f"Queued a new run on #{N} at {HEAD[:9]}."
async def test_push_runs_the_push_and_answers_its_outcome(tmp_path):
    # FakePosting.push_fix returns PostOutcome("posted", "Pushed …"): answers are ["Checking before pushing.", "Pushed …"]
async def test_push_refused_while_the_forge_token_fails(tmp_path):   # failing=("forge:acme/app",)
async def test_free_text_on_an_address_message_is_not_parsed_by_the_model(tmp_path):
    # parser=(ReplyCommand("push"),): rig.reply("давай пуш уже") -> not_understood_line("address"); parser never called
async def test_again_works_on_a_failed_address_job_and_not_on_a_review(tmp_path):
    # failed address job: rig.reply("again") queues; superseded one: outdated_line; a review job's message:
    # '"again" works on a threads message of your own PR. Nothing was done.'
```

`tests/unit/test_reviewer_service.py`: `address_again` supersedes a done address job, returns a queued job with the row's key, stores the instruction in its run; a second call raises `ValueError`; a row without an address action raises `LookupError`.

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the two files, expect PASS**
- [ ] **Step 5: Commit** — `feat(reviewer): text, diff and again on an address message`

---

### Task 8: `ReviewClone.push_patch`

**Files:**
- Modify: `orchestrator/reviewer/clone.py`
- Test: `tests/unit/test_reviewer_clone.py`

**Interfaces — Produces:**

```python
    async def push_patch(self, *, number: int, head: str, branch: str, patch: Path, message: str,
                         author: tuple[str, str], worktree: Path) -> str:
        """Commit `patch` on the PR's head in a worktree at `worktree` and push it to `branch`; the new sha.
        The caller holds `lock`. Never forces: a branch that moved makes the push fail."""
```

Steps, each through `self._git` (so with the token header and no host config): `fetch_pr_head(number)` must return `head` (else `CloneError("head moved")`); `add_worktree(worktree, head)`; `-C <worktree> -c core.symlinks=false apply --index --binary <patch>`; `-C <worktree> -c core.symlinks=false -c user.name=<name> -c user.email=<email> commit --no-verify -m <message>`; `-C <worktree> rev-parse HEAD` (must be a full 40-hex id); `-C <self.path> push --no-signed --recurse-submodules=no origin <sha>^{commit}:refs/heads/<branch>` (the peeled full id, as `orchestrator/skillflow/checkout.py::push` does: a ref named like the sha cannot stand in for it). No step passes `--force`, `-f` or a `+` refspec. The method does not remove the worktree: the caller removes its directory.

- [ ] **Step 1: Write the failing tests** (fixtures `origin`, `pr7`, `refreshed`, helpers `git`, `commit_and_push` exist; build the patch with `git diff --binary` in a scratch checkout of the PR head)

```python
async def test_push_patch_adds_one_commit_to_the_branch(tmp_path, origin, pr7):
    # sha = await clone.push_patch(number=7, head=HEAD7, branch="feature/pr7", patch=p, message="fix: address review comments on #7",
    #                              author=("Cleave Bot", "cleave@pipeline.local"), worktree=tmp_path / "push" / "wt")
    # git("rev-parse", "feature/pr7", cwd=origin) == sha; git("rev-parse", f"{sha}^", cwd=origin) == HEAD7
    # git("log", "-1", "--format=%an <%ae>|%s", sha, cwd=origin) == "Cleave Bot <cleave@pipeline.local>|fix: address review comments on #7"
    # the commit's diff equals the patch's change

async def test_push_patch_refuses_a_moved_head(tmp_path, origin, pr7):
    # another commit pushed to the PR branch first: CloneError matching "head moved"; origin's branch unchanged

async def test_push_patch_never_forces(tmp_path, origin, pr7, monkeypatch):
    # record every argv given to run_command: none holds "--force", "-f", or an element starting with "+"

async def test_push_patch_runs_no_planted_hook(tmp_path, origin, pr7):
    # pre-commit and pre-push hooks planted in the review clone's .git/hooks touch a marker: it is never created
```

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run `tests/unit/test_reviewer_clone.py -q`, expect PASS**
- [ ] **Step 5: Commit** — `feat(reviewer): commit a patch on the PR head and push it without force`

---

### Task 9: `Posting.push_fix`

**Files:**
- Modify: `orchestrator/reviewer/posting.py` (new method beside `post_replies`; `__init__` gets `work_dir`)
- Modify: `orchestrator/reviewer/lines.py` (new lines; `interrupted_line` for `kind == "push"`)
- Modify: `main.py` (`Posting(..., work_dir=reviewer_data / "work")`)
- Test: Create `tests/unit/test_reviewer_push.py`

**Interfaces:**
- Consumes: `push_patch` (Task 8), `PATCH_NAME` (Task 4), `JobRun.fix`, `Post.sha`, `push_of`, `rehead_job`, `PRFull.head_ref/head_repo`, `RepoContext.commit_author` (Task 1).
- Produces: `Posting.__init__(..., work_dir: Path | None = None)` and

```python
    async def push_fix(self, ctx: RepoContext, job: Job) -> PostOutcome:
        """The address job's patch committed on the PR head and pushed, under the posting lock; never raises
        but a cancellation, which leaves the Post `posting` for the restart's recover()."""
```

Order and exact texts (all through `tr()`; `PostOutcome("refused", text)` unless said otherwise):
1. `job = self._current(job.id)`; `run.fix` empty → `"This job changed no code: nothing to push."`
2. `run.fix["protected"]` → `"The fix touches protected files ({paths}): Cleave does not push it. Nothing was done."`
3. `self._store.push_of(job)` → `"Already pushed as {sha}. Nothing was done."` (9 characters)
4. `self._store.board(job.scope).get(job.number)` is a row with `pipeline` set → `"A ticket of the pipeline still holds #{number}'s branch and may overwrite it: nothing pushed. Reply \"push\" again when the ticket is done."` (no GitHub read before this check)
5. The job copy's `fix.patch` is missing or a symlink → `not_pushed_line(number, "the patch is not in this job's copy")` = `"Not pushed to #{number}: {why}."`
6. `pr = await self._read(number, ctx.forge.pr_full(number))`; closed → `pr_closed_line`; `pr.head_sha != job.head_sha` → `'Head moved ({old} → {new}): nothing pushed. Reply "again" for a new run on the new head.'`
7. `pr.head_repo != ctx.readiness.repo` → `"The PR's branch is in a fork ({repo}): Cleave does not push there. Nothing was done."`
8. `pr.head_ref == ctx.cfg.base_branch`, or not `re.fullmatch(r"[A-Za-z0-9._/-]+", pr.head_ref)`, or it starts with `-` → `"Cleave does not push to the branch {branch}. Nothing was done."`
9. `post = self._start(job, ReplyCommand("push"), "push")`; under `ctx.clone.reader(wait=self._reader_wait)`: `sha = await ctx.clone.push_patch(number=…, head=job.head_sha, branch=pr.head_ref, patch=<job copy>/fix.patch, message=<below>, author=ctx.commit_author, worktree=<work_dir>/push/<post.id>/wt)`; `finally` remove `<work_dir>/push/<post.id>` in a thread. A `CloneError` ends the post `failed` → `PostOutcome("failed", not_pushed_line(number, str(e)))`.
   Message: `f"fix({run.ticket_key}): address review comments on #{number}"`, or `f"fix: address review comments on #{number}"` without a ticket key.
10. `after = await ctx.forge.pr_full(number)`; a read error or `after.head_sha != sha` ends the post `unverified` (with `sha` stored) → `PostOutcome("unverified", "The push to #{number} ran, but GitHub does not show {sha} as the head. Check the PR's commits before pushing again: {url}")`.
11. Else: `self._store.rehead_job(job.id, sha, f"{sha}:{job.key.partition(':')[2]}", stamp)`; the post ends `posted` with `sha` and `urls=[f"https://github.com/{repo}/commit/{sha}"]` → `PostOutcome("posted", pushed_line(...))` = `"Pushed {sha} to #{number}: {url}"` plus, when the plan has fixed entries not posted yet, `'\nReply "post replies {numbers}" to answer the fixed threads.'`
- `interrupted_line(scope, number, "push")` → `'[{scope}] #{number}: a push was interrupted by a restart. Check the PR\'s commits, then reply "push" again if the fix is not there.'`

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_reviewer_push.py`: a small rig — `ReviewerStore(connect(tmp_path / "rv.db"))`, an address job in `done` with a run whose `job_dir` holds `fix.patch`, `fix={"files": [...], "protected": [], "bytes": 10}`, `replies` with one fixed entry; `FakeClone` with `lock`, `reader()` and a `push_patch` that records its keywords and returns `NEW`; `FakeForge.pr_full` answering from a list)

```python
async def test_push_records_the_post_and_moves_the_job(rig):
    out = await rig.push()
    assert out.state == "posted" and out.text == (
        f"Pushed {NEW[:9]} to #1712: https://github.com/acme-org/acme-app/commit/{NEW}\n"
        'Reply "post replies 1" to answer the fixed threads.')
    job = rig.store.job(rig.job.id)
    assert (job.head_sha, job.key) == (NEW, f"{NEW}:T1")
    post = rig.store.push_of(job)
    assert (post.kind, post.state, post.head_sha, post.sha) == ("push", "posted", HEAD, NEW)
    assert rig.clone.pushed == [{"number": 1712, "head": HEAD, "branch": "feature/x", "message":
        "fix(ACME-1234): address review comments on #1712", "author": ("Cleave Bot", "cleave@pipeline.local")}]
    assert not (rig.work / "push").exists() or not any((rig.work / "push").iterdir())

async def test_second_push_is_refused(rig):
    await rig.push()
    assert (await rig.push()).text == f"Already pushed as {NEW[:9]}. Nothing was done."
    assert len(rig.clone.pushed) == 1

async def test_push_refused_when_the_head_moved(rig):          # forge head OTHER: text names both heads; nothing pushed
async def test_push_refusals(rig):                             # no fix; protected; fork; base branch; branch "-x" and "a b"
async def test_push_refused_while_a_ticket_workspace_holds_the_pr(rig):
    # rig.store.replace_board(SCOPE, {1712: <the row with pipeline=True>}); out = await rig.push()
    # out.state == "refused"; out.text == "A ticket of the pipeline still holds #1712's branch and may overwrite it: "
    #   'nothing pushed. Reply "push" again when the ticket is done.'; rig.clone.pushed == [] and `FakeForge.pr_full` was not called
async def test_push_failure_is_reported_and_retryable(rig):    # push_patch raises CloneError("git push failed: rejected"):
    # outcome.state == "failed", text "Not pushed to #1712: git push failed: rejected."; job head unchanged; a second push runs
async def test_push_unverified_keeps_the_job_where_it_was(rig): # GitHub still shows HEAD after the push: state "unverified",
    # the post is "unverified" with sha NEW, the job's head is still HEAD
async def test_board_poll_after_a_push_queues_nothing():
    # jobs.plan_jobs with the row at NEW, threads ("T1",), the moved job (done, key f"{NEW}:T1"): queue == []
```

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run `tests/unit/test_reviewer_push.py tests/unit/test_reviewer_posting.py tests/unit/test_main_reviewer.py -q`, expect PASS**
- [ ] **Step 5: Commit** — `feat(reviewer): push an address job's fix on the operator's word`

---

### Task 10: Posting an address job's replies

**Files:**
- Modify: `orchestrator/reviewer/posting.py` (`_post_replies`, `_write_plan`, `_PLAN_KEYS`)
- Test: `tests/unit/test_reviewer_posting_replies.py`

**Interfaces:**
- Consumes: `push_of` (Task 1), `JobRun.fix`, the `fixed` flag (Task 5).
- Produces, in `_post_replies` for `job.kind == "address"` only:
  - the prepared snapshot is checked against `prepared_head = push.head_sha if (push := self._store.push_of(job)) else job.head_sha` (`_other_pr(prepared, number, prepared_head)`); everything read from GitHub uses `job.head_sha` as today;
  - after `validate_plan` (on the model's entries, unchanged): a named entry with `fixed is True` while `run.fix` is not empty and there is no landed push → `_Refused(tr('#{n} reports a fix that is not pushed: reply "push" first. Nothing was done.', n=n))`;
  - each named fixed entry is replaced by a copy whose `body` has `{fix_sha}` replaced by `job.head_sha[:9]`; `fix_shas = {n: job.head_sha}` for those entries;
  - `_write_plan(job_dir, head9, entries, number, fix_shas: dict[int, str] | None = None)` adds `"fix_sha": fix_shas[n]` to those plan entries. The entries passed on to the dry-run check, `_not_postable_now` and the verification never carry `fix_sha`, so `validate_plan`'s refusal still catches a model-written one.
  - a body problem of an address reply answers a new `lines.address_body_problem_line(number, problem)` = `tr('#{number}: its text {problem}: nothing posted. Reply "again: …" to have it rewritten.')` instead of `recheck_body_problem_line`; the check runs on the body after the `{fix_sha}` replacement.
  - Re-check jobs: no change in behaviour or output.

- [ ] **Step 1: Write the failing tests** (the file's `Rig`: add a `kind="recheck"` keyword that sets the job's kind, and `fix=` / `push=` keywords that set `run.fix` and save a landed push post with `head_sha=HEAD`, `sha=NEW` after `rehead_job`)

```python
FIXED_PLAN = [{"n": 1, "thread_id": "T1", "comment_id": 101, "body": "Fixed in {fix_sha}: null check.",
               "resolve": True, "fixed": True},
              {"n": 2, "thread_id": "T2", "body": "Intended.", "resolve": False}]

async def test_fixed_reply_is_refused_before_the_push(tmp_path):
    rig = Rig(tmp_path, kind="address", plan=FIXED_PLAN, fix={"files": [{"path": "a", "added": 1, "deleted": 0}]})
    out = await rig.post(ReplyCommand("post_replies", (1,)))
    assert out.text == '#1 reports a fix that is not pushed: reply "push" first. Nothing was done.' and rig.confirms() == []

async def test_unfixed_reply_posts_before_the_push(tmp_path):      # numbers (2,): posted; the written plan has no fix_sha

async def test_fixed_reply_posts_after_the_push(tmp_path):
    # push=NEW, heads=(NEW,), dry/confirm steps carrying "Fixed in <NEW[:9]>: null check."
    # written = json.loads((job_dir / "posting" / f"replies_{NEW[:9]}.json").read_text())
    # written == [{"n": 1, "thread_id": "T1", "comment_id": 101, "body": f"Fixed in {NEW[:9]}: null check.",
    #              "resolve": True, "fix_sha": NEW}]
    # outcome.state == "posted"; Prepare's snapshot (head HEAD) was accepted

async def test_fixed_reply_without_a_patch_uses_the_jobs_head(tmp_path):   # fix={} : fix_sha == HEAD, no push needed

async def test_a_model_written_fix_sha_is_still_refused(tmp_path):
    # plan entry with "fix_sha": "abc": "#1 cannot be posted: carries fix_sha. Nothing was done."
```

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run `tests/unit/test_reviewer_posting_replies.py tests/integration/test_reviewer_posting.py -q`, expect PASS (every pre-existing test unchanged)**
- [ ] **Step 5: Commit** — `feat(reviewer): post an address job's replies with Cleave's fix_sha`

---

### Task 11: Dashboard

**Files:**
- Modify: `orchestrator/reviewer/service.py` (`overview`, `detail`, `_job_detail`, `_has_plan`'s docstring)
- Modify: `dashboard/static/js/reviews.js`
- Test: `tests/unit/test_reviewer_service.py`, `tests/unit/test_dashboard_reviews.py`

**Interfaces — Produces:**
- `overview()`: wherever it picks a PR's job by `kind in ("review", "recheck")`, `address` counts too.
- `detail()`: `replies_job` / `replies` / `reply_states` come from the newest job of kind `recheck` **or** `address` that has a plan; new keys `"fix"` (that job's `run.fix` or `None`), `"fix_patch"` (the job copy's `fix.patch`, at most `_MAX_REPORT_TEXT` characters, `None` without one) and `"pushed_sha"` (`push_of(job).sha` or `None`) — all `None` when that job is a re-check.
- `reviews.js`: a done address job's label is `Fix ready` when `fix` is set, else `Replies ready`; the detail view shows `fix.files` as a list (`path +added −deleted`), the protected files, `Pushed <sha9>` and the patch in a `<pre>` (escaped with the file's `esc`). No new button.

- [ ] **Step 1: Write the failing tests** — `test_reviewer_service.py`: `detail()` of a PR with a done address job returns its replies, `fix`, `fix_patch` text and `pushed_sha is None`; after a landed push post, `pushed_sha == NEW`. `test_dashboard_reviews.py`: the detail route passes the three keys through.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run both files, expect PASS; open the dashboard against a fixture DB with one address job and check the detail view by eye (state what was checked in the commit body)**
- [ ] **Step 5: Commit** — `feat(dashboard): show an address job's fix and replies`

---

### Task 12: Documents and the live config example

**Files:**
- Modify: `docs/superpowers/specs/2026-11-12-reviewer-mode-design.md` — the two non-goals on author-side turns: add "(amended by `2026-11-18-reviewer-address-design.md`: `answer_threads`, `resolve_threads` and `address_review` run an address job where `process.address_doc` is set)"; the turn-lines example «тебе ответили в 3 тредах — обработай у себя» gets "(only without `address_doc`)".
- Modify: `docs/telegram.md` — the replies table: `push`, `diff`, `again: …`.
- Modify: `docs/superpowers/specs/2026-11-18-reviewer-owns-prs-design.md` — "Known gap (accepted)": add one line, "Closed where `process.address_doc` is set: see `2026-11-18-reviewer-address-design.md`, 'PRs the ticket pipeline opened'."
- Modify: the example repo config under `config/` or `tests/fixtures/config/` that shows a full `reviewer:` section (`grep -rn "recheck_section" --include="*.yaml" .`): add `address_doc` commented out with one line of explanation.

- [ ] **Step 1: Make the edits**
- [ ] **Step 2: Run the whole suite** `.venv/bin/python -m pytest tests -q`, expect PASS
- [ ] **Step 3: Commit** — `docs: address jobs`

---

## After merge (operator)

1. In the repo's config set `reviewer.process.address_doc` to the repo's author-side document and restart the daemon. Until then nothing changes.
2. The forge token must be allowed to push to PR branches of the repo (GitHub classic `repo` scope does).
3. First address message: check the patch with "diff" before the first "push".
4. A PR whose ticket is still open in the pipeline (a fix loop, blocked, failed) gets turn lines and no address job until the ticket is done or archived.
