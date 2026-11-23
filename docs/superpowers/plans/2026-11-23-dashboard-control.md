# Dashboard Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every action the operator can send from Telegram can also be done from the dashboard, through one shared implementation per action, with Telegram kept informed and the dashboard closed to anything but the host and the operator's Tailscale network.

**Architecture:**
- A ticket action module (`orchestrator/ticket_actions.py`) and a reviewer action layer (`orchestrator/reviewer/actions.py`) hold the checks and the effects. They are moved out of `CommandHandler` and `ReviewerReplies`.
- Telegram's handlers keep their wording and call the layer. The dashboard's endpoints call the same layer, under English, and announce what they did in Telegram.
- One ASGI middleware guards the dashboard: a peer allow list, and a cross-site check on every write.

**Tech Stack:** Python 3.12, Starlette (dashboard), python-telegram-bot (adapter), pytest + respx/TestClient, plain ES modules in the browser, `node --test` for pure JS modules.

**Spec:** `docs/superpowers/specs/2026-11-23-dashboard-control-design.md` (approved 2026-11-23).

## Global Constraints

- Telegram's existing answers do not change, with one exception: a typed `reject` at plan review now sends the plan back to analysis.
  - Existing tests in `tests/unit/test_command_handler.py`, `test_reviewer_replies.py` and `test_handler_approval.py` pass unchanged, except the typed-reject test at plan review.
- No action's logic exists twice. The dashboard's endpoints call the layer and never repeat its checks.
- Default allow list: `["127.0.0.0/8", "::1/128", "100.64.0.0/10", "fd7a:115c:a1e0::/48"]`.
- Dashboard texts are English: the layer runs under `telegram_language(ENGLISH)` for a dashboard call. Telegram announcements are in the project's Telegram language.
- Every new text a Telegram message carries is passed through `tr()`/`N_()` and has a Ukrainian entry in `orchestrator/tg_uk/` (the i18n tests enforce both).
- Public repo: test data uses `acme`, `app`, `ACME-123`, `octo`. No real ticket keys, logins, Jira ids or local paths.
- Run Python tests with `timeout 600 ~/Cleave/.venv/bin/python -m pytest <paths> -q -p no:cacheprovider` (the suite hangs after its summary without a timeout). Nine failures are known and environmental: `test_e2e_dry_run` ×3, `test_build_jql_ands_multiple_trigger_labels`, `test_bad_payload_returns_400`, `test_not_a_git_dir`, `test_main` ×3.
- JS tests: `node --test 'tests/js/*.test.mjs'`.

## Review Focus

1. **The same push from both channels at once pushes once.** This holds because both channels share one `Posting` (its lock, and its "Already pushed as …" refusal). Task 4 pins that `ReviewerReplies(actions=…)` uses the given layer and builds no second `Posting`.
2. **An action from a dashboard panel loaded before the job was superseded is refused** with the outdated line and never runs against a newer job (Task 6).
3. **Telegram is down, or the announcement fails:** the dashboard action still completes and returns its result; the failure is logged (Tasks 3 and 6).
4. **The question is answered in the dashboard after it was already answered in Telegram:** refused ("… is ANALYSIS now: nothing was done."), and the input is not written a second time (Task 1).
5. **A dashboard Yes on an LLM-understood command more than 15 minutes old** is refused ("Expired …") and runs nothing (Task 6).

---

### Task 0: Access guard

