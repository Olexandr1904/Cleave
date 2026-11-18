# Skill Flow Comparison Implementation Plan (plan 2 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On a repo with `ticket_flow: both`, every ticket worked by both flows ends in two draft PRs, one comparison message in Telegram with the buttons Staged / Skill / Neither, and — on the operator's tap only — the verdict's forge and tracker writes; the dashboard shows every comparison and the tally.

**Architecture:** The comparison lives in `orchestrator/skillflow/` next to the skill runs: one more table in reviewer.db (`comparisons`), a store with locked read-modify-write methods only, a side reader that says whether each side is at rest, and a `ComparisonKeeper` — its own asyncio task, never the skill runner's — that sends the comparison message when both sides are at rest and carries out a verdict step by step. Comparison messages are anchored in `skill_anchors` under the comparison's id (`cmp:<hex>`), and their buttons use the `sk_` prefix, so the command handler routes them to `SkillReplies` unchanged. The staged pipeline changes only for a workspace that has a comparison row and whose repo is on `both`.

**Tech Stack:** Python 3.12, asyncio, sqlite3, httpx + GraphQL (GitHub adapter), Starlette (dashboard), vanilla JS modules (dashboard/static), pytest + pytest-asyncio + respx.

**Spec:** `docs/superpowers/specs/2026-11-16-skill-flow-comparison-design.md` — sections "The comparison", "Changes to the staged flow" items 2–4, and the dashboard's Comparison section. Its "Decided with the operator" list is binding. Plan 1 as built: `docs/skill-flow.md` and `orchestrator/skillflow/` (read the code; the plan-1 ledger's rulings changed details).

## Global Constraints

- `ticket_flow: staged` (the default) changes nothing. A staged-flow change runs only when `Orchestrator._is_compared(ws)` is true: the workspace's repo is on `ticket_flow: both` **and** a comparison row exists for that workspace (scope, ticket id, `started_at`).
- The verdict's forge writes (mark ready, retitle, comment and close the other PR) and the held tracker writes happen only after the operator's tap on Staged, Skill, Neither or Retry, inside `ComparisonKeeper` in Cleave's process. No force anywhere. Text written to GitHub or the tracker is English.
- No lost update. A skill run is changed outside the runner only through `SkillStore.claim`; the runner keeps plan 1's rule (it saves a run object only while it is `running` or in its `gates`). A comparison is changed only through `ComparisonStore` methods that read and write the row inside one lock hold; there is no `save` of a comparison object.
- Only the skill runner's task runs git in the skill checkout. The keeper runs no git.
- Tests never start the real `claude` and never reach a forge or tracker: fakes, `tmp_path`, local bare repos, respx.
- Telegram text Cleave writes goes through `tr()` with its Ukrainian in `orchestrator/tg_uk/skillflow.py` (`tests/unit/test_tg_i18n.py` must pass). The skill's own relayed text and run-time reasons (git, forge and gate output, stop reasons) are sent as they are: not marked with `model_text`, not translated. GitHub, tracker, dashboard and stored texts stay English.
- Usage is shown in tokens, never dollars (the host runs on a subscription).
- No employer names anywhere: code, tests, fixtures, docs, commit messages, branch names. Examples use `acme`, ticket keys `ACME-1`.
- No `git commit` during execution; the working tree is snapshotted per task. Run tests only through the ledger's `runtests` wrapper. Baseline: 9 known failures (`test_not_a_git_dir`, three in `tests/unit/test_main.py`, three in `tests/integration/test_e2e_dry_run.py`, `test_build_jql_ands_multiple_trigger_labels`, `test_bad_payload_returns_400`). Never run `tests/unit/test_main.py` or `test_main_startup.py` on their own.
- Times are UTC ISO-8601 strings in the store; every time-dependent function takes `now`.

## Review Focus

1. **A tap after a side moved on.** The operator taps Skill on the newest comparison message seconds after replying to the skill run's stop (it is queued again), or taps an older comparison message. Expected: nothing is written to GitHub or the tracker; the operator is told why; a new message comes once both sides rest again. (Tasks 8, 9: the tap checks the message; the keeper re-reads both sides before the first step.)
2. **A forge write fails half-way** (GitHub 5xx, a 422, a timeout). Expected: the verdict is `stuck` with the reason and a Retry button; Retry continues from the step that failed; no step that succeeded runs again, and a loser PR already closed gets no second comment. (Task 8)
3. **The daemon restarts during a verdict.** Expected: the keeper continues the `deciding` comparison from its recorded steps at start-up; only the step in flight can run twice, and each step is safe to repeat except the loser comment, which is skipped when the PR is already closed. (Task 8)
4. **A `both` repo's ticket that has no comparison** (the intake made no run: the repo is not served, or an older run of the ticket has not ended), and every `staged` repo. Expected: the staged flow exactly as today — a ready PR without a title mark, the tracker writes at push time, "Review Complete" offered, comments processed. (Task 4)
5. **The staged workspace is gone or unreadable** (deleted by the age cleanup, a broken state.json). Expected: the staged side counts as at rest with no PR; the comparison message says so; a verdict skips the staged step with a line in the result, and nothing raises out of the keeper's loop. (Tasks 6, 8)

---

## File structure

| File | Responsibility |
|---|---|
| `orchestrator/skillflow/steps.py` (new) | `Stop`, `Deferred`, `fill`, `session_failed`: shared by the runner and the publish steps |
| `orchestrator/skillflow/publish.py` (new) | the commit, gates, push and PR steps, moved out of `runner.py` unchanged |
| `integrations/github/github_adapter.py` | `pr_info`, `mark_ready`, `set_title`, `comment_on_pr` |
| `orchestrator/reviewer/db.py` | migration 6: `comparisons` |
| `orchestrator/skillflow/compare_models.py` (new) | `Comparison`, its states, the side states that count as at rest |
| `orchestrator/skillflow/compare_store.py` (new) | `ComparisonStore` |
| `orchestrator/skillflow/models.py`, `store.py` | `SkillRun.released`; `SkillStore.has_anchors` |
| `orchestrator/pr_creation.py`, `orchestrator/pipeline/actions/push_and_open_pr.py`, `fetch_pr_comments.py`, `orchestrator/pipeline/action_stage.py`, `orchestrator/orchestrator.py` | the staged flow on a compared ticket |
| `orchestrator/skillflow/intake.py` | the comparison row at intake |
| `orchestrator/skillflow/sides.py` (new) | what each side is now: at rest, PR, time, tokens; the workspace lookup; ending a workspace |
| `workspace/workspace.py`, `dashboard/actions.py` | `archive_workspace`, the archive path shared by the dashboard and the verdict |
| `orchestrator/skillflow/compare_messages.py` (new) | the comparison's Telegram texts and buttons |
| `orchestrator/skillflow/verdict.py` (new) | the verdict's steps |
| `orchestrator/skillflow/keeper.py` (new) | `ComparisonKeeper`: the loop, the message, the verdict, the outbox |
| `orchestrator/skillflow/replies.py` | verdict taps, Retry, notes, decided runs; `SkillOff` |
| `orchestrator/skillflow/runner.py` | the sweep removes a released run's worktree (Task 10); messages in the project's language (Task 11) |
| `orchestrator/skillflow/messages.py`, `integrations/telegram/command_handler.py`, `orchestrator/tg_uk/skillflow.py` (new), `orchestrator/tg_uk/__init__.py` | the skill flow's Telegram texts through `tr()` |
| `orchestrator/skillflow/board.py` (new), `dashboard/comparisons_api.py` (new), `dashboard/web.py`, `dashboard/static/{index.html,js/app.js,js/api.js,js/comparison.js,style.css}` | the dashboard's Comparison section |
| `main.py` | wiring |
| `docs/skill-flow.md`, `deploy/README.md` | operator docs |

---

### Task 1: Split the publish steps out of `runner.py`

A pure move, so plan 2's additions land in files of a size one reader can hold (the plan-1 final review's cut). No behaviour changes; the tests change only where they patch a name that moved.

**Files:**
- Create: `orchestrator/skillflow/steps.py`, `orchestrator/skillflow/publish.py`
- Modify: `orchestrator/skillflow/runner.py`
- Modify (patch targets only): `tests/unit/test_skillflow_publish.py`, `tests/unit/test_skillflow_runner.py`, `tests/unit/skillflow_rig.py`

**Interfaces:**
- Produces:

```python
# steps.py — moved from runner.py, renamed without the leading underscore (shared by two modules now)
class Stop(Exception):                      # was _Stop; same __init__(reason, detail="")
class Deferred(Exception):                  # was _Deferred
def fill(pattern: str, key: str) -> str     # was _fill
def session_failed(checkout: SkillCheckout, outcome: TurnOutcome) -> Stop   # was _session_failed

# publish.py
DESCRIPTION_LIMIT, TITLE_LIMIT, MAX_REGATES, _GATE_TAIL, _FORGE_REASON, _FORGE_ERRORS, _gate_detail  # moved
class PublishSteps:
    """The commit, gates and push steps of SkillRunner (a mixin): it uses the host's _turn, _save, _park, _emit,
    _env, _worktree, _notes_rel, _slot, _from_deferred and _stamp."""
    # moved unchanged from runner.py: _publish, _commit, _gates_again, _gates, _run_gates, _push, _open_pr,
    # _publish_turn, _check_commit, _commit_problem, _description, _description_path, _head

# runner.py
class SkillRunner(PublishSteps): ...       # everything else stays here
```

