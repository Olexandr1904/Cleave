# Reviewer Mode Owns Every PR Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In a repo with reviewer mode on, the ticket pipeline hands its PR to reviewer mode right after opening it, and reviewer mode treats that PR like any other; the pipeline's own PR flow runs only where reviewer mode is off.

**Architecture:** The orchestrator is told at start which repos have a reviewer context. For those, the `pr_review` stage returns `DONE` instead of running `fetch_pr_comments`, and the two Telegram messages around it change. In the reviewer, `plan_jobs` and the dashboard stop treating `row.pipeline` as "hands off"; the flag stays as a label.

**Tech Stack:** Python 3, asyncio, pytest + pytest-asyncio, Starlette dashboard.

**Spec:** `docs/superpowers/specs/2026-11-18-reviewer-owns-prs-design.md` (amends `docs/superpowers/specs/2026-11-12-reviewer-mode-design.md`).

## Global Constraints

- Run tests with `.venv/bin/python -m pytest <path> -q` from the repo root. Tests never start the real `claude`.
- Every text bound for Telegram is an English template passed through `tr()`; each new template needs its Ukrainian text in `orchestrator/tg_uk/` (`tests/unit/test_tg_i18n.py::test_every_text_has_ukrainian` fails otherwise).
- A repo with reviewer mode off must behave exactly as before: no changed message, state or GitHub call.
- Do not change `workflows/default-workflow.yaml` or `VALID_TRANSITIONS`.
- Touch only what a task names. Commit after each task; messages in the repo's style (`feat(reviewer): …`, `feat(pipeline): …`, `docs: …`).
- Another plan (`2026-11-18-reviewer-address.md`) edits `orchestrator/reviewer/jobs.py::plan_jobs` too. Here, change only the two `row.pipeline` conditions in it.

## Review Focus

1. A ticket already in `PR_REVIEW` with undecided escalations when the daemon restarts with this change: it must reach `DONE` on its next advance, and a late reply to an old escalation message must not crash the command handler (Task 3 test `test_hand_over_drops_pending_escalations`).
2. The `reviews` switch paused for the repo: the pipeline still hands over (Task 2 test `test_reviewed_scopes_ignore_the_switch` documents that the switch is not consulted).
3. A dry run: no reviewer contexts, so the pipeline keeps its own PR flow (Task 2 test `test_reviewed_scopes_empty_in_a_dry_run`).
4. A workspace whose repo is not in any project config any more (`company_id`/`repo_id` unknown): treated as "reviewer mode off" (Task 3 test `test_fetch_runs_for_an_unknown_scope`).
5. `Orchestrator` built by tests with `MagicMock(spec=Orchestrator)` and no `_reviewed_repos`: must read as "reviewer mode off" (existing `TestHandleActionStage` tests stay green unchanged).

---

### Task 1: Reviewer plans jobs and lines for pipeline PRs

**Files:**
- Modify: `orchestrator/reviewer/jobs.py:76-77` and `:132`
- Modify: `dashboard/reviews_api.py:40`
- Test: `tests/unit/test_reviewer_jobs.py`, `tests/unit/test_dashboard_reviews.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `plan_jobs` ignores `BoardRow.pipeline`; `/api/reviews` groups rows by `row.my_turns` alone.

- [ ] **Step 1: Rewrite the two tests that pin the old rule**

In `tests/unit/test_reviewer_jobs.py` replace the body of `test_pipeline_row_and_done_job_wait_for_new_review` and rename it:

```python
def test_pipeline_row_is_planned_like_any_other():
    p = plan([row(pipeline=True)], {1: [job(head=B, state="queued")]})
    assert p.queue == [(1, "review", A, A)] and p.states == [("j1", "superseded", "new head")]
    assert plan([row(head=B, seen=RECENT)], {1: [job(head=A, state="done")]}).states == []
```

In `test_turn_lines_and_first_poll_baseline` replace the last line with:

```python
    assert plan([row(actions=(("merge", ()),), pipeline=True)], new=new[:1]).lines == [
        "[acme/app] #1: your turn — merge · https://github.com/acme-org/acme-app/pull/1"]
```

In `tests/unit/test_dashboard_reviews.py::test_get_reviews_groups_and_trims` (fixture row #2 is `pipeline=True` with an `answer_threads` turn) change the grouping assertions to:

```python
    assert [p["number"] for p in repo["my_turn"]] == [2, 1]
    assert [p["number"] for p in repo["waiting"]] == [3]
    pr = repo["my_turn"][1]
    ...
    assert repo["my_turn"][0]["pipeline"] is True
    assert repo["waiting"][0]["job"] is None
    assert [(p["number"], p["large"]) for p in repo["waiting"]] == [(3, True)]
