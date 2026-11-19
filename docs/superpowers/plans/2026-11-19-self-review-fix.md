# Findings Jobs (Fix the Findings of a Self-Review) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new reviewer job kind `findings`: on the operator's word (panel button or Telegram "fix"), Claude weighs and fixes the confirmed findings of the operator's self-review of their own PR, as one patch pushed only on "push".

**Architecture:** A findings job is an address job with another input. It reuses the address job's sandbox, `capture_fix`, `push_fix` and lease push; it differs in what queues it (only `ReviewerService.fix_findings`), what Prepare writes (`findings.json` from the self-review's report), what the session writes (`verdicts.json`), its message, and its replies. A constant `FIX_KINDS = ("address", "findings")` replaces the `kind == "address"` checks that are about "a job whose session writes the worktree and whose patch is pushed".

**Tech Stack:** Python 3.12, Starlette, pytest (asyncio); vanilla ES modules, `node:test`.

**Spec:** `docs/superpowers/specs/2026-11-19-self-review-fix-design.md` — read it first; it is the authority. Its base: `docs/superpowers/specs/2026-11-18-reviewer-address-design.md`.

## Global Constraints

- **Interpreter:** a git worktree has no virtualenv. `export VENV_PY=<main checkout>/.venv/bin/python` and run every Python command as `$VENV_PY -m pytest …` from the worktree's root.
- **Whole suite:** `timeout 420 $VENV_PY -m pytest tests --ignore=tests/e2e -q -p no:cacheprovider > /tmp/suite.log 2>&1; grep -E '^FAILED|passed|failed' /tmp/suite.log | tail -15`. pytest does not exit after its summary line (exit 124 from `timeout` is expected). Exactly these 9 fail on an untouched master for environment reasons and are not yours: `tests/integration/test_e2e_dry_run.py` ×3, `tests/integration/test_jira_adapter.py::test_build_jql_ands_multiple_trigger_labels`, `tests/integration/test_project_create_flow.py::test_bad_payload_returns_400`, `tests/unit/test_health_validators.py::TestCheckGitIdentity::test_not_a_git_dir`, `tests/unit/test_main.py` ×3. Any other failure is yours.
- **JS tests:** `node --test 'tests/js/*.test.mjs'` (a directory argument does not work on Node 26).
- **Telegram texts:** every literal passed to `tr()` or `N_()` needs its Ukrainian entry in `orchestrator/tg_uk/reviewer.py` (`TEXTS`); a literal you stop using loses its entry; `tr()` never takes an f-string. `tests/unit/test_tg_i18n.py` enforces all three. Model-written text in a message goes through `model_text()`.
- **Public repository:** no real logins, company or repo names, ticket keys, PR numbers or local paths in code, tests, docs or commit messages. Use `acme/app`, `ACME-123`, `octo`, `octo-reviewer`.
- **Copy:** user-facing English texts are exactly the spec's where the spec gives them.
- **Scope:** do not restart any running Cleave daemon, do not push, do not touch `config-live/`.
- **Style:** match the surrounding code: dataclasses, docstrings that say what and why, no new dependencies. Docs (`docs/telegram.md`, `docs/dashboard.md`) are terse English in the existing voice.

## Review Focus