- [ ] **Step 1: Baseline.** Run the whole suite through `runtests`: only the 9 known failures.
- [ ] **Step 2: Move** the names above (runner.py's `_publish` through `_head`, about lines 452–630, and the constants and `_gate_detail` they alone use) into `publish.py`, and `_Stop`, `_Deferred`, `_fill`, `_session_failed` into `steps.py`. Every use in both modules takes the new name. The module docstrings say what each holds.
- [ ] **Step 3: Retarget the test patches** whose name now lives in `publish.py`: `monkeypatch.setattr(runner_module, "run_gates", …)`, `(runner_module, "pr_message", …)` and `runner_module.MAX_REGATES` become `publish_module` (`from orchestrator.skillflow import publish as publish_module`); the rig's `run_gates` spy likewise. No assertion changes.
- [ ] **Step 4: Run** `runtests t1 tests/unit/test_skillflow_runner.py tests/unit/test_skillflow_publish.py` then the whole suite — expected: the same pass count as Step 1, no `NEW:` line. `wc -l` of `runner.py` is under 500.

---

### Task 2: GitHub adapter — read, mark ready, retitle, comment

**Files:**
- Modify: `integrations/github/github_adapter.py`
- Test: `tests/unit/test_github_adapter_verdict.py` (`@respx.mock`, as `tests/unit/test_github_adapter_prs.py`)

**Interfaces:**
- Produces (GitHub adapter only; `VCSInterface` and the GitLab adapter stay as they are):

```python
@dataclass(frozen=True)
class PRInfo:
    number: int
    title: str
    state: str        # "open" | "closed" (GitHub's; a merged PR is "closed")
    draft: bool
    node_id: str
    url: str

async def pr_info(self, pr_number: int) -> PRInfo                     # GET pulls/{n}
async def mark_ready(self, pr_number: int) -> None                    # GraphQL markPullRequestReadyForReview
async def set_title(self, pr_number: int, title: str) -> None         # PATCH pulls/{n} {"title": …}
async def comment_on_pr(self, pr_number: int, body: str) -> None      # POST issues/{n}/comments {"body": …}
```

- [ ] **Step 1: Write the failing tests**

```python
API = "https://api.github.com/repos/acme/app"
GQL = "https://api.github.com/graphql"
PR = {"number": 7, "title": "ACME-1: Fix login [skill]", "state": "open", "draft": True,
      "node_id": "PR_kw7", "html_url": "https://github.com/acme/app/pull/7"}

@respx.mock
async def test_pr_info(adapter):
    respx.get(f"{API}/pulls/7").respond(200, json=PR)
    assert await adapter.pr_info(7) == PRInfo(7, "ACME-1: Fix login [skill]", "open", True, "PR_kw7", PR["html_url"])

@respx.mock
async def test_mark_ready_sends_the_mutation_with_the_node_id(adapter):
    respx.get(f"{API}/pulls/7").respond(200, json=PR)
    gql = respx.post(GQL).respond(200, json={"data": {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}}})
    await adapter.mark_ready(7)
    body = json.loads(gql.calls[0].request.content)
    assert "markPullRequestReadyForReview" in body["query"] and body["variables"] == {"id": "PR_kw7"}

@respx.mock
async def test_mark_ready_of_a_ready_pr_sends_nothing(adapter):
    respx.get(f"{API}/pulls/7").respond(200, json={**PR, "draft": False})
    gql = respx.post(GQL).respond(200, json={})
    await adapter.mark_ready(7)
    assert not gql.called

@respx.mock
async def test_mark_ready_raises_on_graphql_errors(adapter):
    respx.get(f"{API}/pulls/7").respond(200, json=PR)
    respx.post(GQL).respond(200, json={"errors": [{"message": "Resource not accessible by integration"}]})
    with pytest.raises(RuntimeError, match="not accessible"):
        await adapter.mark_ready(7)

@respx.mock
async def test_set_title(adapter):
    route = respx.patch(f"{API}/pulls/7").respond(200, json=PR)
    await adapter.set_title(7, "ACME-1: Fix login")
    assert json.loads(route.calls[0].request.content) == {"title": "ACME-1: Fix login"}

@respx.mock
async def test_comment_on_pr(adapter):
    route = respx.post(f"{API}/issues/7/comments").respond(201, json={"id": 1})
    await adapter.comment_on_pr(7, "Closed by Cleave")
    assert json.loads(route.calls[0].request.content) == {"body": "Closed by Cleave"}
```

- [ ] **Step 2: Run** `runtests t2 tests/unit/test_github_adapter_verdict.py` — expected: FAIL (no `PRInfo`).
- [ ] **Step 3: Implement** the four methods with `_request` for REST (its retries and `RuntimeError` stay) and `_graphql_request` for the mutation; `mark_ready` reads `pr_info` first and sends nothing for a PR that is not a draft. A GraphQL answer with an `errors` key raises `RuntimeError` with the first error's message (cut to 300 characters).
- [ ] **Step 4: Run** the file — expected: PASS. Then the whole suite.

---

### Task 3: The comparison store

**Files:**
- Modify: `orchestrator/reviewer/db.py` (migration 6), `tests/unit/test_reviewer_store.py:27` (`== 5` → `== 6`)
- Create: `orchestrator/skillflow/compare_models.py`, `orchestrator/skillflow/compare_store.py`
- Modify: `orchestrator/skillflow/models.py` (`released`), `orchestrator/skillflow/store.py` (`has_anchors`)
- Test: `tests/unit/test_skillflow_compare_store.py`

**Interfaces:**
- Produces:

```python
# db.py, migration 6
"CREATE TABLE comparisons (id TEXT PRIMARY KEY, scope TEXT NOT NULL, ticket_id TEXT NOT NULL, run_id TEXT NOT NULL, "
"state TEXT NOT NULL, unsent INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
"data TEXT NOT NULL); CREATE INDEX comparisons_ticket ON comparisons (scope, ticket_id); "
"CREATE INDEX comparisons_run ON comparisons (run_id)"

# compare_models.py
COMPARISON_STATES = ("open", "deciding", "stuck", "closed")
VERDICTS = ("staged", "skill", "neither")
SKILL_AT_REST = ("pr_open", "stopped", "no_change", "failed", "dropped")
STAGED_AT_REST = ("PR_REVIEW", "BLOCKED", "FAILED", "DONE", "ARCHIVED")
STEPS = ("ready", "title", "comment", "close", "tracker", "skill", "staged")   # the verdict's steps, in order
ID_PREFIX = "cmp:"   # a comparison id is "cmp:<12 hex>": it is also the run_id of its anchors in skill_anchors

@dataclass
class Comparison:
    # Fields added later must have defaults: rows already stored lack them.
    id: str
    scope: str
    ticket_id: str
    run_id: str
    state: str
    created_at: str
    updated_at: str
    unsent: int = 0
    # one JSON object in `data`:
    chat_id: str = ""
    summary: str = ""
    skill: str = ""                  # the skill's name, for the message
    workspace_root: str = ""
    workspace_started_at: str = ""   # with ticket_id, which workspace this is
    rest_since: dict = field(default_factory=dict)   # {"skill": iso, "staged": iso}: since when each side rests
    fingerprint: str = ""            # the pair the newest message shows; "" while no message stands
    messages: int = 0                # comparison messages built so far: a tap's claim expects this value
    ask_message_ids: list[int] = field(default_factory=list)   # every part of the newest message with buttons
    outbox: list[dict] = field(default_factory=list)  # {"text": str, "buttons": [[label, action], …], "ids": [int]}
    sides: dict = field(default_factory=dict)  # {"skill": SideView.as_dict(), "staged": …} as the newest message shows them
    verdict: str = ""
    note: str = ""
    decided_at: str = ""
    closed_at: str = ""
    steps_done: list[str] = field(default_factory=list)
    reason: str = ""                 # why the verdict is stuck
    report: list[str] = field(default_factory=list)   # lines for the verdict's closing message

# compare_store.py
class ComparisonStore:
    def __init__(self, conn: sqlite3.Connection)
    def add(self, scope: str, ticket_id: str, run_id: str, now: str, **data) -> Comparison   # state "open"
    def get(self, cid: str) -> Comparison | None              # None also for a row that cannot be read
    def for_run(self, run_id: str) -> Comparison | None
    def for_workspace(self, scope: str, ticket_id: str, started_at: str) -> Comparison | None
    def in_states(self, states: tuple[str, ...]) -> list[Comparison]   # oldest first
    def unsent(self) -> list[Comparison]
    def newest_first(self, limit: int = 200) -> list[Comparison]
    def claim(self, cid: str, from_states: tuple[str, ...], now: str, state: str, *,
              expect: dict | None = None, message: dict | None = None, **changes) -> Comparison | None
    def drop_sent(self, cid: str, count: int, now: str, ask_message_ids: list[int] | None = None) -> None
    def mark_parts(self, cid: str, ids: list[int], now: str) -> None
    def add_note(self, cid: str, text: str, now: str) -> Comparison | None

# models.py — SkillRun gains
released: bool = False   # the comparison has its verdict: the worktree is no longer kept for it (the sweep removes it)

# store.py — SkillStore gains
def has_anchors(self) -> bool
```

`claim` reads the row, checks the state is in `from_states` and every `expect` field equals its value, applies `changes`, appends `message` to the outbox when given, and writes — all inside the lock; otherwise it returns None and writes nothing. `add_note` appends `text` to `note` (a blank line between two notes) on the row as it is, in any state. `drop_sent` and `mark_parts` mirror `SkillStore`'s. A row whose data cannot be read is skipped by every list with one warning naming its id (plan 1's Ruling 16).

- [ ] **Step 1: Write the failing tests**