**Files:**
- Create: `dashboard/web_guard.py`
- Modify: `config/schemas.py` (`DashboardConfig`), `dashboard/web.py` (`create_app`), `dashboard/reviews_api.py` (`_cross_site`), `main.py` (the `create_app(...)` call near line 1188), `config-live.example/global.yaml` (if it has a `dashboard:` block; else `deploy/README.md`'s dashboard section)
- Test: `tests/unit/test_dashboard_guard.py`

**Interfaces:**
- Produces:
  - `DashboardConfig.allowed_networks: list[str]` (default as in Global Constraints).
  - `create_app(..., allowed_networks: list[str] | None = None)`: None means any peer. Tests and callers without config keep working.
  - `web_guard.foreign_origin(request) -> bool`: True when the `Sec-Fetch-Site` header is present and not `same-origin`/`none`, or when an `Origin` header's netloc differs from `Host`.
  - `web_guard.GuardMiddleware(app, allowed_networks: list[str] | None)`.

- [ ] **Step 1: Write the failing tests**

```python
ALLOWED = ["127.0.0.0/8", "::1/128", "100.64.0.0/10", "fd7a:115c:a1e0::/48"]

@pytest.mark.parametrize("peer,code", [("192.168.31.20", 403), ("100.101.1.2", 200), ("fd7a:115c:a1e0::1", 200),
                                       ("127.0.0.1", 200)])
def test_only_the_host_and_tailscale_get_in(store, peer, code):
    c = TestClient(create_app(EventBus(), store, allowed_networks=ALLOWED), client=(peer, 5000))
    assert c.get("/api/health").status_code == code

def test_no_allow_list_admits_any_peer(store):
    assert TestClient(create_app(EventBus(), store)).get("/api/health").status_code == 200

@pytest.mark.parametrize("headers", [{"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}])
@pytest.mark.parametrize("method,path", [("post", "/api/workspaces/ACME-1/approve"), ("post", "/api/daemon/mode"),
                                         ("put", "/api/settings/model"), ("post", "/api/workspaces/ACME-1/delete")])
def test_a_cross_site_write_is_refused_everywhere(action_client, method, path, headers):
    assert getattr(action_client, method)(path, json={}, headers=headers).status_code == 403

def test_a_same_origin_write_passes_the_guard(action_client):
    r = action_client.post("/api/workspaces/ACME-1/approve", json={}, headers={"Origin": "http://testserver"})
    assert r.status_code != 403

def test_default_allow_list():
    assert DashboardConfig().allowed_networks == ALLOWED
```

`action_client` is `create_app` with the `orchestrator`/`mode_handler` fakes of `tests/unit/test_dashboard_actions.py` (import or copy its fixtures).

- [ ] **Step 2: Run them to verify they fail**
  - Run: `timeout 600 …/python -m pytest tests/unit/test_dashboard_guard.py -q -p no:cacheprovider`
  - Expected: FAIL (`create_app` has no `allowed_networks`; `web_guard` is missing).

- [ ] **Step 3: Implement the guard**
  - `GuardMiddleware` is plain ASGI on `http` scopes.
  - Peer check:
    - It applies only when the allow list is not None.
    - It reads `scope["client"][0]` with `ipaddress.ip_address`.
    - A peer that does not parse, or is in no network, gets `PlainTextResponse("Forbidden", 403)`, and a WARNING with the peer is logged.
  - Cross-site check: for POST/PUT/PATCH/DELETE, `foreign_origin` → 403.
  - `create_app` wraps the app as `Starlette(routes=routes, middleware=[Middleware(GuardMiddleware, allowed_networks=…)])`.
  - `reviews_api._cross_site` becomes "content type is not JSON, or `foreign_origin(request)`". Its own tests stay green.
  - `main.py` passes `allowed_networks=global_config.dashboard.allowed_networks`.
  - Document the key (one comment line) where the dashboard block is documented.

- [ ] **Step 4: Run the new tests and the dashboard tests**
  - Run: `timeout 600 …/python -m pytest tests/unit/test_dashboard_guard.py tests/unit/test_dashboard_*.py -q -p no:cacheprovider`
  - Expected: all pass.

- [ ] **Step 5: Commit**: `feat(dashboard): answer only the host and Tailscale; refuse a cross-site write on every route`

---

### Task 1: Ticket actions module

**Files:**
- Create: `orchestrator/ticket_actions.py`
- Modify: `integrations/telegram/command_handler.py`:
  - `_handle_approve`, `_handle_reject`, `_handle_retry`, `_retry_target`, `_restart`, `_skip`, `_handle_provide_input`, `_handle_reviewed`;
  - the `approve`/`reject`/`reviewed`/`retry*`/`skip` branches of `_ticket_callback`;
  - remove the moved `_VALID_RETRY_STATES`, `_SKIP_NEXT`, `_write_human_input`.
- Test: `tests/unit/test_ticket_actions.py`; update the typed-reject-at-plan-review case in `tests/unit/test_command_handler.py`.

**Interfaces:**
- Produces (all functions are sync, take a `Workspace`, and never send anything):

```python
@dataclass(frozen=True)
class TicketResult:
    state: str          # the ticket's state after the call
    refused: str = ""   # why nothing was done, in the current language; "" when it was done

RETRY_STAGES: dict[str, str]          # was CommandHandler._VALID_RETRY_STATES, same keys and order
SKIP_NEXT: dict[str, str]             # was _SKIP_NEXT

def approve(ws, *, wake=None) -> TicketResult
def reject(ws, *, via: str, wake=None) -> TicketResult     # via: "Telegram" or "dashboard", for the FAILED error text
def retry_target(ws, stage: str | None) -> tuple[str, str] # was _retry_target, unchanged
def retry(ws, *, stage: str | None = None, furthest: bool = False, wake=None) -> TicketResult
def skip(ws, *, wake=None) -> TicketResult
def answer(ws, text: str, *, wake=None) -> TicketResult
def reviewed(ws, *, wake=None) -> TicketResult
```

- How the functions behave:
  - `retry` targets:
    - with `stage` → `retry_target(ws, stage)`, then the move with `stage_named=True`;
    - with `furthest` → `retry_target(ws, None)`;
    - otherwise → `ws.state.previous_state or Stage.ANALYSIS`.
    - Every path moves through the former `_restart`.
  - `wake`, when given, is called after every successful move.
  - `reject`:
    - Its target is `APPROVAL_REJECT_STATE.get(previous_state, "FAILED")`.
    - On FAILED it sets `error="Rejected by operator via {via}"`.
  - Refusal texts reuse the existing templates:
    - "{ticket_id} is {state} now: nothing was done.";
    - "No workspace awaiting approval for {ticket_id}.";
    - "Empty input — nothing to resume with.";
    - "Unknown stage '{stage}'. Valid stages: {valid}";
    - "No workspace in PR_REVIEW for {ticket_id}.".
  - `answer` requires `current_state == BLOCKED`. It writes `meta/human-input.md` (the former `_write_human_input`), resets attempts and moves to `previous_state or ANALYSIS`.

- [ ] **Step 1: Write the failing tests**

```python
def test_typed_reject_at_plan_review_goes_back_to_analysis(ws_at_gate):     # ws: AWAITING_APPROVAL, previous PLAN_REVIEW
    assert ticket_actions.reject(ws_at_gate, via="Telegram") == TicketResult("ANALYSIS")

def test_reject_at_another_gate_fails_the_ticket(ws_at_qa_gate):
    assert ticket_actions.reject(ws_at_qa_gate, via="dashboard").state == "FAILED"
    assert ws_at_qa_gate.state.error == "Rejected by operator via dashboard"

def test_retry_modes(tmp_ws_with_qa_report):                                # FAILED, previous QA, reports/qa exists
    assert ticket_actions.retry(tmp_ws_with_qa_report).state == "QA"                       # where it stopped
    assert ticket_actions.retry(fresh(tmp_ws_with_qa_report), furthest=True).state == "PUSHED"
    r = ticket_actions.retry(fresh(tmp_ws_with_qa_report), stage="dev")
    assert r.state == "DEV" and fresh_attempts_from("DEV")

def test_retry_unknown_stage_is_refused(tmp_ws):
    assert ticket_actions.retry(tmp_ws, stage="nope").refused.startswith("Unknown stage 'nope'.")

def test_answer_twice_writes_once(blocked_ws):                               # Review Focus 4
    woke = []
    assert ticket_actions.answer(blocked_ws, "yes, allowed", wake=lambda: woke.append(1)).state == "ANALYSIS"
    second = ticket_actions.answer(blocked_ws, "yes, allowed")
    assert second.refused == "ACME-1 is ANALYSIS now: nothing was done."
    assert human_input(blocked_ws).count("yes, allowed") == 1 and woke == [1]

def test_reviewed_sets_the_signal_and_wakes(pr_review_ws):
    woke = []
    assert ticket_actions.reviewed(pr_review_ws, wake=lambda: woke.append(1)).refused == ""
    assert pr_review_ws.state.human_input_reply == "reviewed" and woke == [1]
```

Use a real `Workspace` on `tmp_path`, as `tests/unit/test_command_handler.py`'s `_make_workspace` does, so transitions are real.

- [ ] **Step 2: Run them to verify they fail**
  - Run: `timeout 600 …/python -m pytest tests/unit/test_ticket_actions.py -q -p no:cacheprovider`
  - Expected: FAIL (`orchestrator.ticket_actions` is missing).

- [ ] **Step 3: Implement the module and switch `CommandHandler` to it**
  - The Telegram reply texts and headers stay in `CommandHandler`. It composes them from `TicketResult` exactly as today.
  - The Retry button and the "retry" escalation reply call `retry(ws)`. The typed `retry X` calls `retry(ws, furthest=True)`, or `stage=` when one is named.
  - `retry_from:` calls `retry(ws, stage=…)`.
  - `_handle_provide_input` keeps its choice of the blocked workspace and `_recently_unblocked`. It calls `answer`.
  - Pass `wake=self._wake_fn` (None-safe).

- [ ] **Step 4: Update the one test that changes and run**
  - Update: the typed `reject` at plan review now answers "Rejected {ticket_id}." with the back-to-analysis wording `_ticket_callback` uses ("Rejected. Back to {state}.").
  - Run: `timeout 600 …/python -m pytest tests/unit/test_ticket_actions.py tests/unit/test_command_handler.py tests/unit/test_handler_approval.py tests/unit/test_tg_i18n.py -q -p no:cacheprovider`
  - Expected: all pass.

- [ ] **Step 5: Commit**: `refactor(tickets): one module for approve, reject, retry, skip, answer and reviewed`

---

### Task 2: PR comment decisions in the module

**Files:**
- Modify: `orchestrator/ticket_actions.py`, `integrations/telegram/command_handler.py`:
  - the `pr_fix`/`pr_wontfix` branch;
  - the classified-reply loop in `handle_reply` (around line 862);
  - `_stage_reinvestigation`.
- Test: `tests/unit/test_ticket_actions.py`

**Interfaces:**
- Consumes: `TicketResult` (Task 1).
- Produces:

```python
@dataclass(frozen=True)
class CommentDecision:
    refused: str = ""          # why nothing was recorded ("Comment not found.", the hint-limit text, …)
    comment: dict | None = None
    stored: str = ""           # "fix", "won't fix: <reason>", or "reinvestigate"
    remaining: int = 0         # comments of the ticket still undecided

def decide_comment(ws, comment_id: str, decision: str, text: str = "", *, via: str, matched_token: str = "",
                   events=None, wake=None) -> CommentDecision
```

- What `decide_comment` takes and does:
  - `decision`:
    - `"fix"`;
    - `"wont_fix"`, with `text` as the reason (empty → "operator decision");
    - `"reinvestigate"`, with `text` as the hint.
  - `via`: `"button"`, `"reply"` or `"dashboard"`. It goes into the `pr_comment_decision_recorded` event, as today.
  - The ticket must be PR_REVIEW and the comment must be in `pending_review_comments`.
  - The wake runs when `remaining == 0` after a fix or won't-fix. Re-investigation keeps `_stage_reinvestigation`'s cap (3 rounds) and its event `pr_comment_hint_exhausted`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_decision_from_each_channel_is_stored_the_same(pr_review_ws_two_comments):
    events, woke = FakeEvents(), []
    d = ticket_actions.decide_comment(pr_review_ws_two_comments, "11", "wont_fix", "", via="dashboard", events=events)
    assert d.stored == "won't fix: operator decision" and d.remaining == 1 and woke == []
    assert events.last == ("pr_comment_decision_recorded", {"comment_id": "11", "decision": d.stored, "via": "dashboard"})
    d = ticket_actions.decide_comment(pr_review_ws_two_comments, "12", "fix", via="button",
                                      events=events, wake=lambda: woke.append(1))
    assert d.stored == "fix" and d.remaining == 0 and woke == [1]

def test_a_fourth_hint_is_refused(pr_review_ws_hint_rounds_3):
    d = ticket_actions.decide_comment(pr_review_ws_hint_rounds_3, "11", "reinvestigate", "look at the null case", via="dashboard")
    assert d.refused.startswith("Hint limit reached (3/3)")

def test_an_unknown_comment_is_refused(pr_review_ws_two_comments):
    assert ticket_actions.decide_comment(pr_review_ws_two_comments, "99", "fix", via="dashboard").refused == "Comment not found."
```

`FakeEvents.last` gives the last `emit(kind, text, ticket_id=, data=)` with `data` reduced to those keys.

- [ ] **Step 2: Run them to verify they fail.** Expected: FAIL (`decide_comment` is missing).

- [ ] **Step 3: Implement `decide_comment` and make the three Telegram paths call it**
  - Telegram keeps:
    - its confirmation texts ("✓ Recognized as FIX (matched: …)");
    - the "Show {n} unanswered" button;
    - the hint-limit message it sends.
  - The data for all of these comes from `CommentDecision`.

- [ ] **Step 4: Run**
  - Run: `timeout 600 …/python -m pytest tests/unit/test_ticket_actions.py tests/unit/test_command_handler.py tests/unit/test_fetch_pr_comments_replies.py tests/unit/test_escalation_view.py -q -p no:cacheprovider`
  - Expected: all pass.

- [ ] **Step 5: Commit**: `refactor(tickets): one function records a PR comment decision, whichever channel sent it`

---

### Task 3: Dashboard ticket endpoints and the Telegram announcement

**Files:**
- Create: `orchestrator/ticket_announce.py`
- Modify:
  - `dashboard/actions.py`: `approve`, `reject`, `retry`; new `skip`, `answer`, `decide`, `reviewed` routes; `build_action_routes`.
  - `dashboard/web.py`: `_scan_all_workspaces` rows; `create_app(..., wake=None, ticket_announcer=None)`.
  - `main.py`: build the announcer when the notifier exists; pass `wake=lambda: orchestrator._wake_event.set()`.
  - `orchestrator/tg_uk/commands.py`: the new texts.
- Test: `tests/unit/test_dashboard_actions.py`, `tests/unit/test_ticket_announce.py`

**Interfaces:**
- Consumes: Tasks 1–2's functions.
- Produces:

```python
class TicketAnnouncer:
    def __init__(self, notifier, chat_id_of: Callable[[Any], str], language_of: Callable[[str], str]) -> None
    async def announce(self, ws, what: Callable[[], str], *, reply_to: int | None = None) -> None
        # sends "🖥 " + tr("Dashboard: {what}", what=what()) under language_of(ws.state.company_id);
        # with no reply_to, the line follows tg_format.tg_header("🖥", …) of the ticket; never raises (logs WARNING)
```

- Routes (JSON bodies; the response is `{"status": "ok", "new_state": …}` or `_error(refused)`: 400, as the existing handlers answer a refusal):
  - `POST /api/workspaces/{id}/retry` with `{"from": "<stage>"}` (optional): no stage means the ticket restarts where it stopped. The QA-gate parking and the "smart" targeting in this handler go.
  - `POST /api/workspaces/{id}/skip`.
  - `POST /api/workspaces/{id}/answer` with `{"text": "…"}`.
  - `POST /api/workspaces/{id}/comments/{comment_id}/decision` with `{"decision": "fix"|"wont_fix"|"reinvestigate", "text": "…"}`.
  - `POST /api/workspaces/{id}/reviewed`.
- Workspace rows gain `human_input_question`, `pending_review_comments` (each comment's `comment_id`, `author`, `file`, `line`, `body` cut to 300 characters, `assessment`, `decision`, `hint_rounds`) and `escalation_msg_id`.
- What gets announced: approve "approved → {state}"; reject "rejected → {state}"; retry "retry from {state}"; skip "skipped → {state}"; answer "answer: «{text}»", the text cut to 200 characters; a decision "Fix on {file_line}" / "Won't fix on {file_line}: {reason}" / "re-investigate {file_line}: {hint}"; reviewed "reviewed".
  - It replies to the comment's latest message id for a decision (`_ensure_msg_ids`), and to `escalation_msg_id` otherwise when set.
  - Refusals are not announced.

- [ ] **Step 1: Write the failing tests** in `test_dashboard_actions.py`:

```python
def test_dashboard_retry_restarts_where_it_stopped(client_with_real_ws):   # FAILED, previous QA, qa report exists
    assert client.post("/api/workspaces/ACME-1/retry", json={}).json()["new_state"] == "QA"

def test_dashboard_retry_from_a_stage(client_with_real_ws):
    assert client.post("/api/workspaces/ACME-1/retry", json={"from": "dev"}).json()["new_state"] == "DEV"

def test_answer_skip_decision_reviewed_routes(...):   # one assert per route on new_state / stored decision
    ...

def test_a_refusal_is_400_and_not_announced(client, announcer):
    r = client.post("/api/workspaces/ACME-1/answer", json={"text": "x"})   # ticket not BLOCKED
    assert r.status_code == 400 and announcer.sent == []

def test_the_action_completes_when_telegram_fails(client, failing_announcer):  # Review Focus 3
    assert client.post("/api/workspaces/ACME-1/approve", json={}).json()["status"] == "ok"
```

In `test_ticket_announce.py`:

```python
async def test_announcement_is_in_the_project_language_and_replies_to_the_escalation(uk_project_ws):
    notifier = FakeNotifier()
    await TicketAnnouncer(notifier, lambda ws: "42", lambda p: "uk").announce(ws, lambda: tr("approved → {state}", state="DEV"),
                                                                             reply_to=77)
    assert notifier.sent == [("42", "🖥 Дашборд: схвалено → DEV", 77)]

async def test_a_failing_send_is_logged_not_raised(caplog): ...
```

- [ ] **Step 2: Run them to verify they fail.** Expected: FAIL.

- [ ] **Step 3: Implement**
  - The routes call `ticket_actions` and keep their `event_bus.emit("dashboard_…")` events.
  - They find the workspace with `_find_workspace`.
  - The Ukrainian entries go in `orchestrator/tg_uk/commands.py`.

- [ ] **Step 4: Run**
  - Run: `timeout 600 …/python -m pytest tests/unit/test_dashboard_actions.py tests/unit/test_ticket_announce.py tests/unit/test_dashboard_web.py tests/unit/test_tg_i18n.py -q -p no:cacheprovider`
  - Expected: all pass.

- [ ] **Step 5: Commit**: `feat(dashboard): answer, skip, retry from a stage, PR comment decisions and reviewed, announced in Telegram`

---

### Task 4: Reviewer action layer

**Files:**
- Create: `orchestrator/reviewer/actions.py`
- Modify:
  - `orchestrator/reviewer/replies.py`: `_execute`, `_post`, `_post_all`, `_check`, `_understand`, `_pr_action`, `_review_again`, `_address_again`, `_fix_findings`, `_skip` move to the layer; the constructor gains `actions=`.
  - `main.py`: build `Posting` and `ReviewerActions` once, outside the `TelegramAdapter` block; pass them to `ReviewerReplies`.
- Test: `tests/unit/test_reviewer_actions.py`; `tests/unit/test_reviewer_replies.py` passes unchanged.

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class ActionResult:
    state: str                 # "refused", "shown", "queued", or the PostOutcome state ("posted", "partial",
                               # "refused", "failed", "unverified", "nothing"); push_post: the post's when it ran
    texts: list[str]           # what was said, in the current language, in order
    command: ReplyCommand | None = None   # the checked command; None when refused before it was checked

LONG_ACTIONS = ("post", "post_replies", "push", "push_post", "text")   # Telegram runs these in a tracked task

class ReviewerActions:
    def __init__(self, contexts, store, service, posting, small, parser, *, credential_failing) -> None
    posting: Posting                                              # the one Posting both channels use
    def check(self, job: Job, cmd: ReplyCommand) -> ReplyCommand | str           # was _check
    def progress_line(self, job: Job, cmd: ReplyCommand) -> str                  # "Checking before pushing." etc.
    async def understand(self, job: Job, text: str) -> ReplyCommand | str        # was _understand minus the echo
    async def run(self, job: Job, cmd: ReplyCommand,
                  say: Callable[[str], Awaitable[None]] | None = None) -> ActionResult
```

- How the layer behaves:
  - `run` takes a checked command.
  - It passes each text to `say` as soon as it exists: the push outcome before the post that follows it. It also collects the texts in `texts`.
  - It never sends anything itself.
- `ReviewerReplies(..., actions: ReviewerActions | None = None)`:
  - When `actions` is None, it builds one from its own arguments (the test rig path).
  - It exposes `.actions`.

- [ ] **Step 1: Write the failing tests** (reuse `Rig` from `test_reviewer_replies.py`; build `ReviewerActions` from its fakes):

```python
async def test_run_says_each_text_in_order_and_returns_them(tmp_path):
    rig = Rig(tmp_path); said = []
    actions = rig.replies.actions
    cmd = actions.check(rig.job, ReplyCommand("post", numbers=[1]))
    result = await actions.run(rig.job, cmd, say=lambda t: _append(said, t))
    assert result.state == "posted" and result.texts == said == [rig.posting.outcome.text]

async def test_a_refusal_is_a_string_from_check(tmp_path):
    rig = Rig(tmp_path); rig.supersede(rig.job)
    assert actions_of(rig).check(rig.job, ReplyCommand("post", numbers=[1])).startswith(outdated_prefix())

def test_replies_given_actions_use_them(tmp_path):                        # Review Focus 1
    rig = Rig(tmp_path); shared = ReviewerActions(..., rig.posting, ...)
    replies = ReviewerReplies(..., actions=shared)
    assert replies.actions is shared and replies.actions.posting is rig.posting
```

- [ ] **Step 2: Run them to verify they fail.** Expected: FAIL (`orchestrator.reviewer.actions` is missing).

- [ ] **Step 3: Move the logic**
  - `ReviewerReplies._execute` becomes: `check` → answer a refusal; a `LONG_ACTIONS` command → answer `progress_line` (for post/push actions, as today), then `_track(run(..., say=answer))`; any other → `await run(..., say=answer)`.
  - `_understand` keeps the Yes/No echo and calls `actions.understand`.

- [ ] **Step 4: Run**
  - Run: `timeout 600 …/python -m pytest tests/unit/test_reviewer_actions.py tests/unit/test_reviewer_*.py tests/unit/test_command_handler.py -q -p no:cacheprovider`
  - Expected: all pass. `test_reviewer_replies.py` is untouched.

- [ ] **Step 5: Commit**: `refactor(reviewer): one action layer that Telegram's replies and the dashboard both call`

---

### Task 5: Buttons follow state

**Files:**
- Modify:
  - `orchestrator/reviewer/messages.py`: `fix_buttons`.
  - `integrations/base/notifier.py`: `set_buttons` with a no-op default.
  - `integrations/telegram/telegram_adapter.py`: `set_buttons`; the keyboard building in `send_message` moves into a shared `_markup(buttons)`.
  - `orchestrator/reviewer/replies.py`: `refresh_buttons`, called after every posting-type `run` that is not refused.
- Test: `tests/unit/test_reviewer_messages.py` (or the existing test file of `fix_buttons`: `grep -rn fix_buttons tests`), `tests/unit/test_telegram_adapter.py`, `tests/unit/test_reviewer_replies.py` (new tests only)

**Interfaces:**
- Produces:
  - `fix_buttons(kind, job_id, replies, fix, *, pushed: bool = False, unposted_replies: bool = True) -> list[Button]`:
    - pushed → no Push / Push anyway / Push + reply all;
    - "Reply all" only while `unposted_replies`;
    - Diff stays while there is a fix.
  - `NotifierInterface.set_buttons(chat_id: str, message_id: int, buttons: list[Button] | None) -> None`: not abstract, the default does nothing.
  - `ReviewerReplies.refresh_buttons(job_id: str) -> None` (async):
    - for kinds address/findings/recheck only;
    - the message is `run.message_ids[-1]`;
    - the labels are in `run.language`;
    - `pushed = store.push_of(job) is not None`;
    - `unposted_replies` = some reply number not in `store.posted_numbers(job, None)`;
    - a failure is logged, never raised.

- [ ] **Step 1: Write the failing tests**

```python
def test_after_the_push_only_reply_all_and_diff_remain():
    assert labels(fix_buttons("address", "j", REPLIES, {"files": ["a"]}, pushed=True)) == ["Reply all", "Diff"]
    assert labels(fix_buttons("address", "j", REPLIES, {"files": ["a"]}, pushed=True, unposted_replies=False)) == ["Diff"]

async def test_set_buttons_replaces_the_keyboard_in_rows(adapter_with_fake_bot):
    await adapter.set_buttons("1", 5, [Button("A", "a", row=0), Button("B", "b", row=1)])
    assert bot.edit_message_reply_markup.call_args.kwargs["reply_markup"].inline_keyboard == [[ib("A", "a")], [ib("B", "b")]]
    await adapter.set_buttons("1", 5, None)
    assert bot.edit_message_reply_markup.call_args.kwargs["reply_markup"] is None

async def test_a_telegram_push_refreshes_the_fix_message_buttons(tmp_path):
    rig = Rig(tmp_path); job = rig.add_job("address", 300); land_push(rig, job)
    await rig.tap("rv_push", job.id, 300)
    assert rig.chat.notifier.buttons_set[-1] == (CHAT, 300, ["Reply all", "Diff"])
```

- [ ] **Step 2: Run them to verify they fail.** Expected: FAIL.

- [ ] **Step 3: Implement**
  - Implement as specified.
  - `runner._deliver` keeps calling `fix_buttons` with the defaults: a fresh job is unpushed.

- [ ] **Step 4: Run**
  - Run: `timeout 600 …/python -m pytest tests/unit/test_reviewer_*.py tests/unit/test_telegram_adapter.py tests/unit/test_notifier_button.py -q -p no:cacheprovider`
  - Expected: all pass.

- [ ] **Step 5: Commit**: `feat(reviewer): a fix message's buttons follow the job's state after an action`

---

### Task 6: Dashboard reviewer API

**Files:**
- Modify:
  - `dashboard/reviews_api.py`: `build_review_routes(service, get_projects, actions=None, replies=None)`.
  - `dashboard/web.py`: `create_app(..., reviewer_actions=None, reviewer_replies=None)`.
  - `orchestrator/reviewer/replies.py`: `announce_dashboard`.
  - `main.py`: pass `reviewer_actions` and `reviewer_replies`.
  - `orchestrator/tg_uk/reviewer.py`: the new texts.
- Test: `tests/unit/test_dashboard_reviews.py`, `tests/unit/test_reviewer_replies.py` (new tests)

**Interfaces:**
- Consumes: `ReviewerActions` (Task 4); `refresh_buttons` (Task 5).
- Produces:
  - `POST /api/reviews/action`:
    - Body: `{"scope", "number", "job_id", "command": {ReplyCommand.to_dict()} | "text": str}`.
    - 400: the job is not this PR's, the body is malformed, or neither command nor text is given.
    - 404: unknown job.
    - Response for a run: `{"state", "texts", "command": describe(cmd, job.head_sha)}`.
    - Response for text the LLM understood: `{"state": "confirm", "question": "Understood: …. Correct?", "confirmation": id}`.
    - The confirmation is stored with `chat_id="dashboard"`.
    - Text the grammar parses runs at once, as in Telegram.
  - `POST /api/reviews/confirm` with `{"confirmation": id, "yes": bool}`:
    - Response: the run's result, or `{"state": "refused", "texts": ["Expired: reply again; nothing was done."]}` past `CONFIRM_TTL`.
    - A second claim gets "Already handled.".
  - Both routes:
    - Their run sits inside `telegram_language(ENGLISH)`.
    - Both go through the existing `_cross_site` check.
    - After a result that is neither "refused" nor "shown", they call `replies.announce_dashboard(job_id, cmd, state)` when `replies` is not None. A failure there is logged, and the HTTP answer is unchanged.
  - `ReviewerReplies.announce_dashboard(job_id: str, cmd: ReplyCommand, state: str) -> None` (async):
    - In the job's anchor language, it answers as a reply to `run.message_ids[-1]`:
      `tr("🖥 Dashboard: {command} — {outcome}", command=describe(cmd, job.head_sha), outcome=tr(OUTCOME[state]))`.
    - It adds " " + the first url of the newest posted Post of this job, when there is one.
    - Then it calls `refresh_buttons(job_id)`.
    - `OUTCOME`: posted "done", partial "partly done", failed "failed", unverified "not verified", nothing "nothing to do", queued "queued".

- [ ] **Step 1: Write the failing tests**

```python
def test_a_button_command_runs_and_answers_in_english(client_with_actions):
    r = c.post("/api/reviews/action", json={"scope": "acme/app", "number": 7, "job_id": J,
                                            "command": {"action": "push"}})
    assert r.json()["state"] == "posted" and r.json()["texts"][0].startswith("Pushed")
    assert replies.announced == [(J, "push", "posted")]

def test_a_superseded_job_is_refused(client_with_actions):                 # Review Focus 2
    supersede(J)
    r = post_action(c, command={"action": "post", "numbers": [1]})
    assert r.json()["state"] == "refused" and replies.announced == [] and posting.calls == []

def test_a_job_of_another_pr_is_400(client_with_actions): ...

def test_text_the_grammar_reads_runs_and_free_text_asks_first(client_with_actions):
    assert post_action(c, text="post replies 1 2").json()["state"] == "posted"
    r = post_action(c, text="please post the first two replies")
    assert r.json()["state"] == "confirm" and r.json()["question"].startswith("Understood: ")

def test_an_expired_dashboard_confirmation_runs_nothing(client_with_actions, clock):   # Review Focus 5
    conf = post_action(c, text="please post the first two").json()["confirmation"]
    clock.advance(minutes=16)
    r = c.post("/api/reviews/confirm", json={"confirmation": conf, "yes": True})
    assert r.json() == {"state": "refused", "texts": ["Expired: reply again; nothing was done."]} and posting.calls == []

def test_the_action_completes_when_the_announcement_fails(client_with_failing_replies):  # Review Focus 3
    assert post_action(c, command={"action": "push"}).status_code == 200

async def test_announce_dashboard_replies_to_the_job_message_in_its_language(tmp_path):
    rig = Rig(tmp_path, language="uk"); ...
    assert rig.chat.sent[-1] == ("🖥 Дашборд: push — виконано https://github.com/acme/app/commit/abc", rig.at(100))
```

- [ ] **Step 2: Run them to verify they fail.** Expected: FAIL (route 404 / method missing).

- [ ] **Step 3: Implement** as specified. Use `ReplyCommand.from_dict`. Use `parse_reply` for text; on None, call `actions.understand`.

- [ ] **Step 4: Run**
  - Run: `timeout 600 …/python -m pytest tests/unit/test_dashboard_reviews.py tests/unit/test_reviewer_*.py tests/unit/test_tg_i18n.py -q -p no:cacheprovider`
  - Expected: all pass.

- [ ] **Step 5: Commit**: `feat(dashboard): push, post and reply commands from the PR panel, announced in Telegram`

---

### Task 7: PR panel UI

**Files:**
- Create: `dashboard/static/js/review-actions.js` (pure: no DOM)
- Modify:
  - `dashboard/static/js/reviews.js`: the panel body, `fixHtml`, `repliesHtml`, `reportHtml`, the "…in Telegram" texts at lines ~327–377.
  - `dashboard/static/js/reviews-board.js`: the "Nothing is pushed until you reply \"push\" in Telegram" text at line ~347.
  - `dashboard/static/js/api.js`: `reviewAction`, `reviewConfirm`.
  - `dashboard/static/style.css`.
- Test: `tests/js/review-actions.test.mjs`

**Interfaces:**
- Consumes: Task 6's routes; `ReviewerService.detail` fields (`report_job`, `replies_job`, `fix_job`, `fix`, `pushed_sha`, `replies`, `reply_states`, `number_states`, `jobs`).
- Produces:
  - `prActions(detail) -> {fix: Button[], replies: Button[], report: Button[], commandJob: string|null}`, where `Button = {label, command, confirm?}`. It mirrors `fix_buttons` with `pushed` taken from `pushed_sha`.
  - `commandJob` is the newest of `detail.jobs` whose id is `report_job`, `replies_job`, `verdicts_job` or `fix_job`. This is the job the command field addresses (plan decision: the newest job the operator could reply to).

- [ ] **Step 1: Write the failing tests**

```js
test('a protected fix offers only push anyway, with a confirm', () => {
  const a = prActions(detail({ fix_job: 'f', fix: { protected: ['.github/x.yml'] }, replies: [r(1)] }));
  assert.deepEqual(a.fix.map(b => b.label), ['Push anyway + reply all…']);
  assert.equal(a.fix[0].confirm.includes('.github/x.yml'), true);
});
test('after the push: reply to open replies only', () => {
  const a = prActions(detail({ fix_job: 'f', fix: {}, pushed_sha: 'abc', replies: [r(1), r(2)], reply_states: { 1: 'posted', 2: 'open' } }));
  assert.deepEqual([...a.fix, ...a.replies].map(b => b.label), ['Reply all', 'Reply to checked']);
});
test('a report offers post all confirmed and post checked with the three events', () => {
  assert.deepEqual(prActions(detail({ report_job: 'r', number_states: { 1: 'open' } })).report.map(b => b.command),
    [{ action: 'post', all_confirmed: true }, { action: 'post', numbers: 'checked' }]);
});
```

- [ ] **Step 2: Run to verify they fail**
  - Run: `node --test 'tests/js/*.test.mjs'`
  - Expected: FAIL (module missing).

- [ ] **Step 3: Implement**
  - `prActions` is pure.
  - In `reviews.js`:
    - Render the bars from it.
    - Add checkboxes to open finding and reply rows.
    - Add the event radio: Comment / Approve / Request changes, default Comment.
    - Add the command field.
    - Show the result line coloured by state.
    - While a request runs, disable the panel's buttons.
    - Reload the panel after each request.
    - For `state === "confirm"`, show the question with Yes / No (`showConfirmDialog` from actions.js).
  - Replace the "…in Telegram" texts with ones naming both places, e.g. "Not pushed yet: push here or in Telegram".

- [ ] **Step 4: Run**
  - Run: `node --test 'tests/js/*.test.mjs'`
  - Expected: all pass.

- [ ] **Step 5: Commit**: `feat(dashboard): the PR panel's push, post and command controls`

---

### Task 8: Ticket panel UI

**Files:**
- Create: `dashboard/static/js/ticket-actions.js` (pure)
- Modify:
  - `dashboard/static/js/detail.js`: `buildActionBar`, `buildInfoSection`'s error panel, `bindActionButtons`, and a new `buildPrComments`.
  - `dashboard/static/js/actions.js`: `skipWorkspace`, `answerWorkspace`, `decideComment`, `markReviewed`, and `retryWorkspace(ticketId, stage)`.
  - `dashboard/static/style.css`.
- Test: `tests/js/ticket-actions.test.mjs`

**Interfaces:**
- Consumes: Task 3's routes and row fields.
- Produces: `ticketControls(ws) -> {retry: boolean, stages: string[], skip: boolean, answer: boolean, comments: object[], reviewed: boolean}`:
  - `stages` = `['analysis', 'plan_review', 'dev', 'scope_check', 'code_review', 'qa', 'push']` (the same keys as `RETRY_STAGES`);
  - `retry` and `skip` for BLOCKED or FAILED;
  - `answer` when BLOCKED and `human_input_pending`;
  - `comments` = the undecided `pending_review_comments` when PR_REVIEW;
  - `reviewed` when PR_REVIEW.

- [ ] **Step 1: Write the failing tests**

```js
test('a blocked ticket waiting for input gets the answer box, retry and skip', () => {
  assert.deepEqual(pick(ticketControls(ws('BLOCKED', { human_input_pending: true }))), { retry: true, skip: true, answer: true, reviewed: false });
});
test('a PR_REVIEW ticket lists only undecided comments and offers reviewed', () => {
  const c = ticketControls(ws('PR_REVIEW', { pending_review_comments: [cm('1', null), cm('2', 'fix')] }));
  assert.deepEqual(c.comments.map(x => x.comment_id), ['1']); assert.equal(c.reviewed, true);
});
```

- [ ] **Step 2: Run to verify they fail**
  - Run: `node --test 'tests/js/*.test.mjs'`
  - Expected: FAIL.

- [ ] **Step 3: Implement**
  - Retry gets a "From stage…" select beside it.
  - Skip asks for confirmation (`showConfirmDialog`).
  - The answer box goes under the "Error / Escalation" panel, with `human_input_question` above it when present.
  - The PR comments block shows author, file:line, the suggestion and the assessment, then Fix / Won't fix (an optional reason field) / Re-investigate (a required hint field).
  - The Reviewed button goes in the action bar.
  - Each control reloads the ticket after the request and shows a refusal's message.

- [ ] **Step 4: Run**
  - Run: `node --test 'tests/js/*.test.mjs'`
  - Expected: all pass.

- [ ] **Step 5: Commit**: `feat(dashboard): answer, retry from a stage, skip, PR comment decisions and reviewed in the ticket panel`

---

### Task 9: Docs and whole-branch verification

**Files:**
- Modify:
  - `docs/telegram.md`: a short section on actions sent from the dashboard being announced with 🖥, and the buttons that follow state.
  - The dashboard's doc, if one exists (`ls docs | grep -i dash`), else `README.md`'s dashboard lines: what can be done from it, and the allow list.

- [ ] **Step 1: Write the doc changes**: what each panel can do, and the `dashboard.allowed_networks` key and its default.
- [ ] **Step 2: Full suite**
  - Run: `timeout 900 …/python -m pytest tests --ignore=tests/e2e -q -p no:cacheprovider` and `node --test 'tests/js/*.test.mjs'`.
  - Expected: only the nine known failures; JS all pass.
- [ ] **Step 3: See it working**
  - Start the dashboard on a copy of the events DB and the reviewer DB, as the memory note "Dashboard preview in headless Chrome" describes: uvicorn plus headless Chrome over CDP.
  - Open a PR panel with a fix and a blocked ticket.
  - Screenshot both, and check that the buttons match `prActions` / `ticketControls`.
  - Do not click actions that would push or post: it is a copy of live data.
- [ ] **Step 4: Commit**: `docs: controlling Cleave from the dashboard`