```

(`...` = the unchanged assertions on `pr`.) If `(2, False)` for `large` was the only coverage of a not-large waiting row, keep it by asserting `repo["my_turn"][0]["large"] is False`.

- [ ] **Step 2: Run them, expect failures**

Run: `.venv/bin/python -m pytest tests/unit/test_reviewer_jobs.py tests/unit/test_dashboard_reviews.py -q`
Expected: the three edited tests FAIL (queue is `[]`, lines are `[]`, `my_turn` is `[1]`).

- [ ] **Step 3: Drop the pipeline conditions**

`orchestrator/reviewer/jobs.py`: delete `if row.pipeline: continue` at the top of the row loop; in the turn-lines loop change the condition to `if change.action not in JOB_ACTIONS and row is not None:`.
`dashboard/reviews_api.py::_repo`: `group = my_turn if row.my_turns else waiting`.

- [ ] **Step 4: Run the two files, expect PASS; then `tests/unit -k reviewer -q`, expect PASS**

- [ ] **Step 5: Commit**

```bash
git add orchestrator/reviewer/jobs.py dashboard/reviews_api.py tests/unit/test_reviewer_jobs.py tests/unit/test_dashboard_reviews.py
git commit -m "feat(reviewer): plan jobs and turn lines for the ticket pipeline's PRs too"
```

---

### Task 2: The orchestrator knows which repos reviewer mode owns

**Files:**
- Modify: `orchestrator/orchestrator.py` (next to `set_switches`, ~line 227)
- Modify: `main.py` (new helper near `_reviewer_contexts`; one call right after `reviewer_contexts = _reviewer_contexts(...)`, ~line 777)
- Test: `tests/unit/test_main_reviewer.py`, `tests/unit/test_action_stage.py`

**Interfaces:**
- Produces:
  - `Orchestrator.set_reviewed_repos(self, scopes: frozenset[str]) -> None` — stores `self._reviewed_repos`.
  - `main._reviewed_scopes(contexts: list[RepoContext]) -> frozenset[str]` — `ctx.scope` of each.
  - Readers use `getattr(self, "_reviewed_repos", frozenset())` and `scope_of(state.company_id, state.repo_id)` from `orchestrator.switches`. No method is added for the lookup: `MagicMock(spec=Orchestrator)` would make a method return a truthy mock.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_main_reviewer.py` (reuse the file's own helpers that `test_reviewer_contexts` and `test_dry_run_builds_no_reviewer` use to build `projects` and `on_auth_failure`):

```python
def test_reviewed_scopes(tmp_path):
    # same arrange as test_reviewer_contexts
    contexts = _reviewer_contexts(global_config, projects, on_auth_failure)
    assert _reviewed_scopes(contexts) == frozenset(ctx.scope for ctx in contexts) != frozenset()

def test_reviewed_scopes_empty_in_a_dry_run(tmp_path):
    # same arrange as test_dry_run_builds_no_reviewer, the repo with reviewer.enabled
    assert _reviewed_scopes(_reviewer_contexts(_global(tmp_path), on, on_auth_failure, dry_run=True)) == frozenset()

def test_reviewed_scopes_ignore_the_switch(tmp_path):
    # _reviewed_scopes takes contexts only: no Switches argument exists to consult
    import inspect
    assert list(inspect.signature(_reviewed_scopes).parameters) == ["contexts"]
```

`tests/unit/test_action_stage.py`:

```python
def test_set_reviewed_repos():
    from orchestrator.orchestrator import Orchestrator
    orch = SimpleNamespace()
    Orchestrator.set_reviewed_repos(orch, frozenset({"test-co/test-repo"}))
    assert orch._reviewed_repos == frozenset({"test-co/test-repo"})
```

- [ ] **Step 2: Run, expect FAIL** (`ImportError: _reviewed_scopes`, `AttributeError: set_reviewed_repos`)

- [ ] **Step 3: Implement** `set_reviewed_repos` (docstring: "The repos whose PRs reviewer mode handles: there the ticket pipeline hands a PR over instead of running its own PR flow.") and `_reviewed_scopes`; in `main.py` call `orchestrator.set_reviewed_repos(_reviewed_scopes(reviewer_contexts))` on the line after `reviewer_contexts` is built (outside the `if reviewer_contexts:` block, so an empty set is stored too).

- [ ] **Step 4: Run both test files, expect PASS**

- [ ] **Step 5: Commit** — `feat(pipeline): tell the orchestrator which repos reviewer mode owns`

---

### Task 3: `pr_review` hands the PR over

**Files:**
- Modify: `orchestrator/pipeline/actions/fetch_pr_comments.py` (new function after `build_public_reply`)
- Modify: `orchestrator/orchestrator.py::_handle_action_stage` (the `_fetch` closure and the `handle_action_stage(...)` call) and `::_on_ticket_done`
- Modify: `orchestrator/pipeline/action_stage.py` (new keyword; the "PR opened" block at the end)
- Modify: `orchestrator/pipeline/actions/finalize.py::on_ticket_done`
- Modify: `orchestrator/tg_uk/pipeline.py` (one new entry)
- Test: `tests/unit/test_action_stage.py`, `tests/unit/test_orchestrator_finalize.py`

**Interfaces:**
- Consumes: `self._reviewed_repos` (Task 2).
- Produces:
  - `hand_over_to_reviewer(workspace: Workspace) -> ActionResult` in `fetch_pr_comments.py`: sets `state.pending_review_comments = None` and `state.human_input_reply = None`, `workspace.save_state()`, logs `"PR handed over to reviewer mode"` plus `" ({n} undecided comment(s) dropped)"` when `n > 0` via `_log_pipeline`, returns `ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})`. `n` = pending entries whose `decision` is `None`.
  - `handle_action_stage(..., reviewer_owns: bool = False)`: when true, the "PR opened … Review Complete" message and the `escalation_msg_id` bookkeeping after `push_and_open_pr` are skipped.
  - `on_ticket_done(workspace, notifier, chat_id, tracker, in_review_status, handed_over: bool = False)`: when true the message body is the handover template of the spec, verbatim:
    `"PR opened: {pr_url}\n\nReviewer mode takes it from here: its turns and review threads come to the reviewer chat.\n\nTicket moved to review status."`
    The header line (`tg_header("✅", …)`) and the tracker transition are unchanged.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_action_stage.py`, in `TestActionFetchPrComments`:

```python
    def test_hand_over_drops_pending_escalations(self):
        from orchestrator.pipeline.actions.fetch_pr_comments import hand_over_to_reviewer
        ws = _fake_workspace(state=Stage.PR_REVIEW, pr_number=10)
        ws.state.pending_review_comments = [{"comment_id": 1, "decision": None}, {"comment_id": 2, "decision": "fix"}]
        ws.state.human_input_reply = "reviewed"
        with patch("orchestrator.pipeline.actions.fetch_pr_comments._log_pipeline") as log:
            result = hand_over_to_reviewer(ws)
        assert (result.success, result.next_state, result.skipped) == (True, Stage.DONE, False)
        assert ws.state.pending_review_comments is None and ws.state.human_input_reply is None
        ws.save_state.assert_called_once()
        log.assert_called_once_with(ws, "PR handed over to reviewer mode (1 undecided comment(s) dropped)")
```

In `TestHandleActionStage` (same patches as `test_skipped_action_no_transition`, plus `on_ticket_done`):

```python
    @pytest.mark.asyncio
    async def test_pr_review_hands_over_in_a_reviewer_repo(self):
        from orchestrator.orchestrator import Orchestrator
        orch = self._make_orchestrator()
        orch._reviewed_repos = frozenset({"test-co/test-repo"})
        orch._on_ticket_done = AsyncMock()
        ws = _fake_workspace(state=Stage.PR_REVIEW, pr_number=10)
        fetch_mock = AsyncMock()
        with patch("orchestrator.pipeline.action_stage.stage_verifier") as sv, \
             patch("orchestrator.pipeline.action_stage.should_approval_gate", return_value=False), \
             patch("orchestrator.pipeline.actions.fetch_pr_comments._log_pipeline"), \
             patch("orchestrator.orchestrator.action_fetch_pr_comments", new=fetch_mock):
            sv.capture_stage_start = MagicMock(return_value=None)
            sv.verify = MagicMock(return_value=SimpleNamespace(ok=True, stage_id="pr_review", reason=""))
            await Orchestrator._handle_action_stage(orch, ws, "pr_review", self._make_stage_def("fetch_pr_comments"))
        fetch_mock.assert_not_awaited()
        ws.transition.assert_called_with(Stage.DONE)
        orch._on_ticket_done.assert_awaited_once_with(ws)

    @pytest.mark.asyncio
    async def test_fetch_runs_for_an_unknown_scope(self):
        # identical, but orch._reviewed_repos = frozenset({"other/repo"}) and
        # fetch_mock = AsyncMock(return_value=ActionResult(success=False, next_state="", error="", metadata={}, skipped=True))
        # assert fetch_mock.assert_awaited_once(); ws.transition.assert_not_called()

    @pytest.mark.asyncio
    async def test_push_sends_no_review_complete_message_in_a_reviewer_repo(self):
        # arrange as test_happy_path_push, with orch._notifier = AsyncMock(), orch._get_chat_id returning "chat-1",
        # orch._reviewed_repos = frozenset({"test-co/test-repo"})
        # assert orch._notifier.send_message.await_count == 0 and ws.transition.assert_called_with(Stage.PR_REVIEW)

    @pytest.mark.asyncio
    async def test_push_still_sends_review_complete_elsewhere(self):
        # same arrange without _reviewed_repos: send_message awaited once, its text contains "Review Complete" hint
        # ("tap Review Complete") and buttons == [Button(label="Review Complete", action="reviewed:T-1")]
```

`tests/unit/test_orchestrator_finalize.py`:

```python
@pytest.mark.asyncio
async def test_handed_over_text(monkeypatch) -> None:
    notifier = AsyncMock()
    monkeypatch.setattr("orchestrator.tg_format.read_ticket_title", lambda w: "Sample title")
    await on_ticket_done(_ws(), notifier, "chat-1", AsyncMock(), "", handed_over=True)
    text = notifier.send_message.await_args.args[1]
    assert text.endswith(
        "PR opened: https://g/pr/42\n\nReviewer mode takes it from here: its turns and review threads come to "
        "the reviewer chat.\n\nTicket moved to review status.")
    assert "ready for merge" not in text

@pytest.mark.asyncio
async def test_not_handed_over_text_is_unchanged(monkeypatch) -> None:
    # same call without handed_over: text ends with
    # "Pipeline complete.\n\nPR ready for merge: https://g/pr/42\n\nTicket moved to review status."
```

- [ ] **Step 2: Run, expect FAIL** (`ImportError: hand_over_to_reviewer`; `TypeError: … handed_over`)

- [ ] **Step 3: Implement**

In `Orchestrator._handle_action_stage` compute once, before the closures:

```python
        owns = scope_of(workspace.state.company_id, workspace.state.repo_id) in getattr(
            self, "_reviewed_repos", frozenset())
```

`_fetch` returns `hand_over_to_reviewer(ws)` when `owns`, else the existing call. Pass `reviewer_owns=owns` to `handle_action_stage`. `_on_ticket_done` computes the same membership for its workspace and passes `handed_over=`. Add the Ukrainian text for the handover template to `orchestrator/tg_uk/pipeline.py`.

- [ ] **Step 4: Run** `tests/unit/test_action_stage.py tests/unit/test_orchestrator_finalize.py tests/unit/test_tg_i18n.py tests/unit/test_orchestrator_pr_review_loop.py -q`, expect PASS

- [ ] **Step 5: Run the whole suite** `.venv/bin/python -m pytest tests -q -x`, expect PASS

- [ ] **Step 6: Commit** — `feat(pipeline): hand the PR over to reviewer mode where it is on`

---

### Task 4: Documents

**Files:**
- Modify: `docs/superpowers/specs/2026-11-12-reviewer-mode-design.md` — the "The ticket pipeline's own PRs" bullet of "The board" (~line 444) and item 15 of its test list (~line 704)
- Modify: `docs/features/pr-comment-responder.md` — one Change Log row
- Modify: `docs/telegram.md`, `docs/dashboard.md` — only where they describe the «конвейер»/pipeline label or "Review Complete" as unconditional (`grep -n "Review Complete\|pipeline's own\|конвейер" docs/*.md`)

- [ ] **Step 1:** Replace the board bullet with: "**The ticket pipeline's own PRs** … show on the board labelled `pipeline` and are handled like any other PR of the operator's: the pipeline hands a PR over right after opening it (see `2026-11-18-reviewer-owns-prs-design.md`)." Replace test 15 with "The ticket pipeline's own PRs are labelled on the board and get jobs and turn lines like any other."
- [ ] **Step 2:** Change Log row, dated 2026-11-18: "The PR flow (`fetch_pr_comments`) runs only in a repo without reviewer mode; elsewhere `pr_review` hands the PR over (`hand_over_to_reviewer`) and the ticket goes `DONE`."
- [ ] **Step 3: Commit** — `docs: reviewer mode owns every PR where it is on`

---

## After merge (operator)

Restart the daemon. Tickets sitting in `PR_REVIEW` for a reviewer-mode repo go `DONE` on the next poll cycle, each with one handover message.