```python
def store(tmp_path):
    return ComparisonStore(connect(tmp_path / "reviewer.db"))

def test_add_and_read(tmp_path):
    s = store(tmp_path)
    c = s.add("acme/app", "ACME-1", "run1", NOW, chat_id="-100", workspace_started_at="T0")
    assert c.id.startswith("cmp:") and c.state == "open"
    assert s.get(c.id).chat_id == "-100"
    assert s.for_run("run1").id == c.id
    assert s.for_workspace("acme/app", "ACME-1", "T0").id == c.id
    assert s.for_workspace("acme/app", "ACME-1", "T1") is None

def test_claim_expects_fields(tmp_path):
    s = store(tmp_path); c = s.add("acme/app", "ACME-1", "run1", NOW)
    s.claim(c.id, ("open",), NOW, "open", messages=2)
    assert s.claim(c.id, ("open",), NOW, "deciding", expect={"messages": 1}, verdict="skill") is None
    taken = s.claim(c.id, ("open",), NOW, "deciding", expect={"messages": 2}, verdict="skill")
    assert taken.state == "deciding" and s.get(c.id).verdict == "skill"

def test_claim_appends_a_message_inside_the_lock(tmp_path):
    s = store(tmp_path); c = s.add("acme/app", "ACME-1", "run1", NOW)
    s.claim(c.id, ("open",), NOW, "open", message={"text": "a", "buttons": []})
    s.claim(c.id, ("open",), NOW, "open", message={"text": "b", "buttons": []})
    assert [e["text"] for e in s.get(c.id).outbox] == ["a", "b"] and s.get(c.id).unsent == 2

def test_a_note_survives_a_later_claim(tmp_path):
    s = store(tmp_path); c = s.add("acme/app", "ACME-1", "run1", NOW)
    s.add_note(c.id, "skill is cleaner", NOW)
    s.claim(c.id, ("open",), NOW, "open", fingerprint="x")
    s.add_note(c.id, "but slower", NOW)
    assert s.get(c.id).note == "skill is cleaner\n\nbut slower"

def test_unknown_change_or_state_is_refused(tmp_path):
    s = store(tmp_path); c = s.add("acme/app", "ACME-1", "run1", NOW)
    with pytest.raises(AttributeError):
        s.claim(c.id, ("open",), NOW, "open", nonsense=1)
    with pytest.raises(ValueError):
        s.claim(c.id, ("open",), NOW, "maybe")

def test_a_damaged_row_is_skipped(tmp_path, caplog):
    s = store(tmp_path); s.add("acme/app", "ACME-1", "run1", NOW); bad = s.add("acme/app", "ACME-2", "run2", NOW)
    s._conn.execute("UPDATE comparisons SET data = '{\"rest_since\": 3, \"x' WHERE id = ?", (bad.id,))
    assert [c.ticket_id for c in s.in_states(("open",))] == ["ACME-1"]
    assert bad.id in caplog.text

def test_skill_store_has_anchors(tmp_path):
    runs = SkillStore(connect(tmp_path / "reviewer.db"))
    assert runs.has_anchors() is False
    runs.add_anchor("-100", 5, "run1", NOW)
    assert runs.has_anchors() is True

def test_released_defaults_false_for_stored_runs(tmp_path):
    runs = SkillStore(connect(tmp_path / "reviewer.db"))
    run = runs.add_run("acme/app", "ACME-1", NOW)
    assert runs.run(run.id).released is False
```

- [ ] **Step 2: Run** `runtests t3 tests/unit/test_skillflow_compare_store.py` — expected: FAIL.
- [ ] **Step 3: Implement** the migration, the model, the store (modelled on `SkillStore`: an upsert that keeps the rowid, oldest first by `created_at, rowid`, `newest_first` the reverse), `released` and `has_anchors` (`SELECT 1 FROM skill_anchors LIMIT 1`).
- [ ] **Step 4: Run** the file and `tests/unit/test_reviewer_store.py` — expected: PASS. Then the whole suite.

---

### Task 4: The staged flow on a compared ticket

**Files:**
- Modify: `orchestrator/pr_creation.py`, `orchestrator/pipeline/actions/push_and_open_pr.py`, `orchestrator/pipeline/actions/fetch_pr_comments.py`, `orchestrator/pipeline/action_stage.py`, `orchestrator/orchestrator.py`
- Test: `tests/unit/test_staged_compared.py`; the existing tests of these modules must pass unchanged.

**Interfaces:**
- Consumes: `ComparisonStore.for_workspace` (Task 3), `scope_of`.
- Produces:

```python
# pr_creation.py
STAGED_MARK = " [staged]"
TITLE_LIMIT = 256
async def create_pr(workspace, vcs, tracker, repo_config, event_bus=None, *, compared: bool = False) -> PRCreationResult
async def tracker_writes_for_pr(tracker, repo_config: RepoConfig, ticket_id: str, pr_url: str) -> list[str]
    # the move to in review and the "PR opened: <url>" comment, as create_pr's AC4 does them today (provider-aware);
    # returns one English line per write that failed ("move to In Review failed: TimeoutError"), [] when both landed

# push_and_open_pr.py
async def action_push_and_open_pr(workspace, vcs, repo_config, notifier, chat_id, tracker, event_bus,
                                  *, compared: bool = False) -> ActionResult

# fetch_pr_comments.py
async def action_fetch_pr_comments(workspace, stage_def, *, get_vcs, get_chat_id, tracker, notifier, agent_runtime,
                                   event_bus=None, compared: bool = False) -> ActionResult

# action_stage.py — handle_action_stage gains
is_compared: Callable[[Workspace], bool] = lambda ws: False

# orchestrator.py
def set_comparisons(self, comparisons) -> None       # the ComparisonStore
def _is_compared(self, workspace: Workspace) -> bool
```

Behaviour with `compared=True` (and only then):
- `create_pr`: the title is `f"{ticket_id}: {summary} [staged]"`, cut to `TITLE_LIMIT` as the skill flow's (`f"{ticket_id}: {summary[:room]}… [staged]"`); `open_pr(…, draft=True)`; no tracker write. With `compared=False` it calls `tracker_writes_for_pr` and ignores the lines (their warnings are logged as today) — the same two calls in the same order as now.
- `action_fetch_pr_comments`: returns `ActionResult(success=False, next_state="", error="", metadata={}, skipped=True)` before Phase 1: no comment is fetched, verified, answered or resolved, and `human_input_reply` is not read.
- `handle_action_stage`, after a successful `push_and_open_pr` of a compared workspace: the PR message is `tr("Draft PR opened: {pr_url}\n\nThis ticket is compared with the skill flow: Cleave answers no review comments on it, and makes the tracker writes once you pick the better PR.", pr_url=pr_url)` with no buttons, and `escalation_msg_id` / `escalation_chat_id` / `human_input_reply` are left as they are.
- `Orchestrator._is_compared(ws)`: `False` when no store is set or `_get_repo_config(ws).ticket_flow != "both"` (no lookup then); else whether `for_workspace(scope_of(company_id, repo_id), ticket_id, started_at)` finds a row. A store error propagates: the stage fails loudly rather than open a non-draft PR or answer comments on a compared ticket. The shims `_push` and `_fetch` pass `compared=self._is_compared(ws)`; `_handle_action_stage` passes `is_compared=self._is_compared`.

- [ ] **Step 1: Write the failing tests** (fakes: a `FakeVCS` that records `push`/`open_pr` calls, a `FakeTracker` that records `transition_ticket`/`add_comment`; a workspace from `tmp_path` with `ticket.md` "# ACME-1: Fix login" and a scope certificate)

```python
async def test_compared_pr_is_a_draft_with_the_mark_and_no_tracker_write(ws, vcs, tracker, repo_cfg):
    result = await create_pr(ws, vcs, tracker, repo_cfg, compared=True)
    assert result.success
    assert vcs.opened == [("ACME-1: Fix login [staged]", "develop", True)]   # (title, base, draft)
    assert tracker.calls == []

async def test_uncompared_pr_is_unchanged(ws, vcs, tracker, repo_cfg):
    await create_pr(ws, vcs, tracker, repo_cfg)
    assert vcs.opened == [("ACME-1: Fix login", "develop", False)]
    assert tracker.calls == [("transition", "ACME-1", "In Review"), ("comment", "ACME-1", "PR opened: u")]

async def test_long_title_is_cut_before_the_mark(ws_long_title, vcs, tracker, repo_cfg):
    await create_pr(ws_long_title, vcs, tracker, repo_cfg, compared=True)
    title = vcs.opened[0][0]
    assert len(title) == 256 and title.endswith("… [staged]")

async def test_tracker_writes_report_each_failure(repo_cfg):
    tracker = FakeTracker(fail={"transition"})
    assert await tracker_writes_for_pr(tracker, repo_cfg, "ACME-1", "u") == ["move to In Review failed: RuntimeError"]
    assert tracker.calls == [("comment", "ACME-1", "PR opened: u")]

async def test_compared_pr_review_processes_nothing(ws_in_pr_review, vcs):
    ws_in_pr_review.state.human_input_reply = "reviewed"
    result = await action_fetch_pr_comments(ws_in_pr_review, None, get_vcs=lambda: (vcs, None), get_chat_id=lambda: "-100",
                                            tracker=None, notifier=None, agent_runtime=None, compared=True)
    assert result.skipped and vcs.comment_reads == 0
    assert ws_in_pr_review.state.human_input_reply == "reviewed"

async def test_compared_pr_message_has_no_review_complete(rig_action_stage):
    await rig_action_stage.push(compared=True)
    text, buttons = rig_action_stage.notifier.sent[-1]
    assert "Draft PR opened: u" in text and "Review Complete" not in text and buttons is None
    assert rig_action_stage.ws.state.escalation_msg_id is None

def test_is_compared_needs_both_and_a_row(orch_with, comparisons):
    ws = orch_with.workspace("ACME-1", started_at="T0")
    assert orch_with._is_compared(ws) is False                      # no row
    comparisons.add("acme/app", "ACME-1", "run1", NOW, workspace_started_at="T0")
    assert orch_with._is_compared(ws) is True
    orch_with.repo_cfg.ticket_flow = "staged"
    assert orch_with._is_compared(ws) is False                      # staged: no lookup
    assert comparisons.lookups == 1
```