1. **GitHub lags a push** — right after "push" of a findings fix, the board still shows the old head: `plan_jobs` must not supersede the re-headed job (its landed push started from the row's head). Test in Task 1.
2. **"fix" on an outdated self-review message** — a newer self-review exists at the same head: the reply is refused ("not the newest self-review"), nothing queued. Tests in Tasks 2 and 4.
3. **Both fix kinds at one head** — an address job and a findings job of one PR: no second fix of either kind while one is pending; two unpushed patches may exist, and the second push is refused by the existing head check. Tests in Task 2 (queueing) and Task 1 (the old-head supersede after the first push).
4. **A self-review with only ❓ or 🟣 items** — not fixable (`no confirmed findings`): no button, no `· "fix"` in its message, "fix" refused. Tests in Tasks 2 and 3.
5. **Runs stored before this change** — a `JobRun` without `verdicts` or `findings_fixable` loads with the defaults and its message builds as before. Test in Task 1.

---

### Task 1: The findings job's data and rules

**Files:**
- Modify: `orchestrator/reviewer/board.py` (add `FIX_KINDS`)
- Modify: `orchestrator/reviewer/models.py` (`JobRun`: `verdicts`, `findings_fixable`)
- Modify: `config/schemas.py` (`_default_reviewer_jobs`: `findings`)
- Modify: `orchestrator/reviewer/report.py` (`SEVERITY`, `confirmed_findings`, `parse_findings_verdicts`)
- Modify: `orchestrator/reviewer/runner.py` (`settle`)
- Modify: `orchestrator/reviewer/jobs.py` (`plan_jobs`: the findings rule)
- Test: `tests/unit/test_reviewer_report.py`, `tests/unit/test_reviewer_runner.py`, `tests/unit/test_reviewer_jobs.py`, `tests/unit/test_reviewer_store.py`, `tests/unit/test_config_reviewer.py`

**Interfaces:**
- Produces:
  - `board.FIX_KINDS: tuple[str, ...] = ("address", "findings")` — kinds whose session writes the PR worktree and whose patch is pushed.
  - `JobRun.verdicts: list = []` (a findings job's parsed `verdicts.json`), `JobRun.findings_fixable: bool = False` (a review run: offer "fix" in its message).
  - `ReviewerGlobalConfig().jobs["findings"] == ReviewerJobConfig("opus", "xhigh", 5400)`.
  - `report.SEVERITY = {"red": "critical", "orange": "major", "yellow": "minor"}`.
  - `report.confirmed_findings(report: dict) -> list[dict]` — from a stored report dict (`Report.to_dict()` shape): one `{"n", "severity", "title", "path", "line", "body"}` per item whose kind is in `CONFIRMED`, in report order.
  - `report.parse_findings_verdicts(text: str, numbers: Collection[int]) -> list[dict]` — `[{"n": int, "verdict": str, "why": str}]`; raises `ReportError` on anything the spec's "verdicts.json is valid" rule refuses.
  - `settle(job, row, jobs)` handles kind `findings`.
  - `plan_jobs(...)` returns state changes for findings jobs (never queues one).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_reviewer_report.py`:

```python
def _report_dict(*kinds):
    items = [ReportItem(i, kind, f"t{i}", "a.kt", i * 10 if i != 2 else None, "", f"b{i}")
             for i, kind in enumerate(kinds, 1)]
    return Report("T", "a" * 40, "", None, "COMMENT", "", tuple(items), (), "", "").to_dict()


def test_confirmed_findings_keeps_red_orange_yellow_in_order():
    got = confirmed_findings(_report_dict("red", "question", "yellow", "preexisting", "orange"))
    assert [(f["n"], f["severity"]) for f in got] == [(1, "critical"), (3, "minor"), (5, "major")]
    assert got[0] == {"n": 1, "severity": "critical", "title": "t1", "path": "a.kt", "line": 10, "body": "b1"}


def test_confirmed_findings_of_questions_only_is_empty():
    assert confirmed_findings(_report_dict("question", "preexisting")) == []


def test_parse_findings_verdicts():
    text = json.dumps([{"n": 3, "verdict": "WON'T FIX", "why": "intended"},
                       {"n": 1, "verdict": "FIXED", "why": "null check added"}])
    assert parse_findings_verdicts(text, {1, 3}) == [
        {"n": 3, "verdict": "WON'T FIX", "why": "intended"}, {"n": 1, "verdict": "FIXED", "why": "null check added"}]


@pytest.mark.parametrize("entries", [
    {"n": 1},                                                    # not a list
    [{"n": 1, "verdict": "FIXED", "why": "x"}],                  # finding 3 missing
    [{"n": 1, "verdict": "FIXED", "why": "x"}, {"n": 1, "verdict": "FIXED", "why": "x"},
     {"n": 3, "verdict": "FIXED", "why": "x"}],                  # repeated
    [{"n": 1, "verdict": "FIXED", "why": "x"}, {"n": 3, "verdict": "FIXED", "why": "x"},
     {"n": 4, "verdict": "FIXED", "why": "x"}],                  # not a finding
    [{"n": "1", "verdict": "FIXED", "why": "x"}, {"n": 3, "verdict": "FIXED", "why": "x"}],
    [{"n": 1, "verdict": "", "why": "x"}, {"n": 3, "verdict": "FIXED", "why": "x"}],
    [{"n": 1, "verdict": "F" * 41, "why": "x"}, {"n": 3, "verdict": "FIXED", "why": "x"}],
    [{"n": 1, "verdict": "FIXED", "why": 5}, {"n": 3, "verdict": "FIXED", "why": "x"}],
])
def test_parse_findings_verdicts_refuses(entries):
    with pytest.raises(ReportError):
        parse_findings_verdicts(json.dumps(entries), {1, 3})
```

`parse_findings_verdicts("not json", {1})` raises `ReportError` too (add it to the parametrization as a raw string case if convenient). `True` is not an integer `n` (`type(n) is int`).

In `tests/unit/test_reviewer_runner.py` (beside `test_settle_address_needs_one_of_its_turns`):

```python
def test_settle_findings_needs_no_turn():
    j = Job("f1", SCOPE, 1712, "findings", HEAD, f"{HEAD}:r1", "running", "", "t", "t")
    assert settle(j, board_row(actions=()), [j]) == ("done", "")
    assert settle(j, board_row(actions=("merge",)), [j]) == ("done", "")
    assert settle(j, None, [j]) == ("stale", "PR gone")
    newer = Job("f2", SCOPE, 1712, "findings", HEAD, f"{HEAD}:r2", "queued", "", "u", "u")
    assert settle(j, board_row(actions=()), [newer, j]) == ("superseded", "newer job")
```

(Use `board_row`'s head argument as the other settle tests do for the new-head case and assert `("superseded", "new head")`.)

In `tests/unit/test_reviewer_jobs.py` (use the file's `row()` and `job()` helpers; a findings job is `job(kind="findings", key=f"{A}:r1", …)`; call `plan_jobs` as the address tests do):

- `test_findings_job_is_never_queued` — an author row with no turns and a done self-review in `jobs_by_pr`: `plan.queue == []`.
- `test_findings_job_at_an_old_head_is_superseded` — findings jobs at head `B` in `queued`, `deferred` and `done`, the row at `A`: each gets `("superseded", "new head")`; one in `failed` gets nothing.
- `test_findings_job_pushed_from_the_rows_head_is_kept` — the job's head is the pushed SHA, `pushed_from=lambda j: A`, the row at `A` (GitHub behind the push): no state change.
- `test_findings_job_with_a_post_under_way_is_kept` — `posting=frozenset({job.id})`: no state change.
- `test_findings_job_of_a_held_pr_is_kept` — `held=frozenset({1})`: no state change.

In `tests/unit/test_reviewer_store.py`:

```python
def test_a_run_stored_before_verdicts_loads_with_the_defaults(store):
    # write a run row whose JSON lacks "verdicts" and "findings_fixable", as an older Cleave wrote it
    ...
    run = store.run(job.id)
    assert (run.verdicts, run.findings_fixable) == ([], False)
```

(Write the row the way the store's existing "old row" tests do, or through `save_run` and then strip the two keys from the stored JSON with SQL.)

In `tests/unit/test_config_reviewer.py`: `ReviewerGlobalConfig().jobs["findings"] == ReviewerJobConfig("opus", "xhigh", 5400)`.

- [ ] **Step 2: Run them to verify they fail**

Run: `$VENV_PY -m pytest tests/unit/test_reviewer_report.py tests/unit/test_reviewer_runner.py tests/unit/test_reviewer_jobs.py tests/unit/test_reviewer_store.py tests/unit/test_config_reviewer.py -q -p no:cacheprovider -k "findings or verdicts"`
Expected: FAIL / ERROR — `confirmed_findings`, `parse_findings_verdicts` not importable; settle returns `("stale", "turn gone")`; no supersede for findings jobs; no `jobs["findings"]`.

- [ ] **Step 3: Implement**

- `FIX_KINDS` in `board.py` next to `ADDRESS_ACTIONS`.
- `JobRun`: `verdicts: list = field(default_factory=list)` and `findings_fixable: bool = False`, with a comment each, after `fix` (defaults: runs already stored lack them).
- `_default_reviewer_jobs`: `"findings": ReviewerJobConfig("opus", "xhigh", 5400)`.
- `report.py`: `SEVERITY`, `confirmed_findings`, `parse_findings_verdicts` (cap the text at `_MAX_REPORT`; `json.loads`; the checks in the spec's "verdicts.json is valid"; return the entries reduced to `n`, `verdict`, `why`).
- `settle`: for `job.kind == "findings"`, `has_turn = True` (like Review now).
- `plan_jobs`: inside the per-row loop, after the address block (a held row is already skipped by `continue`):

```python
for j in jobs:
    if (j.kind == "findings" and j.state in ("queued", "deferred", "done") and j.head_sha != head
            and pushed_from(j) != head and j.id not in posting):
        states.setdefault(j.id, ("superseded", "new head"))
```

Update `plan_jobs`'s docstring (rules in order) and the module docstring to name findings jobs.

- [ ] **Step 4: Run them to verify they pass**

Run: the Step 2 command, then `$VENV_PY -m pytest tests/unit/test_reviewer_jobs.py tests/unit/test_reviewer_runner.py tests/unit/test_reviewer_report.py tests/unit/test_reviewer_store.py tests/unit/test_config_reviewer.py -q -p no:cacheprovider`
Expected: PASS, and the files' other tests still pass.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/reviewer/board.py orchestrator/reviewer/models.py config/schemas.py orchestrator/reviewer/report.py orchestrator/reviewer/runner.py orchestrator/reviewer/jobs.py tests/unit/test_reviewer_report.py tests/unit/test_reviewer_runner.py tests/unit/test_reviewer_jobs.py tests/unit/test_reviewer_store.py tests/unit/test_config_reviewer.py
git commit -m "feat(reviewer): findings jobs — confirmed findings, verdicts, settle and the old-head rule"
```

---

### Task 2: Queueing a findings job, and what the service shows of it

**Files:**
- Modify: `orchestrator/reviewer/service.py`
- Test: `tests/unit/test_reviewer_service.py`

**Interfaces:**
- Consumes: `FIX_KINDS`, `confirmed_findings`, `JobRun.verdicts` (Task 1).
- Produces:
  - `ReviewerService.fix_findings(self, scope: str, number: int, *, review_id: str | None = None, instruction: str | None = None) -> Job` — the spec's "Queueing"; its `ValueError` texts are exactly `"not fixable: <why>"`, `"not the newest self-review"`, `"a post is under way"`, `"already queued"`.
  - `_self_review(jobs: list[Job], head: str) -> Job | None` (module level) — the newest `review` job at `head` (jobs newest first, as `store.jobs` lists them), whatever its state.
  - `_why_no_findings_fix(cfg: ReviewerConfig, row: BoardRow, held: frozenset[int], review: Job | None, run: JobRun | None) -> str` — `""` or the spec's six reasons in order, each an `N_()` literal (Task 4 passes them to `tr()`).
  - `address_now` and `_queue_address` refuse while a job of either `FIX_KINDS` kind is pending or posting.
  - `overview()` per repo: `"fixes"` = each row's newest job of a `FIX_KINDS` kind; `_fix_info(...)` gains `"kind"`; new `"findings_fixable": set[int]`.
  - `detail()` gains `"verdicts_job": str | None` and `"verdicts": list` (the newest findings job with verdicts); `"fix"`, `"fix_patch"`, `"pushed_sha"` come from the newest fix job of either kind that has a plan (address) or verdicts (findings).

- [ ] **Step 1: Write the failing tests**

Build on the address tests (`address_ctx(tmp_path, ADDR_CFG)`, `Rig`, `await rig.service.poll(c)`, row 1 is the operator's PR at head `A`). Add a helper in the test file:

```python
def self_review(rig, *kinds, head=A, state="done", stamp=None):
    """A review job of PR 1 at `head` with a report of these item kinds (see test_reviewer_report._report_dict)."""
    stamp = stamp or T0.isoformat()
    job = rig.store.add_job("acme/app", 1, "review", head, f"{head}:now", stamp)
    rig.store.set_job_state(job.id, state, "", stamp)
    items = tuple(ReportItem(i, k, f"t{i}", "a.kt", i, "", f"b{i}") for i, k in enumerate(kinds, 1))
    rig.store.save_run(JobRun(job.id, report=Report("T", head, "", None, "COMMENT", "", items, (), "", "").to_dict()))
    return job
```

Tests:

```python
async def test_fix_findings_queues_a_findings_job(tmp_path):
    rig, review = ...  # address_ctx + Rig + poll; review = self_review(rig, "red", "question", "yellow")
    old = rig.store.add_job("acme/app", 1, "findings", A, f"{A}:{review.id}", T0.isoformat())
    rig.store.set_job_state(old.id, "done", "", T0.isoformat())
    job = rig.service.fix_findings("acme/app", 1, instruction="leave 3 as it is")
    assert (job.kind, job.head_sha, job.key, job.state) == ("findings", A, f"{A}:{review.id}", "queued")
    assert rig.store.run(job.id).request == {"review": review.id, "instruction": "leave 3 as it is"}
    assert (rig.store.job(old.id).state, rig.store.job(old.id).reason) == ("superseded", "new findings fix")
    # without an instruction the request is {"review": review.id}


@pytest.mark.parametrize("setup, why", [
    ("no_address_doc", "no address_doc"),
    ("not_author", "not your PR"),
    ("pipeline", "a ticket workspace holds it"),
    ("held", "held by a comparison"),
    ("no_review", "no finished self-review at this head"),
    ("review_running", "no finished self-review at this head"),
    ("review_of_old_head", "no finished self-review at this head"),
    ("questions_only", "no confirmed findings"),
])
async def test_fix_findings_refuses_what_is_not_fixable(tmp_path, setup, why):
    ...
    with pytest.raises(ValueError, match=f"^not fixable: {why}$"):
        rig.service.fix_findings("acme/app", 1)


async def test_fix_findings_refuses_a_review_that_is_not_the_newest(tmp_path):
    older = self_review(rig, "red", stamp=T0.isoformat())
    self_review(rig, "red", stamp=(T0 + timedelta(minutes=1)).isoformat())
    with pytest.raises(ValueError, match="^not the newest self-review$"):
        rig.service.fix_findings("acme/app", 1, review_id=older.id)


async def test_one_fix_of_a_pr_at_a_time(tmp_path):
    self_review(rig, "red")
    rig.service.address_now("acme/app", 1)                    # an address job pending
    with pytest.raises(ValueError, match="^already queued$"):
        rig.service.fix_findings("acme/app", 1)
    # and the reverse: with a findings job queued, address_now and address_again raise "already queued"


async def test_fix_findings_refuses_while_a_push_is_under_way(tmp_path):
    # an address job (done) of PR 1 with a Post in state "posting" (as test_address_now_refuses builds one)
    with pytest.raises(ValueError, match="^a post is under way$"):
        rig.service.fix_findings("acme/app", 1)


async def test_fix_findings_lookup_errors(tmp_path):
    with pytest.raises(LookupError):
        rig.service.fix_findings("acme/app", 999)
    # and a scope whose board outlived its configuration, as test_address_now covers it


async def test_overview_shows_findings_fixable_and_the_newest_fix_of_either_kind(tmp_path):
    self_review(rig, "red")
    [repo] = rig.service.overview()
    assert repo["findings_fixable"] == {1}
    job = rig.service.fix_findings("acme/app", 1)
    [repo] = rig.service.overview()
    assert repo["fixes"][1]["id"] == job.id and repo["fixes"][1]["kind"] == "findings"


async def test_detail_of_a_findings_job(tmp_path):
    # a done findings job with run.verdicts and run.fix, and an older address job with a plan
    d = rig.service.detail("acme/app", 1)
    assert d["verdicts_job"] == job.id and d["verdicts"] == VERDICTS
    assert d["fix"] == FIX and d["fix_patch"] == "<the patch text>"
```

Keep the existing address `detail` and `overview` tests passing unchanged (their `fix` now also carries `kind`; update `test_overview_shows_an_address_job`'s expected dict if it compares the whole `_fix_info`).

- [ ] **Step 2: Run them to verify they fail**

Run: `$VENV_PY -m pytest tests/unit/test_reviewer_service.py -q -p no:cacheprovider -k "findings or one_fix"`
Expected: FAIL — `fix_findings` does not exist; `findings_fixable`, `verdicts` keys missing.

- [ ] **Step 3: Implement**

- `_self_review`, `_why_no_findings_fix` beside `_why_not_fixable` (reasons 1–4 as `_why_not_fixable` checks them, minus the thread-turn check; 5: `review is None or review.state not in ("done", "posted")`; 6: `not confirmed_findings((run or JobRun("")).report)`; a report of another head cannot happen because `_self_review` matches the head, but `confirmed_findings({})` must return `[]`).
- A private `_pending_fix(scope, number) -> str` used by `fix_findings`, `address_now` and `_queue_address`: `"a post is under way"` when a `FIX_KINDS` job of the PR has a Post `posting`, `"already queued"` when one is queued, running or deferred, else `""`. Keep `address_now`'s existing order of checks (fixable first).
- `fix_findings` in this order: `_row` (LookupError) → repo context (LookupError, as `address_now`) → `review = _self_review(...)` → `_why_no_findings_fix` → `review_id` check → `_pending_fix` → supersede the PR's `findings` jobs in done/posted/stale (`"new findings fix"`) → `add_job(scope, number, "findings", row.head_sha, f"{row.head_sha}:{review.id}", stamp)` → `save_run(JobRun(job.id, request={"review": review.id, **({"instruction": instruction} if instruction else {})}))`.
- `overview()`: `fixes` from `job.kind in FIX_KINDS`; `"kind": job.kind` in `_fix_info`; `"findings_fixable": {r.number for r in rows if not _why_no_findings_fix(ctx.cfg, r, held, (rv := _self_review(jobs_of_row, r.head_sha)), rv and run_of(rv))}` — reuse the per-repo job list you already iterate (group it by number once; no extra store reads per row beyond `run_of`). Update the docstring.
- `detail()`: add the findings job lookup (`kind == "findings" and runs[j.id].verdicts`), choose the fix job as the newer of the address job with a plan and the findings job with verdicts, take `fix`/`fix_patch`/`pushed_sha` from it; add `verdicts_job`, `verdicts`. Update the docstring.

- [ ] **Step 4: Run them to verify they pass**

Run: `$VENV_PY -m pytest tests/unit/test_reviewer_service.py tests/unit/test_dashboard_reviews.py -q -p no:cacheprovider`
Expected: PASS (the dashboard tests use a fake service; they must still pass).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/reviewer/service.py tests/unit/test_reviewer_service.py
git commit -m "feat(reviewer): fix_findings queues a findings job; one fix of a PR at a time"
```

---

### Task 3: Running a findings job

**Files:**
- Modify: `orchestrator/reviewer/prepare.py`
- Modify: `orchestrator/reviewer/session.py` (`_writable_dirs`, `settings_json`, `allowed_tools` docstring, new `findings_prompt`)
- Modify: `orchestrator/reviewer/runner.py` (`next_job`, `_recheck_ready` docstring, `recover`, `_run`, `_session`, `_finish`, `_message`)
- Modify: `orchestrator/reviewer/messages.py` (`findings_message`; `review_message` gains `fixable`)
- Modify: `orchestrator/tg_uk/reviewer.py`
- Test: `tests/unit/test_reviewer_prepare.py`, `tests/unit/test_reviewer_session.py`, `tests/unit/test_reviewer_runner.py`, `tests/unit/test_reviewer_messages.py`

**Interfaces:**
- Consumes: `FIX_KINDS`, `confirmed_findings`, `parse_findings_verdicts`, `JobRun.verdicts`, `JobRun.findings_fixable` (Task 1); the job's `run.request["review"]` (Task 2).
- Produces:
  - `session.findings_prompt(doc: str, number: int, paths: JobPaths, me: str, instruction: str) -> str` — the spec's prompt text, built like `address_prompt` from `_PREAMBLE`, `_AGENTS_AND_BASH`, the instruction line and `_DATA`.
  - `messages.findings_message(scope: str, number: int, title: str, url: str, head_sha: str, verdicts: list[dict], tg_lines: dict[int, str], fix: dict) -> str` — the spec's message.
  - `messages.review_message(..., fixable: bool = False)` — with `fixable` and a confirmed item, the reply line is `Reply to this message: "post 1 2 4 approve" · "text 2" · "edit 2: …" · "review again" · "fix"`.
  - A findings job runs end to end: Prepare writes `run_dir/findings.json`; the session sandbox is the address job's minus the replies plan directory; `_finish` keeps `run.verdicts` and `run.fix`; the message goes out.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_reviewer_prepare.py` (build on `test_address_job_needs_its_document_on_the_base` and the `world`/`origin` fixtures; the findings job's run must carry `request={"review": review.id}` and the review's run a report dict at the job's head, built as in Task 2's `self_review`):

- `test_findings_job_writes_its_findings` — `json.loads((paths.run_dir / "findings.json").read_text())` equals `confirmed_findings(review_run.report)`; the worktree is at the head.
- `test_findings_job_needs_the_address_document_on_the_base` — `PrepareError` outcome `failed`, reason `<address_doc> not on <base>`.
- `test_findings_job_fails_without_its_self_review` — no review run (or no `request["review"]`): `failed`, `the self-review's report is gone`.
- `test_findings_job_fails_on_a_report_of_another_head` — the review job's head differs from the job's: `failed`, `the self-review's report is gone`.
- `test_findings_job_fails_without_a_confirmed_finding` — a report of ❓ items only: `failed`, `no confirmed findings`.

`tests/unit/test_reviewer_session.py` (beside the address sandbox tests):

- `test_findings_session_writes_the_worktree_and_the_run_directory_only` — `_writable_dirs("findings", paths) == [paths.run_dir, paths.worktree]`; `settings_json(..., "findings")` denies writes to the clone and to `worktree/.git` (as the address case asserts), and `allowed_tools(...)` has no `Edit(` rule for the replies plan's directory.
- `test_findings_prompt` — the prompt contains `Follow docs/threads.md`, `PR #1712, whose author is octo`, `/findings.json`, `/verdicts.json`, `/tg_lines.json`, the worktree path, `commit nothing`, and the instruction line only when an instruction is given.

`tests/unit/test_reviewer_messages.py` (beside `test_address_message`):

```python
VERDICTS = [{"n": 2, "verdict": "FIXED", "why": "null check added"},
            {"n": 3, "verdict": "WON'T FIX", "why": "intended"}]


def test_findings_message():
    text = findings_message("acme/app", 108, "Fix cart sync", "https://github.com/acme/app/pull/108", "8f01a2b3c" + "0" * 31,
                            VERDICTS, {2: "FIXED · CartRepo.kt:88 · null list handled"}, FIX)
    assert text.splitlines()[0] == '[acme/app] Self-review findings on your PR #108 "Fix cart sync"'
    assert text.splitlines()[1].startswith("head 8f01a2b3c · 2 findings · fix: ")
    assert "2. FIXED · CartRepo.kt:88 · null list handled" in text
    assert "3. WON'T FIX · intended" in text
    assert 'Reply to this message: "push" · "diff" · "again: …"' in text


def test_findings_message_without_a_patch_offers_no_push():
    text = findings_message(..., VERDICTS, {}, {})
    assert "no code change" in text and '"push"' not in text and '"diff"' not in text


def test_review_message_offers_fix_only_when_fixable_with_a_confirmed_finding():
    # fixable=True and a red item: the reply line ends with ' · "fix"'
    # fixable=True and only a question item: no "fix"; fixable=False: no "fix"
```

`tests/unit/test_reviewer_runner.py` — a `findings_rig(tmp_path, monkeypatch)` built from `address_rig` whose store holds the self-review (job + run with a report at `HEAD`) and whose fake session writes `run_dir/verdicts.json` (and `tg_lines.json`) instead of the replies plan:

- `test_findings_job_keeps_verdicts_and_fix_and_sends_the_message` — state `("done", "")`; `run.verdicts == VERDICTS`, `run.fix == FIX`; `rig.captured` holds the worktree, the job copy's `fix.patch` and `[".github/**"]`; `rig.chat.texts == [findings_message(...)]`; `rig.session_kw["kind"] == "findings"`, `rig.session_kw["job_cfg"] == JOBS["findings"]`, `rig.session_kw["prompt"] == findings_prompt("docs/threads.md", 1712, rig.paths, "octo", "")`; the job root is removed.
- `test_findings_prompt_carries_the_operators_instruction` — `request={"review": …, "instruction": "leave 3"}` reaches the prompt.
- `test_findings_job_fails_without_verdicts` — the session writes nothing: `("failed", "no verdicts written")`.
- `test_findings_job_fails_on_unreadable_verdicts` — `verdicts.json` misses a finding: state failed, reason starts with `report unreadable: `.
- `test_findings_job_fails_when_the_worktree_git_pointer_changed` and `…_when_prepare_left_changes` — as the address ones.
- `test_findings_jobs_are_recovered_and_wait_for_the_first_poll` — as the address one.
- `test_a_self_review_run_offers_fix` — a review job of a row with role `author`, in a repo with `address_doc`, report with a red item: `run.findings_fixable is True` and the sent message's reply line ends with `· "fix"`; the same review on a row without `author`: `False`, no `"fix"`.

- [ ] **Step 2: Run them to verify they fail**

Run: `$VENV_PY -m pytest tests/unit/test_reviewer_prepare.py tests/unit/test_reviewer_session.py tests/unit/test_reviewer_runner.py tests/unit/test_reviewer_messages.py -q -p no:cacheprovider -k "findings or fix"`
Expected: FAIL — no `findings_prompt`, `findings_message`; Prepare writes no `findings.json`; the runner treats a findings job as a re-check.

- [ ] **Step 3: Implement**

- **prepare.py:** in `_prepare`, `if job.kind in FIX_KINDS and not ctx.clone.has_file(process.address_doc)` (was `== "address"`). Before taking the review clone's lock, for `job.kind == "findings"`: `findings = _findings_of(store, job)` — the job's run's `request.get("review")` → `store.job(...)` must be a `review` at `job.head_sha` with a run that has a report, else `PrepareError("failed", "the self-review's report is gone")`; `confirmed_findings(report)` empty → `PrepareError("failed", "no confirmed findings")`. Pass `findings` to `_prepare_job_clone` (keyword, default `None`) and write it there with `_write_json(run / "findings.json", …)` — `_write_json` takes a dict today: widen its annotation to `dict | list`.
- **session.py:** `_writable_dirs`: `findings` → `[paths.run_dir, paths.worktree]`; `settings_json`: `kind in FIX_KINDS` for the pointer rule; docstrings name findings jobs; add `findings_prompt` (spec text; `{paths.run_dir}/findings.json`, `{paths.run_dir}/verdicts.json`, `{paths.run_dir}/tg_lines.json`).
- **runner.py:**
  - `next_job` and `_recheck_ready`: findings jobs wait like address jobs (`job.kind not in ("recheck", "address", "findings")`).
  - `recover`: `kinds=("review", "recheck", "address", "findings")`.
  - `_run`: `require_clean` for `job.kind in FIX_KINDS`.
  - `_session`: `findings_prompt(process.address_doc, job.number, paths, me, run.request.get("instruction", ""))`; the pointer guard for `job.kind in FIX_KINDS`.
  - `_finish`: the output file is `paths.job_dir / "verdicts.json"` for findings (`N_("no verdicts written")` when missing); parse with `parse_findings_verdicts(text, numbers)` where `numbers` are the `n` of `confirmed_findings` of the review run named by `run.request["review"]` (read from the store; an unreadable one is `ReportError` → `report unreadable: …`); store in `run.verdicts`; `capture_fix` for `job.kind in FIX_KINDS`. For a review job, set `run.findings_fixable = row is not None and "author" in row.roles and bool(ctx.cfg.process.address_doc)` where `row` is read (the existing `row = …board(...).get(...)` line comes after; move that read up or read it once more).
  - `_message`: findings → `findings_message(job.scope, job.number, run.title, run.url, job.head_sha, run.verdicts, tg_lines, run.fix)`; review → pass `fixable=run.findings_fixable`.
- **messages.py:** `findings_message` mirrors `address_message`: header `tr('[{scope}] Self-review findings on your PR #{number} "{title}"', …)`, the second line `tr("head {head} · {findings} · {change}", …)` with `count(len(verdicts), "finding")` (add the noun to `NOUNS` if `count` needs it — see `tests/unit/test_tg_i18n.py`), the protected-files line as the address message's, one line per verdict `f"{n}. {model_text(_cut(gist))}"` where `gist = tg_lines.get(n) or f"{verdict} · {why}"`, then `tr("Reply to this message: {commands}", …)` with `"push"` (a patch without protected files), `"diff"` (a patch), `"again: …"`, and the URL. `review_message(..., fixable=False)`: a second module constant for the hint with `· "fix"`, used when `fixable` and the report has a `CONFIRMED` item.
- **tg_uk/reviewer.py:** Ukrainian for every new literal (header, head line, `the self-review's report is gone`, `no confirmed findings`, `no verdicts written`, the review hint with "fix", the noun). Keep quotes, emoji, digits and placeholders exactly (the catalogue test checks).

- [ ] **Step 4: Run them to verify they pass**

Run: `$VENV_PY -m pytest tests/unit/test_reviewer_prepare.py tests/unit/test_reviewer_session.py tests/unit/test_reviewer_runner.py tests/unit/test_reviewer_messages.py tests/unit/test_tg_i18n.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/reviewer/prepare.py orchestrator/reviewer/session.py orchestrator/reviewer/runner.py orchestrator/reviewer/messages.py orchestrator/tg_uk/reviewer.py tests/unit/test_reviewer_prepare.py tests/unit/test_reviewer_session.py tests/unit/test_reviewer_runner.py tests/unit/test_reviewer_messages.py
git commit -m "feat(reviewer): run a findings job — findings.json in, verdicts and one patch out, its message"
```

---

### Task 4: Telegram — "fix", the findings message's replies, and the push

**Files:**
- Modify: `orchestrator/reviewer/grammar.py` (`_PHRASES["fix"]`, `parse_reply`, `check_command`, `describe` if it must name `fix`)
- Modify: `orchestrator/reviewer/replies.py` (`_execute`, `_check`, `_fix_findings`)
- Modify: `orchestrator/reviewer/posting.py` (`_push_fix`)
- Modify: `orchestrator/reviewer/lines.py` (`not_understood_line`)
- Modify: `orchestrator/tg_uk/reviewer.py`
- Modify: `docs/telegram.md`
- Test: `tests/unit/test_reviewer_grammar.py`, `tests/unit/test_reviewer_replies.py`, `tests/unit/test_reviewer_push.py`

**Interfaces:**
- Consumes: `ReviewerService.fix_findings` and its `ValueError` texts, `_why_no_findings_fix`'s `N_()` reasons (Task 2); `FIX_KINDS` (Task 1); findings jobs with `run.fix` (Task 3).
- Produces:
  - `parse_reply(text)` → `ReplyCommand("fix")` for the spec's words (each only as the whole reply, like `push`).
  - `check_command(cmd, kind, …)`: `fix` only on kind `review`; on a findings message only `push`, `diff`, `again` (and `review again`, `skip`); `push`/`diff`/`again` only on a `FIX_KINDS` message, with the spec's refusal texts.
  - `not_understood_line("findings")` → `tr('Not understood; nothing was done. Reply like "push", "diff" or "again: …".')`.
  - `push_fix` pushes a findings job's patch with the spec's commit message and no "post replies" hint.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_reviewer_grammar.py` (beside `test_address_grammar`):

```python
@pytest.mark.parametrize("text", ["fix", "Fix", "fix findings", "пофикси", "почини", "фикс", "пофікси", "полагодь"])
def test_fix_grammar(text):
    assert parse_reply(text) == ReplyCommand("fix")


@pytest.mark.parametrize("text", ["fix 2", "fix and push", "please fix"])
def test_fix_is_the_whole_reply_only(text):
    assert parse_reply(text) is None or parse_reply(text).action != "fix"


def test_check_command_on_a_findings_message():
    for action in ("push", "diff"):
        assert check_command(ReplyCommand(action), "findings", None, [], set(), set()) == ReplyCommand(action)
    refused = 'This is a findings message: reply "push", "diff" or "again: …". Nothing was done.'
    assert check_command(ReplyCommand("post", (1,), "COMMENT"), "findings", None, [], set(), set()) == refused
    assert check_command(ReplyCommand("text", (1,)), "findings", None, [], set(), set()) == refused
    assert check_command(ReplyCommand("post_replies", (1,)), "findings", None, [], set(), set()) == refused


def test_fix_only_on_a_review_message():
    assert check_command(ReplyCommand("fix"), "review", REPORT, [], set(), set()) == ReplyCommand("fix")
    for kind in ("recheck", "address", "findings"):
        assert check_command(ReplyCommand("fix"), kind, None, [], set(), set()) == (
            '"fix" works on the message of a self-review of your own PR. Nothing was done.')


def test_push_on_a_review_message_names_both_fix_messages():
    assert check_command(ReplyCommand("push"), "review", REPORT, [], set(), set()) == (
        '"push" works on a fix message of your own PR (threads or self-review findings). Nothing was done.')


def test_not_understood_on_a_findings_message():
    assert not_understood_line("findings") == 'Not understood; nothing was done. Reply like "push", "diff" or "again: …".'
```

(Use the `ReplyCommand` constructor and the `REPORT` fixture the file already uses; adjust `ReplyCommand(...)` arguments to its real fields.)

`tests/unit/test_reviewer_replies.py` (beside `test_again_works_on_a_failed_address_job_and_not_on_a_review` and `test_push_and_diff_need_a_done_address_job`; the fake service gets a `fix_findings` that records its calls and can raise):

- `test_fix_on_a_self_review_message_queues_a_findings_job` — reply "fix" to a done review job's message: `service.fix_findings` called with `("acme/app", 1712)` and `review_id=job.id`; the answer is `Fixing the findings of #1712 at <head9>: the result comes here; nothing is pushed until you reply "push".`
- `test_fix_answers_each_refusal` — the service raising `ValueError("already queued")` → `A run on #1712 is already queued.`; `ValueError("a post is under way")` → `A push of #1712 is under way; try again when it ends. Nothing was done.`; `ValueError("not the newest self-review")` → `This is not the newest self-review of #1712 at its head. Nothing was done.`; `ValueError("not fixable: no confirmed findings")` → `Cannot fix the findings of #1712: no confirmed findings. Nothing was done.`; `LookupError` → `#1712 is not on the board any more; nothing was done.`
- `test_fix_needs_the_claude_login` — with `claude_login` failing, the login refusal the address "again" gives.
- `test_again_on_a_findings_message_queues_with_the_instruction` — reply `again: leave 3` on a done (and on a failed) findings job: `fix_findings("acme/app", 1712, instruction="leave 3")` with no `review_id`; on a superseded one: the outdated line.
- `test_push_and_diff_work_on_a_findings_job` — "diff" answers the patch parts; "push" reaches `posting.push_fix`.

`tests/unit/test_reviewer_push.py` (beside the address `push_fix` success test):

- `test_push_of_a_findings_fix` — a done findings job with a patch: the commit message is `fix(ACME-123): self-review findings on #1712` (with the run's ticket key) and `fix: self-review findings on #1712` without one; the answer is `pushed_line(...)` with no `post replies` line; the job is re-headed (`<new sha>:<review id>`).
- `test_push_refuses_a_findings_job_without_a_patch` — `This job changed no code: nothing to push.`

- [ ] **Step 2: Run them to verify they fail**

Run: `$VENV_PY -m pytest tests/unit/test_reviewer_grammar.py tests/unit/test_reviewer_replies.py tests/unit/test_reviewer_push.py -q -p no:cacheprovider -k "fix or findings"`
Expected: FAIL — "fix" parses to None; findings messages refused as non-address; `push_fix` refuses a findings job.

- [ ] **Step 3: Implement**

- **grammar.py:** `_PHRASES["fix"] = ("fix", "fix findings", "пофикси", "почини", "фикс", "пофікси", "полагодь")`, parsed like `push` (whole reply only). `check_command`: first `fix` (refused unless `kind == "review"`); then `kind == "findings"` refuses every action but `push`, `diff`, `again` (and the PR actions, which `_check` handles before); `push`/`diff`/`again` refused when `kind not in FIX_KINDS`, with the new text (the old `'"{action}" works on a threads message of your own PR. Nothing was done.'` goes, with its Ukrainian entry). Keep the address branch as it is. If `describe()` or the LLM context enumerate actions, make sure `fix` never reaches them (it is never confirmed by Yes/No).
- **replies.py:** in `_execute`, a `fix` branch before `text`: claude login failing → `_NO_LOGIN`; else answer `self._fix_findings(job, review_id=job.id)`. The `again` branch: `self._fix_findings(job, instruction=checked.instruction)` for a findings job, `_address_again` otherwise. `_check`: the `again` rule applies to `job.kind in FIX_KINDS`. `_fix_findings(job, *, review_id=None, instruction=None) -> str` maps the outcomes to the texts in the tests (the `not fixable: <why>` case passes `tr(why)`: the reasons are `N_()` literals from Task 2). The `text` branch is unreachable for findings (refused by `check_command`).
- **posting.py `_push_fix`:** `job.kind not in FIX_KINDS or not run.fix` → the existing refusal; the commit message `what = "address review comments" if job.kind == "address" else "self-review findings"`; `fixed = [...] if job.kind == "address" else []`. Docstrings name both kinds.
- **lines.py:** `not_understood_line` for `findings`; docstring.
- **tg_uk/reviewer.py:** Ukrainian for every new text and the changed refusal; remove the replaced one.
- **docs/telegram.md:** under "Replying to reviewer messages": a "#### Fixing the findings of a self-review" section — "fix" on the self-review's message (words, refusals), the findings message (the spec's example, neutral names), its replies (push, diff, again) and what they refuse; the review message's hint with "fix". Match the address job's section's style.

- [ ] **Step 4: Run them to verify they pass**

Run: `$VENV_PY -m pytest tests/unit/test_reviewer_grammar.py tests/unit/test_reviewer_replies.py tests/unit/test_reviewer_push.py tests/unit/test_reviewer_posting.py tests/unit/test_reviewer_reply_llm.py tests/unit/test_tg_i18n.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/reviewer/grammar.py orchestrator/reviewer/replies.py orchestrator/reviewer/posting.py orchestrator/reviewer/lines.py orchestrator/tg_uk/reviewer.py docs/telegram.md tests/unit/test_reviewer_grammar.py tests/unit/test_reviewer_replies.py tests/unit/test_reviewer_push.py
git commit -m "feat(reviewer): \"fix\" on a self-review message; push, diff and again on a findings message"
```

---

### Task 5: Dashboard — Fix findings…, the To fix column, the panel's verdicts

**Files:**
- Modify: `dashboard/reviews_api.py` (`_pr`: `can_fix_findings`, `fix.kind`; route `POST /api/reviews/fix-findings`; detail passes `verdicts_job`, `verdicts`)
- Modify: `dashboard/static/js/api.js` (`fixFindings`)
- Modify: `dashboard/static/js/reviews-board.js` (`columnOf`, `fixState`, `moveText`, `buttonsHtml`, `againConfirm`)
- Modify: `dashboard/static/js/reviews.js` (`CALLS`, the confirmation condition, the panel's "Fix of the findings" block)
- Modify: `dashboard/static/style.css` (only if the verdicts block needs a rule; reuse the replies list's classes first)
- Modify: `docs/dashboard.md`
- Test: `tests/unit/test_dashboard_reviews.py`, `tests/js/reviews-board.test.mjs`

**Interfaces:**
- Consumes: `overview()`'s `findings_fixable`, `fixes[n]["kind"]`; `detail()`'s `verdicts_job`, `verdicts`; `ReviewerService.fix_findings` (Task 2).
- Produces:
  - API: each PR has `can_fix_findings: bool`; `fix` (when present) has `kind`. `POST /api/reviews/fix-findings` `{scope, number}` → 200 `{"job": …}`, 404 on `LookupError`, 409 `{"error": <text>}` on `ValueError` — the same shape as `/api/reviews/fix-now`.
  - JS: `fixState(pr, kind = 'address')`, `columnOf(pr)` with the ready-fix rule, `againConfirm(pr, 'fix-findings' | 'fix-findings-again')`, buttons `fix-findings` / `fix-findings-again` in the panel.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_dashboard_reviews.py`: add `can_fix_findings` to `PR_KEYS`; the fake service's overview gives `findings_fixable` and a `fixes` entry with `kind`; the fake gains `fix_findings`.

- `test_pr_can_fix_findings_and_fix_kind` — `can_fix_findings` is `True` for a number in `findings_fixable`, else `False`; `pr["fix"]["kind"] == "findings"`.
- `test_fix_findings_codes` — as `test_fix_now_codes`: 200 with the job; `LookupError` → 404; `ValueError("already queued")` → 409 with that text; the fake was called with `("acme/app", 1)` and no review id.
- Add `/api/reviews/fix-findings` to the bad-body and cross-site parametrizations; `test_no_posting_route` expects 6 routes.
- `test_detail_carries_verdicts` — the detail response has `verdicts_job` and `verdicts` from the service.

`tests/js/reviews-board.test.mjs` (use the file's fixture and `fixJob` helper; give `fixJob` a `kind` field, default `'address'`):

```js
test('a findings fix ready to push puts my PR in To fix', () => {
  const pr = { ...mine, my_turns: [], fix: fixJob({ kind: 'findings', state: 'done', fix: true, pushed: false }) };
  assert.equal(columnOf(pr), 'fix');
  assert.equal(moveText(pr, 'fix', 'octo').text, 'fix ready: push from Telegram');
  assert.equal(columnOf({ ...pr, fix: { ...pr.fix, pushed: true } }), 'others');
  assert.equal(columnOf({ ...pr, fix: { ...pr.fix, fix: false } }), 'others');
});

test('Fix findings… in the panel only, with its states', () => {
  const base = { ...mine, can_fix_findings: true, can_fix: false, fix: null };
  assert.match(buttonsHtml('acme/app', base, 'panel'), /data-review-action="fix-findings"[^>]*>Fix findings…</);
  assert.doesNotMatch(buttonsHtml('acme/app', base, 'card'), /fix-findings/);
  const again = { ...base, fix: fixJob({ kind: 'findings', state: 'done', fix: true }) };
  assert.match(buttonsHtml('acme/app', again, 'panel'), />Fix findings again…</);
  const addressDone = { ...base, fix: fixJob({ kind: 'address', state: 'done', fix: true }) };
  assert.match(buttonsHtml('acme/app', addressDone, 'panel'), />Fix findings…</);   // not "again": another kind
  const queued = { ...base, fix: fixJob({ kind: 'address', state: 'queued' }) };
  assert.match(buttonsHtml('acme/app', queued, 'panel'), /disabled[^>]*>Queued</);
  const both = { ...queued, can_fix: true };
  assert.equal((buttonsHtml('acme/app', both, 'panel').match(/>Queued</g) || []).length, 1);
  assert.equal(fixState(addressDone, 'findings'), 'none');
  assert.equal(fixState(addressDone), 'again');
});

test('Fix findings… asks first, naming the confirmed findings and the head', () => {
  const pr = { ...mine, review: { ...mine.review, head: '8f01a2b3c', findings: { critical: 1, major: 2, minor: 1, question: 3, preexisting: 0, dropped: 0 } } };
  const c = againConfirm(pr, 'fix-findings');
  assert.equal(c.title, 'Fix the findings of the self-review?');
  assert.match(c.bodyHtml, /each of the 4 confirmed findings of the self-review at 8f01a2b3c/);
  assert.match(c.bodyHtml, /Nothing is pushed until you reply &quot;push&quot; in Telegram\./);
  assert.match(againConfirm({ ...pr, fix: fixJob({ kind: 'findings', state: 'done', started_at: '2026-11-19T10:00:00Z' }) },
    'fix-findings-again').bodyHtml, /is replaced; it was not pushed\./);
});
```

(Adapt `mine`, `fixJob` and the escaping of quotes to what the file and `esc` actually produce.)

- [ ] **Step 2: Run them to verify they fail**

Run: `$VENV_PY -m pytest tests/unit/test_dashboard_reviews.py -q -p no:cacheprovider` and `node --test 'tests/js/*.test.mjs'`
Expected: FAIL — no `can_fix_findings`, no route; `columnOf` returns `'others'`; no Fix findings… button.

- [ ] **Step 3: Implement**

- **reviews_api.py:** `_pr` adds `can_fix_findings` (`n in repo["findings_fixable"]`) and leaves `fix` as the service gives it (now with `kind`); the route as `fix-now`'s, calling `service.fix_findings(scope, number)`; detail passes the two new keys through if it filters keys.
- **api.js:** `fixFindings(scope, number)` → `postReviewAction('/api/reviews/fix-findings', …)`.
- **reviews-board.js:**
  - `columnOf`: a ready fix — `fix && fix.state === 'done' && fix.fix && !fix.pushed` — counts as a turn of mine other than merge: review/recheck turn → `'review'`; else (other non-merge turn or a ready fix) → `'fix'`; else merge → `'merge'`; else `'others'`. Update the module's column comment and `COLUMNS`' hint if it states the rule.
  - `fixState(pr, kind = 'address')`: posting and pending as today; `'again'` only when `(pr.fix.kind || 'address') === kind`.
  - `moveText` (column `'fix'`): for `fix.kind === 'findings'`: `'fix ready: push from Telegram'` when done, unpushed, with a patch; else the turns' words (no fix line). Address fixes unchanged.
  - `buttonsHtml`: after the Fix now block, `if (mine && where === 'panel' && pr.can_fix_findings)`: posting or pending → the disabled button, unless Fix now already rendered it (`pr.can_fix`); `'again'` for kind findings → `button('fix-findings-again', 'Fix findings again…')`; else `button('fix-findings', 'Fix findings…')`.
  - `againConfirm`: `'fix-findings'` and `'fix-findings-again'` return `{ title: 'Fix the findings of the self-review?', label: 'Fix findings', bodyHtml }` with the spec's body (`n` = critical + major + minor of `pr.review.findings`, `pr.review.head`), plus for `…again` ` The fix from <when> is replaced; it was not pushed.` (`fmtWhen(fix.started_at)`; `The current fix is replaced; it was not pushed.` without a time).
- **reviews.js:** `CALLS['fix-findings'] = CALLS['fix-findings-again'] = fixFindings`; the confirmation runs for `kind.endsWith('-again') || kind === 'fix-findings'`; the status line after a queue says `Queued a fix of the findings of #<n> at <head9>` (as Fix now's says its own); the panel body renders `detail.verdicts` under the report as "Fix of the findings": `n. VERDICT — why` per entry (escape everything; model text), then the existing fix files / patch / pushed commit block, which now reads `detail.fix` whatever the kind.
- **docs/dashboard.md:** the column rule (a ready fix is my move), the panel buttons table (Fix findings… / Fix findings again…, their states), the confirmation text, the panel's "Fix of the findings" block, and the API list (`can_fix_findings`, `fix.kind`, `POST /api/reviews/fix-findings` with its 404/409, detail's `verdicts_job`/`verdicts`).

- [ ] **Step 4: Run them to verify they pass**

Run: `$VENV_PY -m pytest tests/unit/test_dashboard_reviews.py tests/unit/test_dashboard_web.py -q -p no:cacheprovider` and `node --test 'tests/js/*.test.mjs'`
Expected: PASS.

- [ ] **Step 5: Look at it**

Start a throwaway dashboard on a free port over a **copy** of a reviewer database (never the live one, never the live daemon's port), open the Reviews tab in a browser, and check: an own PR with a self-review shows Fix findings… in the panel, not on the card; the dialog's text; after confirming, the button reads Queued and the status line says so; a PR whose findings fix is done and unpushed sits in To fix with `fix ready: push from Telegram`. Stop the server afterwards.

- [ ] **Step 6: Commit**

```bash
git add dashboard/reviews_api.py dashboard/static/js/api.js dashboard/static/js/reviews-board.js dashboard/static/js/reviews.js dashboard/static/style.css docs/dashboard.md tests/unit/test_dashboard_reviews.py tests/js/reviews-board.test.mjs
git commit -m "feat(dashboard): Fix findings… in the PR panel; a ready fix is my move"
```

---

## Finish

- [ ] Whole suite (Global Constraints) and the JS tests: only the 9 known failures.
- [ ] `ruff check` on the changed Python files reports nothing the base did not.
- [ ] Final whole-branch review on the most capable model, against the spec and this plan's Review Focus; fix Critical and Important findings test-first.