(`rig_action_stage` drives `handle_action_stage` with a fake notifier and an `is_compared` that returns the flag; the compared message keeps the uncompared one's `tg_header` line.)

- [ ] **Step 2: Run** `runtests t4 tests/unit/test_staged_compared.py` — expected: FAIL.
- [ ] **Step 3: Implement** as specified. `tracker_writes_for_pr` keeps today's order (transition, then comment) and today's provider logic, moved out of `create_pr` unchanged.
- [ ] **Step 4: Run** the file and the existing tests of `pr_creation`, `push_and_open_pr`, `fetch_pr_comments`, `action_stage` and `orchestrator` — expected: PASS with no change to an existing test. Then the whole suite.

---

### Task 5: The comparison row at intake

**Files:**
- Modify: `orchestrator/skillflow/intake.py`
- Test: `tests/unit/test_skillflow_intake.py` (new tests)

**Interfaces:**
- Consumes: `ComparisonStore.add` (Task 3).
- Produces: `SkillIntake.__init__(…, comparisons: ComparisonStore | None = None)`. After the run's ticket copy is renamed into place, `create` adds a comparison: `comparisons.add(scope, ticket.id, run.id, now, chat_id=run.chat_id, summary=ticket.summary, skill=run.skill, workspace_root=workspace.state.workspace_root, workspace_started_at=workspace.state.started_at)`. When that add raises, the run is failed with `reason="comparison not stored: <class>"` (a claim from `queued`, as the rename's failure does) and the error propagates: a skill run with no comparison would publish a draft PR that no verdict ever reaches, while the staged side ran as uncompared. No run means no comparison.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_run_gets_its_comparison(rig):
    run = rig.intake.create(rig.ws, rig.ticket, "acme", rig.repo_cfg)
    c = rig.comparisons.for_run(run.id)
    assert (c.scope, c.ticket_id, c.state, c.workspace_started_at) == ("acme/app", "ACME-1", "open", rig.ws.state.started_at)
    assert c.chat_id == run.chat_id and c.skill == run.skill

def test_no_run_no_comparison(rig):
    rig.repo_cfg.ticket_flow = "staged"
    assert rig.intake.create(rig.ws, rig.ticket, "acme", rig.repo_cfg) is None
    assert rig.comparisons.newest_first() == []

def test_a_failed_comparison_add_fails_the_run(rig):
    rig.comparisons.add = raising(sqlite3.OperationalError("locked"))
    with pytest.raises(sqlite3.OperationalError):
        rig.intake.create(rig.ws, rig.ticket, "acme", rig.repo_cfg)
    (run,) = rig.runs.runs_for_ticket("acme/app", "ACME-1")
    assert run.state == "failed" and run.reason == "comparison not stored: OperationalError"
```

- [ ] **Step 2: Run** `runtests t5 tests/unit/test_skillflow_intake.py` — expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the file — expected: PASS. Then the whole suite.

---

### Task 6: The two sides — at rest, PR, time, tokens; ending a workspace

**Files:**
- Create: `orchestrator/skillflow/sides.py`
- Modify: `workspace/workspace.py` (`archive_workspace`), `dashboard/actions.py` (the archive route calls it)
- Test: `tests/unit/test_skillflow_sides.py`; `tests/unit/test_dashboard_actions.py` passes unchanged

**Interfaces:**
- Consumes: `Comparison`, `SKILL_AT_REST`, `STAGED_AT_REST` (Task 3).
- Produces:

```python
# workspace/workspace.py
def archive_workspace(ws: Workspace) -> None
    # the dashboard's archive path, moved: DEFERRED → FAILED → ARCHIVED; FAILED, DONE → ARCHIVED; other states raise
    # InvalidTransitionError as transition() does. The dashboard keeps its own check of the states it offers.

# sides.py
@dataclass(frozen=True)
class SideView:
    state: str            # the run's or the workspace's state; "gone" when there is none to read
    at_rest: bool
    pr_number: int        # 0: no PR
    pr_url: str
    outcome: str          # skill: run.outcome, else run.reason; staged: state.error or ""
    minutes: int          # from intake to when it came to rest (rest_since), else to `now`
    tokens_in: int
    tokens_out: int
    fingerprint: str      # changes whenever the side moved on
    def as_dict(self) -> dict
    @classmethod
    def from_dict(cls, data: dict) -> "SideView"

def skill_view(run: SkillRun | None, rest_since: str, now: datetime) -> SideView
def staged_view(ws: Workspace | None, rest_since: str, now: datetime) -> SideView
def staged_tokens(logs_dir: Path) -> tuple[int, int]
def pair_fingerprint(skill: SideView, staged: SideView) -> str        # f"{skill.fingerprint}#{staged.fingerprint}"

class Workspaces:
    def __init__(self, active: Callable[[], list[Workspace]])
    def find(self, c: Comparison) -> Workspace | None
        # the active workspace with c.ticket_id and started_at == c.workspace_started_at; else Workspace(c.workspace_root)
        # when its state.json is there and matches; else None. An unreadable state.json is None, with one warning.

def end_workspace(ws: Workspace) -> str
    # PR_REVIEW → DONE (no on_ticket_done: no notice, no tracker write); ARCHIVED stays; BLOCKED → FAILED → ARCHIVED;
    # anything else through archive_workspace. Returns the state it ended in.
```

Rules:
- Skill: at rest when `run.state in SKILL_AT_REST`; PR from `run.pr_number` / `run.pr_url`; minutes from `run.created_at`; tokens `run.tokens_in` / `run.tokens_out`; fingerprint `f"{state}|{turn_count}|{reason}|{pr_number}"`.
- Staged: at rest when `current_state in STAGED_AT_REST`; PR from `state.pr_number` / `state.pr_url`; minutes from `state.started_at`; tokens from `staged_tokens(ws.logs_dir)`, the sum of `input_tokens=` and `output_tokens=` over every line of every `*.log` there (the line each agent run's `_write_log` appends); fingerprint `f"{state}|{pr_number}|{error}"` — not `last_updated_at`, which a compared PR review rewrites every poll cycle.
- `None` (no run, no workspace): `SideView("gone", True, 0, "", "", 0, 0, 0, "gone")`.
- Minutes: whole minutes from the start to `rest_since` when it is set, else to `now`.

- [ ] **Step 1: Write the failing tests**

```python
def test_skill_view_of_an_open_pr(run_factory):
    run = run_factory(state="pr_open", pr_number=13, pr_url="u13", created_at="2026-11-17T10:00:00+00:00",
                      tokens_in=800, tokens_out=30, turn_count=4)
    view = skill_view(run, "2026-11-17T11:05:00+00:00", NOW)
    assert (view.at_rest, view.pr_number, view.minutes, view.tokens_in) == (True, 13, 65, 800)

def test_a_waiting_or_interrupted_run_is_not_at_rest(run_factory):
    for state in ("waiting", "interrupted", "queued", "running", "gates", "deferred"):
        assert skill_view(run_factory(state=state), "", NOW).at_rest is False

def test_a_turn_changes_the_fingerprint(run_factory):
    assert skill_view(run_factory(state="stopped", turn_count=3), "", NOW).fingerprint != \
           skill_view(run_factory(state="stopped", turn_count=4), "", NOW).fingerprint

def test_staged_view_reads_the_logs(tmp_path, ws_factory):
    ws = ws_factory(tmp_path, state="PR_REVIEW", pr_number=12, pr_url="u12")
    (ws.logs_dir / "dev.log").write_text(
        "[2026-11-17 10:00:00] model=m input_tokens=1000 output_tokens=40 tool_calls=3 tool_rounds=2 duration=5.0s\n"
        "[2026-11-17 10:10:00] model=m input_tokens=500 output_tokens=10 tool_calls=0 tool_rounds=0 duration=1.0s\n")
    (ws.logs_dir / "qa.log").write_text("garbage\n")
    view = staged_view(ws, "", NOW)
    assert (view.at_rest, view.pr_number, view.tokens_in, view.tokens_out) == (True, 12, 1500, 50)

def test_pr_review_iterations_do_not_move_the_staged_fingerprint(ws_factory, tmp_path):
    ws = ws_factory(tmp_path, state="PR_REVIEW")
    before = staged_view(ws, "", NOW).fingerprint
    ws.increment_iteration("pr_review")
    assert staged_view(ws, "", NOW).fingerprint == before

def test_gone_is_at_rest_with_no_pr():
    assert staged_view(None, "", NOW) == SideView("gone", True, 0, "", "", 0, 0, 0, "gone")

def test_find_matches_started_at(tmp_path, ws_factory, comparison_for):
    ws = ws_factory(tmp_path, state="PR_REVIEW")
    found = Workspaces(lambda: [ws]).find(comparison_for(ws))
    assert found is ws
    assert Workspaces(lambda: [ws]).find(comparison_for(ws, started_at="other")) is None

def test_find_reads_a_done_workspace_from_disk(tmp_path, ws_factory, comparison_for):
    ws = ws_factory(tmp_path, state="DONE")
    assert Workspaces(lambda: []).find(comparison_for(ws)).state.current_state == "DONE"

@pytest.mark.parametrize("start, end, hops", [
    ("PR_REVIEW", "DONE", ["DONE"]), ("BLOCKED", "ARCHIVED", ["FAILED", "ARCHIVED"]),
    ("FAILED", "ARCHIVED", ["ARCHIVED"]), ("DONE", "ARCHIVED", ["ARCHIVED"]), ("ARCHIVED", "ARCHIVED", [])])
def test_end_workspace(tmp_path, ws_factory, start, end, hops):
    ws = ws_factory(tmp_path, state=start)
    seen = record_transitions(ws)
    assert end_workspace(ws) == end and seen == hops
```

- [ ] **Step 2: Run** `runtests t6 tests/unit/test_skillflow_sides.py` — expected: FAIL.
- [ ] **Step 3: Implement.** `dashboard/actions.py`'s archive route keeps its refusal check and its event and calls `archive_workspace(ws)` for the transitions (its MagicMock tests see the same `transition` calls).
- [ ] **Step 4: Run** the file and `tests/unit/test_dashboard_actions.py` — expected: PASS. Then the whole suite.

---

### Task 7: The comparison's Telegram texts

**Files:**
- Create: `orchestrator/skillflow/compare_messages.py`, `orchestrator/tg_uk/skillflow.py`
- Modify: `orchestrator/tg_uk/__init__.py` (`_AREAS` gains `skillflow`)
- Test: `tests/unit/test_skillflow_compare_messages.py`; `tests/unit/test_tg_i18n.py` passes

**Interfaces:**
- Consumes: `Comparison`, `SideView` (Tasks 3, 6).
- Produces (each returns text in the current Telegram language; button labels too; side states, URLs, reasons and the outcome are shown as they are):

```python
def comparison_message(c: Comparison, skill: SideView, staged: SideView) -> str
def verdict_buttons(cid: str, skill: SideView, staged: SideView) -> list[list[str]]
    # [[label, action], …]: Staged only when staged.pr_number, Skill only when skill.pr_number, Neither always;
    # actions "sk_win_staged:<cid>", "sk_win_skill:<cid>", "sk_win_none:<cid>"
def retry_buttons(cid: str) -> list[list[str]]                  # [[tr("Retry"), "sk_retry:<cid>"]]
def moved_on_message(c: Comparison, side: str) -> str           # side: "skill" | "staged"
def stuck_message(c: Comparison, step: str, pr_number: int, reason: str) -> str
def verdict_message(c: Comparison) -> str                        # the closing message, from c.verdict and c.report
def side_name(side: str) -> str                                  # tr("staged") / tr("skill"), for the lines above
def duration(minutes: int) -> str                                # "45 min", "2 h 05 min"
```

The English templates (each passed to `tr()` exactly as written; `{…}` are `tr` values):

| Use | Template |
|---|---|
| comparison, first line | `"{ticket} · comparison: {summary}"` |
| staged line | `"Staged: {side}"` |
| skill line | `"Skill ({skill}): {side}"` |
| a side with a PR | `"PR #{number} {url} · {state} · {time} · {tokens_in} in / {tokens_out} out tokens"` |
| a side without | `"no PR · {state}{outcome} · {time} · {tokens_in} in / {tokens_out} out tokens"` (outcome as `": <outcome>"` or `""`) |
| comparison, last line | `"Tap the better side. A reply to this message is kept as the verdict's note."` |
| buttons | `"Staged"`, `"Skill"`, `"Neither"`, `"Retry"` |
| duration | `"{minutes} min"`, `"{hours} h {minutes:02d} min"` |
| moved on | `"{ticket} · comparison: the {side} side moved on after your verdict; nothing was done. A new message comes when both sides are at rest."` |
| stuck | `"{ticket} · verdict {verdict} not finished: {step} failed: {reason}\nRetry continues from that step."` |
| step names | `"marking PR #{number} ready"`, `"retitling PR #{number}"`, `"commenting on PR #{number}"`, `"closing PR #{number}"` |
| verdict, first line | `"{ticket} · verdict: {verdict}"` (verdict through `side_name`, or `tr("neither")`) |
| verdict lines | `"Ready for review: PR #{number} {url}"`, `"Closed: PR #{number}"`, `"Both PRs stay drafts."`, `"Tracker: the \"PR opened\" comment and the move to review are made."`, `"Tracker: {problem}"`, `"Staged workspace: {state}"`, `"Skill run: dropped."`, `"Skill run: kept; its worktree is removed."`, `"Staged workspace: not found."` |

`c.report` holds the verdict lines as `[template, values]` pairs so the closing message is translated when it is built; `verdict_message` renders them in order.

- [ ] **Step 1: Write the failing tests**

```python
def test_comparison_message_shows_both_sides(comparison):
    staged = SideView("PR_REVIEW", True, 12, "u12", "", 130, 1_200_000, 40_000, "f")
    skill = SideView("stopped", True, 0, "", "stopped: low confidence", 65, 800_000, 30_000, "g")
    text = comparison_message(comparison, skill, staged)
    assert text.splitlines() == [
        "ACME-1 · comparison: Fix login",
        "Staged: PR #12 u12 · PR_REVIEW · 2 h 10 min · 1200000 in / 40000 out tokens",
        "Skill (fix-this): no PR · stopped: stopped: low confidence · 1 h 05 min · 800000 in / 30000 out tokens",
        "Tap the better side. A reply to this message is kept as the verdict's note.",
    ]

def test_buttons_offer_only_sides_with_a_pr(comparison):
    staged = SideView("PR_REVIEW", True, 12, "u12", "", 1, 0, 0, "f")
    skill = SideView("no_change", True, 0, "", "no app fix", 1, 0, 0, "g")
    assert verdict_buttons("cmp:abc", skill, staged) == [["Staged", "sk_win_staged:cmp:abc"],
                                                         ["Neither", "sk_win_none:cmp:abc"]]

def test_texts_follow_the_language(comparison, catalogue_uk):
    with telegram_language("uk"):
        assert comparison_message(comparison, GONE, GONE).splitlines()[-1] != \
               "Tap the better side. A reply to this message is kept as the verdict's note."

def test_callback_data_fits_telegram():
    assert all(len(action.encode()) <= 64 for _, action in verdict_buttons("cmp:" + "f" * 12, WITH_PR, WITH_PR))
```

- [ ] **Step 2: Run** `runtests t7 tests/unit/test_skillflow_compare_messages.py tests/unit/test_tg_i18n.py` — expected: FAIL.
- [ ] **Step 3: Implement** the builders and the Ukrainian catalogue module (same shape as `orchestrator/tg_uk/reviewer.py`; the Ukrainian keeps every placeholder, quoted text, emoji and number, as `test_tg_i18n.py` checks).
- [ ] **Step 4: Run** both files — expected: PASS. Then the whole suite.

---

### Task 8: The keeper and the verdict

The one writer of comparisons besides the reply handler, and the only code that writes to GitHub or the tracker for a verdict. **Review on the strongest model.**

**Files:**
- Create: `orchestrator/skillflow/verdict.py`, `orchestrator/skillflow/keeper.py`
- Test: `tests/unit/test_skillflow_keeper.py`, `tests/unit/test_skillflow_verdict.py` (a `FakeForge` with `pr_info` / `mark_ready` / `set_title` / `comment_on_pr` / `close_pr` that records calls and can fail a named call; a `FakeTracker`; a real `ComparisonStore` and `SkillStore` on `tmp_path`; a `FakeChat` as in `tests/unit/test_skillflow_runner.py`)

**Interfaces:**
- Consumes: Tasks 2–7; `SkillRepo` (`runner.py`), `SkillChat.send`, `SendError`, `tracker_writes_for_pr`, `telegram_language`.
- Produces:

```python
# verdict.py
class StepFailed(Exception):
    def __init__(self, step: str, pr_number: int, reason: str) -> None

class Verdict:
    """The steps of one comparison's verdict, from its steps_done on; each step recorded once it returns."""
    def __init__(self, comparisons: ComparisonStore, runs: SkillStore, workspaces: Workspaces, repo: SkillRepo,
                 tracker, now: Callable[[], datetime])
    async def run(self, c: Comparison) -> Comparison     # the comparison after its last recorded step; raises StepFailed

# keeper.py
class ComparisonKeeper:
    def __init__(self, repos: list[SkillRepo], comparisons: ComparisonStore, runs: SkillStore, chat: SkillChat,
                 workspaces: Workspaces, *, tracker_for: Callable[[str], Any], language_of: Callable[[str], str],
                 event_bus=None, now: Callable[[], datetime] = utcnow, idle_seconds: float = 30.0)
    def wake(self) -> None
    async def loop(self, stop: asyncio.Event) -> None    # cycle() until stop; never raises (cancellation aside)
    async def cycle(self) -> None                        # flush_outbox, then every open comparison, then every deciding one
    async def watch(self, c: Comparison) -> None
    async def decide(self, c: Comparison) -> None
    async def flush_outbox(self) -> None
```

**`watch(c)`** (an `open` comparison of a served scope; comparisons of other scopes are left as they are):
1. Read both sides: `skill_view(runs.run(c.run_id), …)`, `staged_view(workspaces.find(c), …)` with `c.rest_since`.
2. Update `rest_since` (a side at rest without a time gets `now`; a side not at rest loses its time) with one claim `("open",) → "open"` when it changed.
3. Not both at rest: when `c.fingerprint` is set (a message stands), the comparison is reopened by a claim with `fingerprint=""`, `ask_message_ids=[]` — the old message's buttons are refused from then on. Nothing is sent.
4. Both at rest and `pair_fingerprint(…) != c.fingerprint`: one claim `("open",) → "open"` with `expect={"messages": c.messages}`, `fingerprint=…`, `messages=c.messages + 1`, `ask_message_ids=[]`, `sides={"skill": …, "staged": …}` and `message={"text": comparison_message(…), "buttons": verdict_buttons(…)}`. Texts are built under `telegram_language(language_of(scope))`.

**`decide(c)`** (a `deciding` comparison of a served scope; others wait until served):
1. With `steps_done == []`, both sides are read again. When the pair's fingerprint is not `c.fingerprint`, a claim `("deciding",) → "open"` undoes the verdict (`verdict=""`, `decided_at=""`, `fingerprint=""`, `ask_message_ids=[]`) with `moved_on_message` for the side that moved; nothing else happens.
2. `Verdict.run(c)`. On `StepFailed`: claim `("deciding",) → "stuck"`, `reason=<redacted, cut to 300>`, `messages=c.messages + 1`, `ask_message_ids=[]`, `message={"text": stuck_message(…), "buttons": retry_buttons(c.id)}`. Any other exception is a `StepFailed` of the step in hand with the class name as reason (logged with `_where`), so a bug parks the verdict and never loops it.
3. When it returns: claim `("deciding",) → "closed"`, `closed_at=now`, `message={"text": verdict_message(c), "buttons": []}`; an event `comparison_verdict` with `{verdict, scope}`.

**`Verdict.run`** — winner = `c.verdict` (`"neither"`: no winner); `c.sides[…]["pr_number"]` give the PRs (the fingerprint check guarantees they are current). Each step below that is not in `c.steps_done` runs, then is recorded by a claim `("deciding",) → "deciding"` with `steps_done=[…, step]` and its report lines appended:
- `ready`: `forge.mark_ready(winner_pr)` (skipped for neither).
- `title`: `info = pr_info(winner_pr)`; the mark (` [skill]` or ` [staged]`) is cut from the end of `info.title` when it is there; `set_title` only when the title changes (skipped for neither). Report: `Ready for review: PR #n url`.
- `comment`: for a loser PR (not for neither): when `pr_info(loser_pr).state == "open"`, `comment_on_pr(loser_pr, f"Closed by Cleave: the comparison of the two flows chose the {winner} flow's PR #{winner_pr}.")`.
- `close`: for the same PR, when still open: `close_pr(loser_pr)`. Report: `Closed: PR #n`. For neither, the report is `Both PRs stay drafts.`
- `tracker` (not for neither): `tracker_writes_for_pr(tracker, repo.cfg, ticket_id, winner_url)`; a failed write is a report line `Tracker: <problem>`, never a stuck verdict and never retried (the tracker writes are made once). Without a tracker: `Tracker: <none configured>`.
- `skill`: the winner run is claimed `("pr_open",) → "pr_open"` with `released=True`; a losing or neither run is claimed from any state but `running`, `gates` and `dropped` to `dropped` with `reason=f"comparison verdict: {verdict}"`, or from `dropped` to `dropped` unchanged. A run that is `running` or in its `gates` (taken up again just before the verdict) makes the step wait: it is not recorded, and the next `decide` tries it again; this wait is not a stuck verdict. Report `Skill run: dropped.` / `Skill run: kept; its worktree is removed.`
- `staged`: `end_workspace(workspaces.find(c))`, report `Staged workspace: <state>`; none found → `Staged workspace: not found.`; an `InvalidTransitionError` is a report line with its class, recorded done.

Forge failures (`RuntimeError`, `httpx.HTTPError`) of `ready`, `title`, `comment`, `close` raise `StepFailed(step, pr, reason)`.

**Outbox:** as the runner's `flush_outbox` / `_send_outbox`: `chat.send(c, text, [Button(label, action) …] or None, skip=len(ids))` (`SkillChat.send` takes any object with `id` and `chat_id`: the comparison's id is its anchor), `drop_sent` with `ask_message_ids` for an entry with buttons, `mark_parts` on a partial `SendError`.

- [ ] **Step 1: Write the failing tests** (`rig` = stores, fakes, a keeper on one served scope `acme/app`, a staged workspace in `PR_REVIEW` with PR #12, a skill run `pr_open` with PR #13, the comparison open)

```python
async def test_message_once_both_rest(rig):
    rig.staged.state = "ANALYSIS"
    await rig.keeper.cycle()
    assert rig.chat.sent == []
    rig.staged.state = "PR_REVIEW"
    await rig.keeper.cycle(); await rig.keeper.cycle()
    (text, buttons), = rig.chat.sent
    assert "ACME-1 · comparison: Fix login" in text
    assert [b.action for b in buttons] == ["sk_win_staged:" + rig.cid, "sk_win_skill:" + rig.cid, "sk_win_none:" + rig.cid]
    assert rig.comparisons.get(rig.cid).ask_message_ids == rig.chat.ids[-1:]

async def test_a_side_that_moves_on_reopens(rig):
    await rig.keeper.cycle()
    rig.runs.claim(rig.run_id, ("pr_open",), NOW, "running")        # as if it moved on
    await rig.keeper.cycle()
    c = rig.comparisons.get(rig.cid)
    assert c.fingerprint == "" and c.ask_message_ids == []
    rig.runs.claim(rig.run_id, ("running",), NOW, "pr_open", turn_count=9)
    await rig.keeper.cycle()
    assert len(rig.chat.sent) == 2 and rig.comparisons.get(rig.cid).messages == 2

async def test_skill_verdict_writes(rig):
    await rig.decided("skill")
    assert rig.forge.calls == [("mark_ready", 13), ("pr_info", 13), ("set_title", 13, "ACME-1: Fix login"),
                               ("pr_info", 12), ("comment_on_pr", 12, "Closed by Cleave: the comparison of the two flows chose the skill flow's PR #13."),
                               ("pr_info", 12), ("close_pr", 12)]
    assert rig.tracker.calls == [("transition", "ACME-1", "In Review"), ("comment", "ACME-1", "PR opened: u13")]
    assert rig.runs.run(rig.run_id).released is True and rig.runs.run(rig.run_id).state == "pr_open"
    assert rig.staged.state.current_state == "DONE"
    c = rig.comparisons.get(rig.cid)
    assert c.state == "closed" and c.steps_done == list(STEPS)
    assert "Ready for review: PR #13 u13" in rig.chat.sent[-1][0]

async def test_neither_closes_nothing(rig):
    await rig.decided("neither")
    assert rig.forge.calls == [] and rig.tracker.calls == []
    assert rig.runs.run(rig.run_id).state == "dropped"
    assert "Both PRs stay drafts." in rig.chat.sent[-1][0]

async def test_staged_winner_drops_the_skill_run_and_ends_the_workspace(rig):
    await rig.decided("staged")
    assert ("mark_ready", 12) in rig.forge.calls and ("close_pr", 13) in rig.forge.calls
    assert rig.runs.run(rig.run_id).state == "dropped" and rig.staged.state.current_state == "DONE"

async def test_a_failed_step_is_stuck_and_retry_continues_from_it(rig):
    rig.forge.fail("comment_on_pr", RuntimeError("GitHub POST … → 502: bad gateway"))
    await rig.decided("skill")
    c = rig.comparisons.get(rig.cid)
    assert c.state == "stuck" and c.steps_done == ["ready", "title"]
    assert "commenting on PR #12 failed" in rig.chat.sent[-1][0] and rig.chat.sent[-1][1][0].action == "sk_retry:" + rig.cid
    rig.forge.heal(); rig.forge.calls.clear()
    rig.comparisons.claim(rig.cid, ("stuck",), NOW, "deciding")       # what Retry does
    await rig.keeper.cycle()
    assert rig.forge.calls[0] == ("pr_info", 12) and ("mark_ready", 13) not in rig.forge.calls
    assert rig.comparisons.get(rig.cid).state == "closed"

async def test_a_closed_loser_gets_no_second_comment(rig):
    rig.forge.prs[12] = "closed"
    await rig.decided("skill")
    assert not any(call[0] == "comment_on_pr" for call in rig.forge.calls)

async def test_moved_on_before_the_first_step_undoes_the_verdict(rig):
    await rig.message_out()
    rig.comparisons.claim(rig.cid, ("open",), NOW, "deciding", verdict="skill")
    rig.runs.claim(rig.run_id, ("pr_open",), NOW, "pr_open", turn_count=99)
    await rig.keeper.cycle()
    c = rig.comparisons.get(rig.cid)
    assert c.state == "open" and c.verdict == "" and rig.forge.calls == []
    assert "moved on after your verdict" in rig.chat.sent[-1][0]

async def test_a_running_loser_makes_the_skill_step_wait(rig):
    await rig.message_out()
    done = ["ready", "title", "comment", "close", "tracker"]
    rig.comparisons.claim(rig.cid, ("open",), NOW, "deciding", verdict="staged", steps_done=done)
    rig.runs.claim(rig.run_id, ("pr_open",), NOW, "running")    # taken up just before the verdict
    await rig.keeper.cycle()
    c = rig.comparisons.get(rig.cid)
    assert (c.state, c.steps_done) == ("deciding", done) and "not finished" not in rig.chat.sent[-1][0]
    rig.runs.claim(rig.run_id, ("running",), NOW, "stopped")
    await rig.keeper.cycle()
    assert rig.comparisons.get(rig.cid).state == "closed" and rig.runs.run(rig.run_id).state == "dropped"

async def test_a_restart_continues_a_deciding_comparison(rig):
    rig.comparisons.claim(rig.cid, ("open",), NOW, "deciding", verdict="skill", steps_done=["ready", "title"],
                          fingerprint=rig.fingerprint(), sides=rig.sides())
    keeper = rig.new_keeper()
    await keeper.cycle()
    assert ("mark_ready", 13) not in rig.forge.calls and rig.comparisons.get(rig.cid).state == "closed"

async def test_a_missing_workspace_is_a_report_line(rig):
    rig.delete_workspace()
    await rig.decided("skill")
    assert "Staged workspace: not found." in rig.chat.sent[-1][0]

async def test_telegram_down_keeps_the_message_for_later(rig):
    rig.chat.down = True
    await rig.keeper.cycle()
    assert rig.comparisons.get(rig.cid).unsent == 1
    rig.chat.down = False
    await rig.keeper.cycle()
    assert rig.comparisons.get(rig.cid).unsent == 0 and len(rig.chat.sent) == 1

async def test_an_unserved_scope_is_left_alone(rig):
    other = rig.comparisons.add("acme/web", "ACME-2", "run9", NOW)
    await rig.keeper.cycle()
    assert rig.comparisons.get(other.id).fingerprint == ""
```

(`rig.decided(v)` gets the message out, claims `open → deciding` with `verdict=v` as the tap does, and runs `cycle()`.)

- [ ] **Step 2: Run** `runtests t8 tests/unit/test_skillflow_keeper.py tests/unit/test_skillflow_verdict.py` — expected: FAIL.
- [ ] **Step 3: Implement.** The loop is the runner's shape: clear the wake event, `cycle()` inside a `try` that logs `_where(e)`, then wait for `wake()`, `idle_seconds` or `stop`. Each comparison is handled in its own `try`: one that raises holds back only itself, with one warning naming its id.
- [ ] **Step 4: Run** both files — expected: PASS. Then the whole suite.

---

### Task 9: Replies and taps on comparisons; the dormant skill flow

Telegram input that leads to forge and tracker writes. **Review on the strongest model.**

**Files:**
- Modify: `orchestrator/skillflow/replies.py`
- Test: `tests/unit/test_skillflow_replies_compare.py`; `tests/unit/test_skillflow_replies.py` passes unchanged

**Interfaces:**
- Consumes: `ComparisonStore` (Task 3), the texts (Task 7), `ComparisonKeeper.wake` (Task 8).
- Produces:

```python
class SkillReplies:
    def __init__(self, store, chat, wake, now=utcnow, *, comparisons: ComparisonStore | None = None,
                 wake_keeper: Callable[[], None] = lambda: None,
                 language_of: Callable[[str, str], str] = lambda scope, chat_id: ENGLISH)

class SkillOff:
    """The skill flow on a daemon that serves no repo: a reply to an old skill-run or comparison message, or a tap on
    one of its buttons, is answered "The skill flow is off on this daemon; nothing was done." and never reaches the
    intent parser."""
    def __init__(self, store: SkillStore, chat: SkillChat)
    def is_anchor(self, chat_id: str, message_id: int) -> bool
    async def handle_reply(self, chat_id: str, message_id: int, text: str, sent_at: datetime | None = None) -> None
    async def handle_callback(self, action: str, target: str, chat_id: str, message_id: int) -> None
    async def answer(self, chat_id: str, reply_to: int, text: str) -> None
```

Rules (each answer through `SkillChat.answer`, anchored to the comparison or run; texts through `tr()`, in `language_of(scope, chat_id)`; a store error is answered as today's internal error):
- **A reply to a comparison message** (anchor id starting `cmp:`): `add_note(cid, text, now)`, answer `"Kept as the comparison's note."` — any state, any of its messages, no age rule (a note changes nothing), an OK word included.
- **`sk_win_staged` / `sk_win_skill` / `sk_win_none`** on `cmp:<id>`, refused in this order with nothing written: comparison unknown (`"This comparison is not known; nothing was done."`); state not `open` (`"The comparison has its verdict ({verdict}); nothing was done."`, or for `deciding` / `stuck` `"The verdict is being carried out; nothing was done."`); an outbox entry with buttons waiting (`"The comparison's newest message has not gone out yet; nothing was done. Wait for it."`); the message not in `ask_message_ids` (`"That is not the comparison's newest message; nothing was done. Use the newest one."`); the chosen side without a PR in `c.sides` (`"That side has no PR; nothing was done."`). Then `claim(cid, ("open",), now, "deciding", expect={"messages": c.messages, "fingerprint": c.fingerprint}, verdict=…, decided_at=now, steps_done=[], reason="")`; a lost claim is answered from the row as it is now; a won one calls `wake_keeper()` and is answered `"Verdict: {verdict}. Cleave marks PR #{number} ready and makes the tracker writes for it{closing}."` (`closing` = `tr(", and closes PR #{number}", …)` when the other side has a PR) or `"Verdict: neither. Both PRs stay drafts; both sides end."`.
- **`sk_retry`** on `cmp:<id>`: only on a `stuck` comparison and its newest message; `claim(("stuck",) → "deciding", expect={"messages": c.messages})`, `wake_keeper()`, answer `"Retrying the verdict from the step that failed."`.
- **A run whose comparison is not `open`**: a text reply, Approve or Drop on any of the run's messages is refused with `"This run's comparison has its verdict; nothing was done."` before any other check of `_act`.
- A tap carries no date: no age rule for taps (as plan 1).

- [ ] **Step 1: Write the failing tests** (`rig`: real stores on `tmp_path`, a `FakeChat` recording answers, a comparison with its newest message ids `[50]`, sides with PRs 12 and 13, `messages=1`)

```python
async def test_verdict_tap_claims_and_wakes(rig):
    await rig.replies.handle_callback("sk_win_skill", rig.cid, "-100", 50)
    c = rig.comparisons.get(rig.cid)
    assert (c.state, c.verdict) == ("deciding", "skill") and rig.woken == 1
    assert rig.chat.answers[-1] == "ACME-1 · Verdict: skill. Cleave marks PR #13 ready and makes the tracker writes for it, and closes PR #12."

async def test_an_older_message_is_refused(rig):
    await rig.replies.handle_callback("sk_win_skill", rig.cid, "-100", 49)
    assert rig.comparisons.get(rig.cid).state == "open" and "not the comparison's newest" in rig.chat.answers[-1]

async def test_a_side_without_a_pr_is_refused(rig):
    rig.set_sides(skill_pr=0)
    await rig.replies.handle_callback("sk_win_skill", rig.cid, "-100", 50)
    assert rig.comparisons.get(rig.cid).state == "open" and rig.chat.answers[-1].endswith("That side has no PR; nothing was done.")

async def test_a_tap_racing_a_new_message_loses(rig):
    rig.comparisons.claim(rig.cid, ("open",), NOW, "open", messages=2)   # the keeper's new message, after the tap's read
    rig.replies_reads_stale(rig.cid)                                      # the handler read messages=1
    await rig.replies.handle_callback("sk_win_staged", rig.cid, "-100", 50)
    assert rig.comparisons.get(rig.cid).state == "open"

async def test_second_tap_is_answered_not_done_twice(rig):
    await rig.replies.handle_callback("sk_win_none", rig.cid, "-100", 50)
    await rig.replies.handle_callback("sk_win_none", rig.cid, "-100", 50)
    assert rig.woken == 1 and "being carried out" in rig.chat.answers[-1]

async def test_reply_is_a_note(rig):
    await rig.replies.handle_reply("-100", 50, "ok", sent_at=NOW - timedelta(hours=3))
    assert rig.comparisons.get(rig.cid).note == "ok" and rig.comparisons.get(rig.cid).state == "open"
    assert rig.chat.answers[-1].endswith("Kept as the comparison's note.")

async def test_retry_only_when_stuck(rig):
    await rig.replies.handle_callback("sk_retry", rig.cid, "-100", 50)
    assert rig.comparisons.get(rig.cid).state == "open"
    rig.stuck(message_ids=[60])
    await rig.replies.handle_callback("sk_retry", rig.cid, "-100", 60)
    assert rig.comparisons.get(rig.cid).state == "deciding" and rig.woken == 1

async def test_a_decided_runs_messages_are_refused(rig):
    rig.comparisons.claim(rig.cid, ("open",), NOW, "deciding", verdict="staged")
    await rig.replies.handle_reply("-100", rig.run_message, "try again", sent_at=NOW)
    assert rig.runs.run(rig.run_id).state == "stopped"
    assert rig.chat.answers[-1].endswith("This run's comparison has its verdict; nothing was done.")

async def test_skill_off_answers_and_keeps_the_parser_out(off_rig):
    assert off_rig.off.is_anchor("-100", 50) is True
    await off_rig.off.handle_reply("-100", 50, "ok")
    await off_rig.off.handle_callback("sk_win_skill", "cmp:abc", "-100", 50)
    assert off_rig.chat.answers == ["The skill flow is off on this daemon; nothing was done."] * 2
```

- [ ] **Step 2: Run** `runtests t9 tests/unit/test_skillflow_replies_compare.py` — expected: FAIL.
- [ ] **Step 3: Implement.** `handle_reply` branches on the anchor id before reading a run; `handle_callback` maps the four new actions; `_act` gains the decided-run check. Every input stays one compare-and-set.
- [ ] **Step 4: Run** the file and `tests/unit/test_skillflow_replies.py` — expected: PASS. Then the whole suite.

---

### Task 10: The runner removes a released run's worktree

**Files:**
- Modify: `orchestrator/skillflow/runner.py`
- Test: `tests/unit/test_skillflow_publish.py` (new tests)

**Interfaces:**
- Produces: `sweep_dropped` handles `in_states(("dropped", "pr_open"))` whose run is `dropped` or `released`, and not `cleaned`; `_clean`'s claim is from the run's own state to the same state (`(run.state,) → run.state`). The rule for the worktree stays: it is removed only when no other run of the ticket that has not ended works there; never forced; a refusal is a `worktree_left_line`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_released_winner_loses_its_worktree(rig):
    run = rig.run_in("pr_open", released=True)
    await rig.runner.sweep_dropped()
    assert ("remove_worktree", rig.worktree) in rig.checkout.calls and rig.store.run(run.id).cleaned is True
    assert rig.store.run(run.id).state == "pr_open"

async def test_an_unreleased_open_pr_keeps_its_worktree(rig):
    rig.run_in("pr_open")
    await rig.runner.sweep_dropped()
    assert not any(c[0] == "remove_worktree" for c in rig.checkout.calls if isinstance(c, tuple))
```

- [ ] **Step 2: Run** `runtests t10 tests/unit/test_skillflow_publish.py` — expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the file and `tests/unit/test_skillflow_runner.py` — expected: PASS. Then the whole suite.

---

### Task 11: The skill flow's own Telegram lines through `tr()`

Plan 1's messages and answers predate the per-project language; Cleave's own lines in them now follow it. The skill's relayed text, the status line and every run-time reason stay as they are.

**Files:**
- Modify: `orchestrator/skillflow/messages.py`, `orchestrator/skillflow/replies.py` (plan 1's answers), `orchestrator/skillflow/runner.py` (`language_of`), `integrations/telegram/command_handler.py` (`_SKILL_STORE_UNREAD`, `_SKILL_OFF` through `N_()` and `tr()`), `orchestrator/tg_uk/skillflow.py`
- Test: `tests/unit/test_skillflow_messages.py`, `tests/unit/test_skillflow_replies.py`, `tests/unit/test_skillflow_runner.py` (they pass in English unchanged), `tests/unit/test_tg_i18n.py`, new tests in `tests/unit/test_skillflow_messages.py` and `tests/unit/test_skillflow_runner.py`

**Interfaces:**
- Produces: `SkillRunner.__init__(…, language_of: Callable[[str], str] = lambda scope: ENGLISH)`; `run_job`'s body, `recover`'s per-run body and `_clean` run under `telegram_language(language_of(run.scope))`, so every message the runner builds is in the project's language. Every fixed English line of `messages.py` (the header's `skill run`, `stopped`, `needs you`, `no change`, `No worktree at`, `draft PR opened`, `Gates:`, `none configured`, `deferred until`, `interrupted by a restart at the {phase} step`, `failed`, `worktree left at`, `Notes:`, each closing line, the cut line, the Approve / Drop labels) and every answer of `replies.py` goes through `tr()` with a `{…}` template — no f-string inside `tr()`. `OK_WORDS` is unchanged.

- [ ] **Step 1: Write the failing tests**

```python
def test_waiting_message_in_ukrainian_keeps_the_skills_text(run, tmp_path, catalogue_uk):
    with telegram_language("uk"):
        text = waiting_message(run, "Which screen? Яке поле?", tmp_path)
    assert "Which screen? Яке поле?" in text and "Reply to answer" not in text

def test_cleave_stop_keeps_its_reason(run, catalogue_uk):
    run.reason = "push failed: [rejected] fetch first"
    with telegram_language("uk"):
        assert "push failed: [rejected] fetch first" in cleave_stop_message(run)

def test_buttons_are_translated(catalogue_uk):
    with telegram_language("uk"):
        assert [b.label for b in parked_buttons("r1")] != ["Approve", "Drop"]

# tests/unit/test_skillflow_runner.py
async def test_runner_messages_follow_the_language(rig):
    rig.runner = rig.make_runner(language_of=lambda scope: "uk")
    rig.queue_run(turn_text="Which screen?")
    await rig.runner.run_job(rig.next())
    assert "Which screen?" in rig.chat.sent[-1][0] and "Reply to answer, or tap Approve to say ok." not in rig.chat.sent[-1][0]
```

- [ ] **Step 2: Run** `runtests t11 tests/unit/test_skillflow_messages.py tests/unit/test_tg_i18n.py` — expected: FAIL.
- [ ] **Step 3: Implement** the templates and their Ukrainian in `orchestrator/tg_uk/skillflow.py`.
- [ ] **Step 4: Run** the skill-flow message, reply and runner files and `test_tg_i18n.py` — expected: PASS, the English assertions of plan 1 unchanged. Then the whole suite.

---

### Task 12: The dashboard's Comparison section

**Files:**
- Create: `orchestrator/skillflow/board.py`, `dashboard/comparisons_api.py`, `dashboard/static/js/comparison.js`
- Modify: `dashboard/web.py` (`create_app(…, comparisons=None)`), `dashboard/static/index.html`, `dashboard/static/js/app.js`, `dashboard/static/js/api.js`, `dashboard/static/style.css`
- Test: `tests/unit/test_skillflow_board.py`, `tests/unit/test_dashboard_comparisons.py` (as `tests/unit/test_dashboard_reviews.py`)

**Interfaces:**
- Consumes: `ComparisonStore.newest_first`, `SkillStore.run`, `Workspaces.find`, `skill_view`, `staged_view` (Tasks 3, 6).
- Produces:

```python
# board.py
class ComparisonBoard:
    def __init__(self, comparisons: ComparisonStore, runs: SkillStore, workspaces: Workspaces,
                 now: Callable[[], datetime] = utcnow)
    def overview(self) -> dict
    # {"rows": [{"id", "scope", "ticket_id", "summary", "skill", "state", "verdict", "note", "created_at",
    #            "decided_at", "reason", "skill_side": SideView.as_dict(), "staged_side": SideView.as_dict()}],
    #  "tally": {"skill": n, "staged": n, "neither": n, "undecided": n}}
    # sides read live for an open comparison; from c.sides once it has a verdict. Tally: verdict of every comparison
    # that has one (deciding, stuck, closed); undecided = open.

# comparisons_api.py
def build_comparison_routes(board) -> list[Route]      # GET /api/comparisons -> board.overview() (async endpoint)
```

Frontend: a nav entry `Comparison` (`id="nav-comparison"`), a view `comparison` in `app.js` (hash `#/comparison`, the router regex, the 30 s auto-refresh, the Refresh button), `loadComparisons()` in `api.js` (`{rows: [], tally: {…0}}` on 404, as `loadReviews`), and `renderComparison()` in `comparison.js`: the tally on top (Skill, Staged, Neither, Undecided), then one table row per comparison: ticket and summary, staged side (state, PR link, time, tokens), skill side (the same), the verdict (or the state while it is open, deciding or stuck, with `reason` for stuck), the note. Read-only: no verdict buttons on the dashboard. Every text inserted with `textContent` (never `innerHTML` with data). English.

- [ ] **Step 1: Write the failing tests**

```python
def test_overview_tally_and_rows(board_rig):
    board_rig.comparison("ACME-1", state="closed", verdict="skill")
    board_rig.comparison("ACME-2", state="stuck", verdict="staged", reason="502")
    board_rig.comparison("ACME-3", state="open")
    data = board_rig.board.overview()
    assert data["tally"] == {"skill": 1, "staged": 1, "neither": 0, "undecided": 1}
    assert [r["ticket_id"] for r in data["rows"]] == ["ACME-3", "ACME-2", "ACME-1"]
    assert data["rows"][1]["reason"] == "502"

def test_route(client):
    response = client.get("/api/comparisons")
    assert response.status_code == 200 and response.json()["tally"]["undecided"] == 0

def test_no_route_without_a_board(store):
    assert TestClient(create_app(EventBus(), store)).get("/api/comparisons").status_code == 404
```

- [ ] **Step 2: Run** `runtests t12 tests/unit/test_skillflow_board.py tests/unit/test_dashboard_comparisons.py` — expected: FAIL.
- [ ] **Step 3: Implement** the board, the route (mounted when `comparisons` is given, as `reviewer` is), and the frontend.
- [ ] **Step 4: Run** both files and `tests/unit/test_dashboard_web.py` — expected: PASS. Then the whole suite. Open the dashboard once with a fake board (a throwaway script in the scratchpad that calls `create_app` with a `FakeBoard`) and check the section renders, at phone width too.

---

### Task 13: Wiring and operator docs

**Files:**
- Modify: `main.py`, `docs/skill-flow.md`, `deploy/README.md`
- Test: `tests/unit/test_main_skillflow.py` (new tests; never run `test_main.py` on its own)

**Interfaces:**
- Consumes: everything above.
- Produces in `main()`:
  - One connection `skill_conn = connect(reviewer_db_path(global_config.reviewer))` for `SkillStore` and `ComparisonStore`.
  - With skill repos: `ComparisonStore(skill_conn)`; `orchestrator.set_comparisons(comparisons)`; `SkillIntake(…, comparisons=comparisons)`; `Workspaces(orchestrator.get_active_workspaces)`; `ComparisonKeeper(skill_repos, comparisons, skill_store, skill_chat, workspaces, tracker_for=orchestrator.get_tracker_for_project, language_of=…, event_bus=event_bus)`; `SkillRunner(…, language_of=…)`; `SkillReplies(…, comparisons=comparisons, wake_keeper=keeper.wake, language_of=…)`; `create_app(…, comparisons=ComparisonBoard(comparisons, skill_store, workspaces))`.
  - `language_of(scope)` = `project_language(global_config, projects, scope.split("/", 1)[0])`; the replies' `language_of(scope, chat_id)` = that, or `chat_language(global_config, projects, chat_id)` when there is no scope.
  - Without skill repos but with Telegram: when `SkillStore(skill_conn).has_anchors()`, `skill=SkillOff(store, SkillChat(notifier, store))` goes to the command handler, so a reply to an old skill-run or comparison message never reaches the intent parser (plan 1's residual M6).
  - The keeper's loop starts with the runner (`_start_background("Comparisons", keeper.loop(stop))`, no first-round wait: it calls no CLI) and its entry joins `_stop_background`'s dictionary.
  - `set_comparisons` is called only with skill repos: on a daemon serving no `both` repo `_is_compared` is always false.

`docs/skill-flow.md`: replace "Not built yet — the comparison (plan 2)" with a section **The comparison**: what changes for the staged flow on a compared ticket (a draft PR titled `… [staged]`, no tracker writes at push time, PR review answers no comments and offers no "Review Complete"); "at rest" for each side; the comparison message, its buttons and when each is offered; a side that moves on; the note; what each verdict does, step by step, including that the staged flow's "ready for merge" notice is replaced by the verdict's closing message; a stuck verdict and Retry; the tracker writes made once and never retried; worktree removal at the verdict; the dashboard section; known limits (a ticket that gets no run gets no comparison; turning `both` off returns an open comparison's workspace to the staged flow's own PR review with its draft PR and no tracker writes — decide every open comparison first; a comparison of a repo not served waits until it is served again; a restart in a multi-part send repeats parts, as plan 1's). Also: the Telegram language of Cleave's lines; the dashboard's own "skill runs" query stays. `deploy/README.md`'s skill-flow host steps gain: the repo's GitHub plan accepts draft PRs (a private repo on the free plan does not), and the token can mark a PR ready (GraphQL), edit its title, comment and close it — checked by the first compared ticket on the host.

- [ ] **Step 1: Write the failing tests**

```python
def test_keeper_starts_and_stops_with_the_runner(main_rig):
    tasks = main_rig.start()
    assert {"Skill runs", "Comparisons"} <= set(tasks)

def test_comparisons_are_set_only_with_skill_repos(main_rig):
    assert getattr(main_rig.wired(both=[]).orchestrator, "_comparisons", None) is None
    assert main_rig.wired(both=["app"]).orchestrator._comparisons is not None

def test_skill_off_with_old_anchors(main_rig):
    main_rig.store.add_anchor("-100", 5, "run1", NOW)
    handler = main_rig.wired(both=[]).command_handler
    assert isinstance(handler._skill, SkillOff)

def test_no_skill_off_on_a_daemon_that_never_used_it(main_rig):
    assert main_rig.wired(both=[]).command_handler._skill is None
```

(Shape `main_rig` on the existing helpers of `tests/unit/test_main_skillflow.py`; when `main()` is too big to drive, extract the block into `_wire_skill(...)` and test that.)

- [ ] **Step 2: Run** `runtests t13 tests/unit/test_main_skillflow.py` — expected: FAIL.
- [ ] **Step 3: Implement** the wiring and the docs.
- [ ] **Step 4: Run** the file, then the whole suite — expected: only the nine known failures.

---

## Not in this plan

- The stall of the staged poll cycle while a skill turn holds the build slot (plan 1's final review, I3): unchanged; `docs/skill-flow.md` already names the two levers. The keeper runs in its own task and is not stalled by it.
- Answering PR comments in the reviewer subsystem; a skill-only mode; removing the staged agents (follow-ups of the spec).
- Verdict buttons on the dashboard (the verdict is a Telegram tap).
- Removing a dropped or released run's local branch; cleaning old run directories.
- What a host run must confirm (in `deploy/README.md` after Task 13): draft PRs on the repo's plan; the token's rights for mark-ready, retitle, comment and close; that the skill side's `tokens_in` (from the CLI's usage) and the staged side's (`input_tokens=` in the agent logs) count the same thing — if one includes cache reads and the other does not, the message's token figures are not comparable and a later change picks one measure.
