# Orchestrator Layer Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut `orchestrator/orchestrator.py` (2,801 LOC, one class, ~50 methods) into focused modules; generalize `TrackerInterface` so future Trello/GitLab adapters drop in without touching orchestrator code. **Behavior-preserving.**

**Architecture:** Three layers — Runtime (daemon shell), Coordination (use-cases/verbs), Ports & adapters (already present, tightened). The current `Orchestrator` class becomes a thin DI container (~300–400 LOC). Tracker port gains 4 new methods (`get_comments`, `get_status_history`, `download_attachment`, `list_transitions`) that close the Jira-specific leaks in orchestrator (`_request`, `_email`, `_token`).

**Tech Stack:** Python 3.11+, asyncio, pytest, httpx. Existing dataclasses (`TicketData`, `Workspace`, `Stage`) stay where they are.

**Companion spec:** [`docs/superpowers/specs/2026-05-11-orchestrator-layer-refactor-design.md`](../specs/2026-05-11-orchestrator-layer-refactor-design.md)

---

## File structure

**New files:**
```
orchestrator/git_ops.py
orchestrator/ticket_sync.py
orchestrator/notify.py
orchestrator/approval_gate.py
orchestrator/escalation.py
orchestrator/ingest.py
orchestrator/runtime.py
orchestrator/pipeline/__init__.py
orchestrator/pipeline/driver.py
orchestrator/pipeline/agent_stage.py
orchestrator/pipeline/action_stage.py
orchestrator/pipeline/actions/__init__.py
orchestrator/pipeline/actions/push_and_open_pr.py
orchestrator/pipeline/actions/fetch_pr_comments.py
orchestrator/pipeline/actions/finalize.py

tests/unit/test_orchestrator_poll_create.py
tests/unit/test_orchestrator_advance_happy.py
tests/unit/test_orchestrator_pr_review_loop.py
tests/unit/test_orchestrator_finalize.py
tests/unit/test_orchestrator_done.py
tests/unit/test_orchestrator_reconcile.py
tests/unit/test_orchestrator_notify_misc.py
```

**Modified files:**
```
integrations/base/tracker.py            (+ dataclasses, + 4 abstract methods)
integrations/jira/jira_adapter.py       (+ 4 implementations, move _extract_adf_text)
config/schemas.py                       (hoist default_branch/branch_prefix; rename to tracker_label)
config/config_loader.py                 (accept jira_repo_label alias)
orchestrator/orchestrator.py            (shrink to ~300 LOC wiring)
tests/unit/test_orchestrator_refetch.py (update mocks for new tracker API)
tests/unit/test_action_stage.py         (update mocks)
tests/integration/test_jira_adapter.py  (cover new methods)
```

---

## Conventions

- **Verify command:** Each task ends with `pytest tests/unit/test_orchestrator_*.py tests/unit/test_action_stage.py -x` (fast subset) and where touched, `pytest tests/integration/ -x`.
- **Commit boundary:** One commit per task. Message format: `refactor(orchestrator): <task title>`. No `Co-Authored-By` lines.
- **Behavior preservation:** Do not "improve" logic during a move. If you spot a bug, leave a `# TODO(refactor-followup):` comment and continue; file an issue after the PR lands.
- **Backward-compat shim:** During Steps D and E, retain method shims on `Orchestrator` that delegate to the moved functions. This is removed in Task G2.

---

## Phase 0: Characterization tests (before any production code moves)

These pin down current behavior. They are deliberately **shaped against the existing method names on `Orchestrator`** — Phase F updates their imports once the methods move.

### Task 0.1: Ingest path tests

**Files:**
- Create: `tests/unit/test_orchestrator_poll_create.py`

- [ ] **Step 1: Write the tests**

```python
"""Characterization tests for _poll_and_create_workspaces and _create_workspace_for_ticket.

Pin down: tracker polling, label-based routing, dedupe (memory + disk),
per-project parallelism cap, dry-run no-op.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.base.tracker import TicketData
from orchestrator.orchestrator import Orchestrator
from workspace.workspace import Stage


def _make_orchestrator(
    *,
    tracker=None,
    projects=None,
    active=None,
    workspaces_base: Path,
    dry_run: bool = False,
):
    """Build a minimally-wired Orchestrator with __new__ to skip __init__ deps."""
    orc = Orchestrator.__new__(Orchestrator)
    orc._tracker = tracker
    orc._projects = projects or {}
    orc._active_workspaces = active or []
    orc._dry_run = dry_run
    orc._global_config = SimpleNamespace(
        workspaces=SimpleNamespace(base_dir=str(workspaces_base)),
        defaults=SimpleNamespace(max_parallel_tickets=3),
        telegram=SimpleNamespace(default_chat_id=""),
    )
    orc._workspace_manager = MagicMock()
    orc._on_project_added = None
    orc._events = None
    orc._registry = MagicMock()
    return orc


def _ticket(ticket_id: str, labels: list[str]) -> TicketData:
    return TicketData(
        id=ticket_id, url=f"https://j/{ticket_id}",
        summary=f"summary {ticket_id}", description="", labels=labels,
    )


def _project(repo_id: str, repo_label: str, max_parallel: int = 5):
    repo = SimpleNamespace(
        repo=SimpleNamespace(id=repo_id),
        jira_repo_label=repo_label,
        git=SimpleNamespace(clone_url="https://x/y.git", depth=0),
        vcs=SimpleNamespace(
            provider="github",
            github=SimpleNamespace(default_branch="develop", branch_prefix="feature"),
            gitlab=SimpleNamespace(default_branch="develop", branch_prefix="feature"),
        ),
    )
    return SimpleNamespace(
        config=SimpleNamespace(
            jira=SimpleNamespace(
                url="https://x", trigger_labels=["ai-pipeline"], ignore_labels=[],
            ),
            parallelism=SimpleNamespace(max_concurrent_tickets=max_parallel),
        ),
        repos={repo_id: repo},
    )


@pytest.mark.asyncio
async def test_poll_routes_by_repo_label(tmp_path: Path) -> None:
    """A ticket whose labels include repo_label gets a workspace under that repo."""
    tracker = AsyncMock()
    tracker.poll_tickets.return_value = [
        _ticket("PROJ-1", labels=["ai-pipeline", "android"]),
    ]
    orc = _make_orchestrator(
        tracker=tracker,
        projects={"acme": _project("android", "android")},
        workspaces_base=tmp_path,
    )
    orc._create_workspace_for_ticket = AsyncMock(
        return_value=SimpleNamespace(state=SimpleNamespace(
            ticket_id="PROJ-1", company_id="acme", current_state=Stage.ANALYSIS,
        )),
    )

    await orc._poll_and_create_workspaces()

    orc._create_workspace_for_ticket.assert_called_once()
    pt_arg = orc._create_workspace_for_ticket.call_args.args[0]
    assert pt_arg.ticket.id == "PROJ-1"
    assert pt_arg.repo_id == "android"


@pytest.mark.asyncio
async def test_poll_dedupes_in_memory(tmp_path: Path) -> None:
    """A ticket already in _active_workspaces is skipped."""
    tracker = AsyncMock()
    tracker.poll_tickets.return_value = [_ticket("PROJ-1", ["ai-pipeline", "android"])]
    existing = SimpleNamespace(state=SimpleNamespace(
        ticket_id="PROJ-1", company_id="acme", current_state=Stage.DEV,
    ))
    orc = _make_orchestrator(
        tracker=tracker,
        projects={"acme": _project("android", "android")},
        active=[existing], workspaces_base=tmp_path,
    )
    orc._create_workspace_for_ticket = AsyncMock()

    await orc._poll_and_create_workspaces()

    orc._create_workspace_for_ticket.assert_not_called()


@pytest.mark.asyncio
async def test_poll_dedupes_on_disk(tmp_path: Path) -> None:
    """A ticket whose workspace exists on disk is skipped even if not in memory."""
    tracker = AsyncMock()
    tracker.poll_tickets.return_value = [_ticket("PROJ-1", ["ai-pipeline", "android"])]
    orc = _make_orchestrator(
        tracker=tracker,
        projects={"acme": _project("android", "android")},
        workspaces_base=tmp_path,
    )
    # Pre-create the workspace dir on disk
    (tmp_path / "acme" / "android" / "tickets" / "PROJ-1").mkdir(parents=True)
    orc._create_workspace_for_ticket = AsyncMock()

    await orc._poll_and_create_workspaces()

    orc._create_workspace_for_ticket.assert_not_called()


@pytest.mark.asyncio
async def test_poll_respects_parallel_cap(tmp_path: Path) -> None:
    """When active count >= max_concurrent_tickets, remaining tickets are skipped."""
    tracker = AsyncMock()
    tracker.poll_tickets.return_value = [
        _ticket(f"PROJ-{i}", ["ai-pipeline", "android"]) for i in range(5)
    ]
    active = [
        SimpleNamespace(state=SimpleNamespace(
            ticket_id=f"OLD-{i}", company_id="acme", current_state=Stage.DEV,
        )) for i in range(2)
    ]
    orc = _make_orchestrator(
        tracker=tracker,
        projects={"acme": _project("android", "android", max_parallel=2)},
        active=list(active), workspaces_base=tmp_path,
    )
    orc._create_workspace_for_ticket = AsyncMock()

    await orc._poll_and_create_workspaces()

    # Already at cap (2/2) — no new workspaces
    orc._create_workspace_for_ticket.assert_not_called()


@pytest.mark.asyncio
async def test_poll_dry_run_no_create(tmp_path: Path) -> None:
    """dry_run=True logs but does not call _create_workspace_for_ticket."""
    tracker = AsyncMock()
    tracker.poll_tickets.return_value = [_ticket("PROJ-1", ["ai-pipeline", "android"])]
    orc = _make_orchestrator(
        tracker=tracker,
        projects={"acme": _project("android", "android")},
        workspaces_base=tmp_path, dry_run=True,
    )
    orc._create_workspace_for_ticket = AsyncMock()

    await orc._poll_and_create_workspaces()

    orc._create_workspace_for_ticket.assert_not_called()
```

- [ ] **Step 2: Run and verify they pass against current code**

```
pytest tests/unit/test_orchestrator_poll_create.py -v
```
Expected: 5 passed.

- [ ] **Step 3: Commit**

```
git add tests/unit/test_orchestrator_poll_create.py
git commit -m "test(orchestrator): characterize _poll_and_create_workspaces"
```

---

### Task 0.2: Advance happy-path tests

**Files:**
- Create: `tests/unit/test_orchestrator_advance_happy.py`

- [ ] **Step 1: Write the tests**

```python
"""Characterization tests for the advance_workspace happy path.

Pins down: terminal-state skip, max-iteration → escalate, agent stage success
→ advance, action stage success → advance.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.orchestrator import Orchestrator
from workspace.workspace import Stage


def _orc():
    orc = Orchestrator.__new__(Orchestrator)
    orc._workflow = SimpleNamespace(stages={})
    orc._dry_run = False
    orc._mode_handler = None
    orc._events = None
    orc._agent_runtime = MagicMock()
    orc._notifier = None
    orc._tracker = None
    orc._projects = {}
    orc._global_config = SimpleNamespace(telegram=SimpleNamespace(default_chat_id=""))
    orc._workspace_manager = MagicMock()
    return orc


def _ws(state):
    ws = MagicMock()
    ws.state = SimpleNamespace(
        ticket_id="T-1", company_id="acme", current_state=state,
        stage_iterations={}, previous_state="",
    )
    ws.transition = MagicMock()
    ws.update_state = MagicMock()
    ws.save_state = MagicMock()
    return ws


@pytest.mark.asyncio
async def test_advance_skips_terminal_states() -> None:
    """DONE, ARCHIVED, BLOCKED, MANUAL_CONTROL short-circuit immediately."""
    orc = _orc()
    for state in (Stage.DONE, Stage.ARCHIVED, Stage.BLOCKED, Stage.MANUAL_CONTROL):
        ws = _ws(state)
        await orc.advance_workspace(ws)
        ws.transition.assert_not_called()


@pytest.mark.asyncio
async def test_advance_max_iterations_escalates(monkeypatch) -> None:
    """When iteration count >= max_iterations, escalate is triggered."""
    orc = _orc()
    orc._handle_escalate = AsyncMock()
    ws = _ws(Stage.DEV)
    ws.state.stage_iterations = {"dev": 5}

    orc._workflow = SimpleNamespace(stages={
        "dev": SimpleNamespace(max_iterations=5, agent="dev-agent", action=None),
    })

    # Patch routing helpers to deterministic answers
    monkeypatch.setattr(
        "orchestrator.orchestrator.should_escalate", lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "orchestrator.orchestrator.get_next_stage", lambda *a, **k: "escalate",
    )

    await orc.advance_workspace(ws)
    orc._handle_escalate.assert_awaited_once()


@pytest.mark.asyncio
async def test_advance_dispatches_agent_stage() -> None:
    """A stage with `agent` set routes to _handle_agent_stage."""
    orc = _orc()
    orc._handle_agent_stage = AsyncMock()
    orc._workflow = SimpleNamespace(stages={
        "dev": SimpleNamespace(max_iterations=0, agent="dev-agent", action=None),
    })
    ws = _ws(Stage.DEV)
    await orc.advance_workspace(ws)
    orc._handle_agent_stage.assert_awaited_once()


@pytest.mark.asyncio
async def test_advance_dispatches_action_stage() -> None:
    """A stage with `action` set routes to _handle_action_stage."""
    orc = _orc()
    orc._handle_action_stage = AsyncMock()
    orc._workflow = SimpleNamespace(stages={
        "push": SimpleNamespace(
            max_iterations=0, agent=None, action="push_and_open_pr",
        ),
    })
    ws = _ws(Stage.PUSH)
    await orc.advance_workspace(ws)
    orc._handle_action_stage.assert_awaited_once()
```

- [ ] **Step 2: Run and verify**

```
pytest tests/unit/test_orchestrator_advance_happy.py -v
```
Expected: 4 passed.

- [ ] **Step 3: Commit**

```
git add tests/unit/test_orchestrator_advance_happy.py
git commit -m "test(orchestrator): characterize advance_workspace happy path"
```

---

### Task 0.2b: `_parse_agent_outcome` mapping test

The agent success path inside `_handle_agent_stage` depends on
`_parse_agent_outcome(stage_id, output, workspace) -> 'pass' | 'fail' | None`.
Existing tests mock this method; its actual text→outcome mapping has no
coverage. The method moves to `pipeline/agent_stage.py` in Task E.1 — pin
the contract first.

**Files:**
- Create: `tests/unit/test_orchestrator_outcome_parser.py`

- [ ] **Step 1: Read the current method**

`orchestrator/orchestrator.py:2630–2665`. It uses module-level helpers
`_looks_like_pass` (line 2784) and `_looks_like_fail` (line 2796).

- [ ] **Step 2: Write tests against current behavior**

```python
"""Characterization test for Orchestrator._parse_agent_outcome.

The function maps agent stdout to one of {'pass', 'fail', None}. It is
called by _handle_agent_stage to decide the next stage. Pin the mapping
on a handful of canonical strings.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.orchestrator import Orchestrator


def _orc():
    return Orchestrator.__new__(Orchestrator)


def _ws():
    ws = MagicMock()
    ws.state = SimpleNamespace(ticket_id="T-1")
    return ws


def test_parse_outcome_pass_phrases() -> None:
    orc = _orc()
    for text in (
        "status: pass",
        "VERDICT: PASS — all clear",
        "QA pass; advances to qa",
        "All gates passed.",
    ):
        assert orc._parse_agent_outcome("qa", text.lower(), _ws()) == "pass", text


def test_parse_outcome_fail_phrases() -> None:
    orc = _orc()
    for text in (
        "status: fail",
        "VERDICT: FAIL",
        "status: blocked",
    ):
        assert orc._parse_agent_outcome("qa", text.lower(), _ws()) == "fail", text


def test_parse_outcome_no_verdict_returns_none() -> None:
    orc = _orc()
    assert orc._parse_agent_outcome("dev", "just some commentary", _ws()) is None
```

- [ ] **Step 3: Run and verify**

```
pytest tests/unit/test_orchestrator_outcome_parser.py -v
```

If a specific assertion fails, the *current* behavior is the contract — adjust the assertion (or drop that case) to match. The test must pass against the unmodified source.

- [ ] **Step 4: Commit**

```
git add tests/unit/test_orchestrator_outcome_parser.py
git commit -m "test(orchestrator): characterize _parse_agent_outcome mapping"
```

---

### Task 0.3: PR review loop tests

This is the highest-risk area (350+ untested lines). Write 5 cases.

**Files:**
- Create: `tests/unit/test_orchestrator_pr_review_loop.py`

- [ ] **Step 1: Write the tests**

```python
"""Characterization tests for _action_fetch_pr_comments and helpers.

The 350-line PR-review escalation loop has had effectively zero coverage.
These tests pin the externally observable contract: outcome state, calls
to tracker/vcs/notifier, written resolution-report entries.

Each test only pins the minimum needed. Adapt mocks if internal helpers
change shape, but the assertions must continue to hold.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.orchestrator import Orchestrator
from workspace.workspace import Stage


@pytest.fixture
def workspace(tmp_path: Path):
    ws = MagicMock()
    ws.source_dir = tmp_path / "src"
    ws.source_dir.mkdir()
    ws.reports_dir = tmp_path / "ai_pipeline" / "T-1"
    ws.reports_dir.mkdir(parents=True)
    ws.state = SimpleNamespace(
        ticket_id="T-1", company_id="acme", repo_id="android",
        current_state=Stage.PR_REVIEW, pr_number=42,
        pr_url="https://g/pr/42", branch="feature/T-1",
        stage_iterations={}, escalation_msg_id=None,
        escalation_chat_id=None, human_input_reply=None,
        human_input_question=None,
    )
    ws.transition = MagicMock()
    ws.update_state = MagicMock()
    ws.save_state = MagicMock()
    ws.meta_dir = tmp_path / "meta"
    ws.meta_dir.mkdir()
    return ws


def _orc(vcs=None, notifier=None, tracker=None):
    orc = Orchestrator.__new__(Orchestrator)
    orc._vcs = vcs or AsyncMock()
    orc._tracker = tracker or AsyncMock()
    orc._notifier = notifier or AsyncMock()
    orc._repo_vcs = {}
    orc._projects = {}
    orc._global_config = SimpleNamespace(
        telegram=SimpleNamespace(default_chat_id="chat-1"),
    )
    orc._events = None
    return orc


@pytest.mark.asyncio
async def test_no_pr_number_marks_done(workspace) -> None:
    """If state.pr_number is missing, action returns DONE / skip semantics."""
    workspace.state.pr_number = None
    orc = _orc()
    stage_def = SimpleNamespace()
    result = await orc._action_fetch_pr_comments(workspace, stage_def)
    # Existing behavior: skipped=True so iteration is rolled back
    assert getattr(result, "skipped", False) or result.next_state == Stage.DONE


@pytest.mark.asyncio
async def test_no_comments_marks_done(workspace) -> None:
    """Empty review-comment list → workspace marked DONE."""
    vcs = AsyncMock()
    vcs.get_pr_comments.return_value = []
    vcs.check_pr_status.return_value = SimpleNamespace(all_passing=True, checks=[])
    orc = _orc(vcs=vcs)
    stage_def = SimpleNamespace()
    result = await orc._action_fetch_pr_comments(workspace, stage_def)
    # Either next_state DONE or success without escalation
    assert result.next_state in (Stage.DONE, Stage.PR_REVIEW)


@pytest.mark.asyncio
async def test_human_reply_fix_routes_to_dev(workspace) -> None:
    """Human replied 'fix' to escalation → workspace routes back to DEV."""
    workspace.state.human_input_reply = "fix"
    workspace.state.human_input_question = "Should we fix comment #999?"
    vcs = AsyncMock()
    vcs.get_pr_comments.return_value = []
    orc = _orc(vcs=vcs)
    # Stage existence (existing logic gates on this)
    stage_def = SimpleNamespace()
    result = await orc._action_fetch_pr_comments(workspace, stage_def)
    # Acceptable: either result.next_state == Stage.DEV or workspace transition called with DEV
    assert (
        result.next_state == Stage.DEV
        or any(c.args[0] == Stage.DEV for c in workspace.transition.call_args_list)
    )


@pytest.mark.asyncio
async def test_human_reply_wont_fix_posts_resolution(workspace) -> None:
    """Human replied 'won't fix: <reason>' → reply posted + comment resolved."""
    workspace.state.human_input_reply = "won't fix: by design"
    workspace.state.human_input_question = "Resolve comment #999?"
    vcs = AsyncMock()
    vcs.get_pr_comments.return_value = []
    orc = _orc(vcs=vcs)
    stage_def = SimpleNamespace()
    await orc._action_fetch_pr_comments(workspace, stage_def)
    # Either reply_to_comment OR resolve_comment was awaited
    assert vcs.reply_to_comment.await_count + vcs.resolve_comment.await_count >= 1


@pytest.mark.asyncio
async def test_open_comments_trigger_escalation(workspace) -> None:
    """Unaddressed PR comments cause a Telegram escalation to be sent."""
    from integrations.base.vcs import PRComment, PRStatus
    vcs = AsyncMock()
    vcs.get_pr_comments.return_value = [
        PRComment(id=999, body="please rename foo to bar", path="x.py", line=10,
                  author="reviewer"),
    ]
    vcs.check_pr_status.return_value = PRStatus(all_passing=True, checks=[])
    orc = _orc(vcs=vcs)
    stage_def = SimpleNamespace()
    await orc._action_fetch_pr_comments(workspace, stage_def)
    # Notifier was called at least once with the escalation chat
    assert orc._notifier.send_message.await_count >= 1
```

> Note: these tests are intentionally tolerant of internal helper shapes — they only assert externally observable contracts. If a specific assertion fails because the current code already behaves differently than the test expects, **the test is wrong**, not the code. Update the assertion to match current behavior, then commit. The goal is "pin the *current* contract", not "specify the *ideal* contract".

- [ ] **Step 2: Run and verify**

```
pytest tests/unit/test_orchestrator_pr_review_loop.py -v
```
Expected: 5 passed. **If any fail:** read the actual orchestrator behavior at the relevant line and update the test assertion to match. The test should pass against the *current* code unmodified.

- [ ] **Step 3: Commit**

```
git add tests/unit/test_orchestrator_pr_review_loop.py
git commit -m "test(orchestrator): characterize PR review loop"
```

---

### Task 0.4: Finalize / Jira transition tests

**Files:**
- Create: `tests/unit/test_orchestrator_finalize.py`

- [ ] **Step 1: Write the tests**

```python
"""Characterization tests for _on_ticket_done fuzzy Jira transition.

The matching logic walks `tracker._request('GET', '/issue/.../transitions')`
output and POSTs the first matching transition. After refactor this same
logic must work via tracker.list_transitions() + tracker.transition_ticket().
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.orchestrator import Orchestrator
from workspace.workspace import Stage


def _orc(tracker, project):
    orc = Orchestrator.__new__(Orchestrator)
    orc._tracker = tracker
    orc._notifier = AsyncMock()
    orc._projects = {"acme": project}
    orc._global_config = SimpleNamespace(
        telegram=SimpleNamespace(default_chat_id="chat-1"),
    )
    orc._events = None
    orc._workflow = SimpleNamespace(stages={})
    return orc


def _ws():
    ws = MagicMock()
    ws.state = SimpleNamespace(
        ticket_id="T-1", company_id="acme", current_state=Stage.DONE,
        pr_url="https://g/pr/42",
    )
    ws.meta_dir = MagicMock()
    return ws


def _project(in_review="In Review"):
    return SimpleNamespace(
        config=SimpleNamespace(
            jira=SimpleNamespace(
                statuses=SimpleNamespace(in_review=in_review),
            ),
        ),
        repos={},
    )


@pytest.mark.asyncio
async def test_exact_match_transition_fires() -> None:
    """When 'In Review' is offered verbatim, that transition's id is POSTed."""
    tracker = AsyncMock()
    tracker._request.side_effect = [
        # GET /issue/.../transitions
        {"transitions": [
            {"id": "21", "name": "Start Review", "to": {"name": "In Review"}},
        ]},
        # POST /issue/.../transitions
        {},
    ]
    orc = _orc(tracker, _project("In Review"))
    await orc._on_ticket_done(_ws())
    # Two requests: GET then POST
    assert tracker._request.await_count == 2
    post_call = tracker._request.await_args_list[1]
    assert post_call.args[0] == "POST"


@pytest.mark.asyncio
async def test_fuzzy_match_on_review_keyword() -> None:
    """When target is missing but a transition contains 'review', it still fires."""
    tracker = AsyncMock()
    tracker._request.side_effect = [
        {"transitions": [
            {"id": "31", "name": "Ready for Review", "to": {"name": "Reviewing"}},
        ]},
        {},
    ]
    orc = _orc(tracker, _project("Nonexistent Status"))
    await orc._on_ticket_done(_ws())
    assert tracker._request.await_count == 2


@pytest.mark.asyncio
async def test_no_matching_transition_does_nothing_fatal() -> None:
    """When no transition matches even fuzzily, _on_ticket_done logs and returns
    without raising."""
    tracker = AsyncMock()
    tracker._request.return_value = {"transitions": [
        {"id": "11", "name": "Close as Won't Do", "to": {"name": "Closed"}},
    ]}
    orc = _orc(tracker, _project("In Review"))
    # Should not raise
    await orc._on_ticket_done(_ws())
    # GET happened; POST did not
    assert tracker._request.await_count == 1
```

- [ ] **Step 2: Run and verify**

```
pytest tests/unit/test_orchestrator_finalize.py -v
```
Expected: 3 passed.

- [ ] **Step 3: Commit**

```
git add tests/unit/test_orchestrator_finalize.py
git commit -m "test(orchestrator): characterize _on_ticket_done fuzzy match"
```

---

### Task 0.5: `_on_ticket_done` notification test

**Files:**
- Create: `tests/unit/test_orchestrator_done.py`

- [ ] **Step 1: Write the test**

```python
"""Characterization test for _on_ticket_done DONE notification."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.orchestrator import Orchestrator
from workspace.workspace import Stage


@pytest.mark.asyncio
async def test_done_sends_completion_message(monkeypatch) -> None:
    """_on_ticket_done sends a TG message containing pipeline-complete copy."""
    orc = Orchestrator.__new__(Orchestrator)
    notifier = AsyncMock()
    tracker = AsyncMock()
    tracker._request.return_value = {"transitions": []}
    orc._notifier = notifier
    orc._tracker = tracker
    orc._projects = {"acme": SimpleNamespace(
        config=SimpleNamespace(
            jira=SimpleNamespace(statuses=SimpleNamespace(in_review="")),
        ),
        repos={},
    )}
    orc._global_config = SimpleNamespace(
        telegram=SimpleNamespace(default_chat_id="chat-1"),
    )
    orc._events = None

    ws = MagicMock()
    ws.state = SimpleNamespace(
        ticket_id="T-1", company_id="acme", current_state=Stage.DONE,
        pr_url="https://g/pr/42",
    )
    ws.meta_dir = MagicMock()

    # tg_format.read_ticket_title hits the filesystem; patch it
    monkeypatch.setattr(
        "orchestrator.tg_format.read_ticket_title", lambda w: "Sample title",
    )

    await orc._on_ticket_done(ws)

    assert notifier.send_message.await_count == 1
    chat_id, message = notifier.send_message.await_args.args[:2]
    assert chat_id == "chat-1"
    assert "Pipeline complete" in message
    assert "https://g/pr/42" in message
```

- [ ] **Step 2: Run and verify**

```
pytest tests/unit/test_orchestrator_done.py -v
```
Expected: 1 passed.

- [ ] **Step 3: Commit**

```
git add tests/unit/test_orchestrator_done.py
git commit -m "test(orchestrator): characterize _on_ticket_done notification"
```

---

### Task 0.6: Reconcile-disk-workspaces test

**Files:**
- Create: `tests/unit/test_orchestrator_reconcile.py`

- [ ] **Step 1: Write the test**

```python
"""Characterization test for _reconcile_disk_workspaces."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.orchestrator import Orchestrator


def test_reconcile_adopts_disk_workspaces() -> None:
    """A workspace on disk but absent from _active_workspaces gets re-adopted."""
    orc = Orchestrator.__new__(Orchestrator)
    on_disk_ws = SimpleNamespace(state=SimpleNamespace(
        ticket_id="T-99", company_id="acme", current_state="dev",
    ))
    wm = MagicMock()
    wm.discover_workspaces.return_value = [on_disk_ws]
    orc._workspace_manager = wm
    orc._active_workspaces = []
    orc._events = None

    orc._reconcile_disk_workspaces()

    assert any(
        ws.state.ticket_id == "T-99" for ws in orc._active_workspaces
    )


def test_reconcile_no_duplicate_on_second_call() -> None:
    """A workspace already in _active_workspaces is not duplicated."""
    orc = Orchestrator.__new__(Orchestrator)
    ws = SimpleNamespace(state=SimpleNamespace(
        ticket_id="T-1", company_id="acme", current_state="dev",
    ))
    wm = MagicMock()
    wm.discover_workspaces.return_value = [ws]
    orc._workspace_manager = wm
    orc._active_workspaces = [ws]
    orc._events = None

    orc._reconcile_disk_workspaces()

    assert len([w for w in orc._active_workspaces if w.state.ticket_id == "T-1"]) == 1
```

- [ ] **Step 2: Run and verify**

```
pytest tests/unit/test_orchestrator_reconcile.py -v
```
Expected: 2 passed.

> If `_reconcile_disk_workspaces` requires more setup than this (the current method may have specific expectations about workspace state), inspect orchestrator.py:1179 and adjust the test mocks to match — the goal is *pin current behavior*, even if minimal.

- [ ] **Step 3: Commit**

```
git add tests/unit/test_orchestrator_reconcile.py
git commit -m "test(orchestrator): characterize _reconcile_disk_workspaces"
```

---

### Task 0.7: Notification helpers tests

**Files:**
- Create: `tests/unit/test_orchestrator_notify_misc.py`

- [ ] **Step 1: Write the tests**

```python
"""Characterization tests for _notify_rerun and _notify_verification_blocked.

These methods produce operator-visible TG messages. Pin the chat_id, message
shape (key phrases), and that buttons (when present) survive.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.orchestrator import Orchestrator


def _orc(notifier):
    orc = Orchestrator.__new__(Orchestrator)
    orc._notifier = notifier
    orc._projects = {}
    orc._global_config = SimpleNamespace(
        telegram=SimpleNamespace(default_chat_id="chat-1"),
    )
    orc._events = None
    return orc


def _ws():
    ws = MagicMock()
    ws.state = SimpleNamespace(
        ticket_id="T-1", company_id="acme", current_state="dev",
    )
    ws.meta_dir = MagicMock()
    return ws


@pytest.mark.asyncio
async def test_notify_rerun_sends_message(monkeypatch) -> None:
    notifier = AsyncMock()
    orc = _orc(notifier)
    monkeypatch.setattr(
        "orchestrator.tg_format.read_ticket_title", lambda w: "title",
    )
    await orc._notify_rerun(_ws(), reason="manual rerun")
    assert notifier.send_message.await_count == 1
    msg = notifier.send_message.await_args.args[1]
    assert "manual rerun" in msg.lower() or "rerun" in msg.lower()


@pytest.mark.asyncio
async def test_notify_verification_blocked_sends_message(monkeypatch) -> None:
    notifier = AsyncMock()
    orc = _orc(notifier)
    monkeypatch.setattr(
        "orchestrator.tg_format.read_ticket_title", lambda w: "title",
    )
    await orc._notify_verification_blocked(
        _ws(), stage_id="qa", reason="no new commits",
    )
    assert notifier.send_message.await_count == 1
    msg = notifier.send_message.await_args.args[1]
    assert "no new commits" in msg or "qa" in msg.lower()
```

> If a method signature differs from this assumed shape (e.g. `_notify_rerun` requires different args), read orchestrator.py:1157 and orchestrator.py:2464 and align the test calls.

- [ ] **Step 2: Run and verify**

```
pytest tests/unit/test_orchestrator_notify_misc.py -v
```
Expected: 2 passed.

- [ ] **Step 3: Commit**

```
git add tests/unit/test_orchestrator_notify_misc.py
git commit -m "test(orchestrator): characterize misc notification helpers"
```

---

### Task 0.8: Phase-0 gate

- [ ] **Step 1: Run the full unit suite and confirm everything is still green**

```
pytest tests/unit/ -x
```
Expected: PASS for all (existing + new characterization tests).

- [ ] **Step 2: Commit gate**

If the suite is green there is nothing to commit; this step is a checkpoint. If anything is red, do NOT proceed to Phase A — fix the test or update the assertion to match current behavior, then re-run.

---

## Phase A: Tracker port expansion (additive)

### Task A.1: Add `TicketComment` and `StatusChange` dataclasses + new abstract methods

**Files:**
- Modify: `integrations/base/tracker.py`

- [ ] **Step 1: Write a failing test**

```python
# tests/unit/test_tracker_interface.py
"""Verify the tracker port exposes the new comment/history/attachment methods."""
import inspect
from integrations.base.tracker import TicketComment, StatusChange, TrackerInterface


def test_new_dataclasses_present() -> None:
    c = TicketComment(id="1", author="a", created="2026-05-11", body="hi")
    assert c.body == "hi"
    s = StatusChange(
        created="2026-05-11", from_status="A", to_status="B", author="a",
    )
    assert s.to_status == "B"


def test_new_abstract_methods_declared() -> None:
    abstract = TrackerInterface.__abstractmethods__
    for name in (
        "get_comments", "get_status_history",
        "download_attachment", "list_transitions",
    ):
        assert name in abstract, f"missing abstract method: {name}"


def test_methods_are_async() -> None:
    for name in (
        "get_comments", "get_status_history",
        "download_attachment", "list_transitions",
    ):
        method = getattr(TrackerInterface, name)
        assert inspect.iscoroutinefunction(method), f"{name} must be async"
```

- [ ] **Step 2: Run, expect failure**

```
pytest tests/unit/test_tracker_interface.py -v
```
Expected: ImportError on `TicketComment` and `StatusChange`.

- [ ] **Step 3: Add to `integrations/base/tracker.py`**

Append after the existing `TicketData` dataclass (after line 26):

```python
@dataclass
class TicketComment:
    """A comment on a ticket — provider-neutral."""
    id: str
    author: str
    created: str        # ISO date "YYYY-MM-DD" suffices
    body: str           # plain text; adapter strips formatting markup


@dataclass
class StatusChange:
    """One step in a ticket's status history."""
    created: str
    from_status: str
    to_status: str
    author: str
```

In `class TrackerInterface(ABC):` block, after the existing 4 methods, add:

```python
    @abstractmethod
    async def get_comments(self, ticket_id: str) -> list[TicketComment]:
        """Return all comments on a ticket, oldest first."""

    @abstractmethod
    async def get_status_history(self, ticket_id: str) -> list[StatusChange]:
        """Return the status-transition history of a ticket, oldest first."""

    @abstractmethod
    async def download_attachment(self, url: str) -> bytes:
        """Fetch an attachment's bytes. Adapter owns its auth headers."""

    @abstractmethod
    async def list_transitions(self, ticket_id: str) -> list[str]:
        """Return human-readable names of currently available transitions/lists."""
```

- [ ] **Step 4: Run, expect pass**

```
pytest tests/unit/test_tracker_interface.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```
git add integrations/base/tracker.py tests/unit/test_tracker_interface.py
git commit -m "refactor(tracker): add TicketComment, StatusChange, 4 new abstract methods"
```

---

### Task A.2: Implement new methods on `JiraAdapter` — `get_comments`

**Files:**
- Modify: `integrations/jira/jira_adapter.py`

- [ ] **Step 1: Write a failing test**

```python
# tests/integration/test_jira_adapter.py — APPEND
import pytest
from unittest.mock import AsyncMock, patch

from integrations.base.tracker import TicketComment
from integrations.jira.jira_adapter import JiraAdapter


@pytest.mark.asyncio
async def test_get_comments_returns_ticketcomment_list() -> None:
    adapter = JiraAdapter(
        url="https://x", email="e", token="t", project_key="P",
    )
    raw = {
        "fields": {
            "comment": {
                "comments": [
                    {
                        "id": "1001",
                        "author": {"displayName": "Alice"},
                        "created": "2026-05-10T12:00:00.000+0000",
                        "body": "first comment",
                    },
                    {
                        "id": "1002",
                        "author": {"displayName": "Bob"},
                        "created": "2026-05-11T09:00:00.000+0000",
                        "body": {
                            "type": "doc", "version": 1,
                            "content": [{"type": "paragraph", "content": [
                                {"type": "text", "text": "ADF comment"},
                            ]}],
                        },
                    },
                ],
            },
        },
    }
    with patch.object(adapter, "_request", AsyncMock(return_value=raw)):
        comments = await adapter.get_comments("PROJ-1")

    assert len(comments) == 2
    assert isinstance(comments[0], TicketComment)
    assert comments[0].id == "1001"
    assert comments[0].author == "Alice"
    assert comments[0].body == "first comment"
    assert comments[0].created == "2026-05-10"
    # ADF body must be stripped to plain text
    assert comments[1].body == "ADF comment"
```

- [ ] **Step 2: Run, expect failure**

```
pytest tests/integration/test_jira_adapter.py::test_get_comments_returns_ticketcomment_list -v
```
Expected: AttributeError — `get_comments` not implemented.

- [ ] **Step 3: Implement in `JiraAdapter`**

Add after `add_comment` (around line 232):

```python
    async def get_comments(self, ticket_id: str) -> list[TicketComment]:
        """Fetch all comments on a ticket. ADF bodies are stripped to plain text."""
        data = await self._request(
            "GET", f"/issue/{ticket_id}?expand=changelog&fields=comment",
        )
        raw_comments = (
            data.get("fields", {}).get("comment", {}).get("comments", []) or []
        )
        out: list[TicketComment] = []
        for c in raw_comments:
            body = c.get("body", "")
            if isinstance(body, dict):
                body = _extract_adf_text(body)
            out.append(TicketComment(
                id=str(c.get("id", "")),
                author=(c.get("author") or {}).get("displayName", "?"),
                created=(c.get("created") or "")[:10],  # ISO date only
                body=str(body),
            ))
        return out
```

Update the top-of-file imports:
```python
from integrations.base.tracker import (
    TicketComment, TicketData, TrackerInterface,
)
```

- [ ] **Step 4: Run, expect pass**

```
pytest tests/integration/test_jira_adapter.py::test_get_comments_returns_ticketcomment_list -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add integrations/jira/jira_adapter.py tests/integration/test_jira_adapter.py
git commit -m "feat(jira): implement get_comments on JiraAdapter"
```

---

### Task A.3: Implement `get_status_history` on `JiraAdapter`

**Files:**
- Modify: `integrations/jira/jira_adapter.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_jira_adapter.py — APPEND
from integrations.base.tracker import StatusChange


@pytest.mark.asyncio
async def test_get_status_history_returns_status_changes() -> None:
    adapter = JiraAdapter(url="https://x", email="e", token="t", project_key="P")
    raw = {
        "changelog": {
            "histories": [
                {
                    "created": "2026-05-10T08:00:00.000+0000",
                    "author": {"displayName": "Alice"},
                    "items": [
                        {"field": "status", "fromString": "To Do",
                         "toString": "In Progress"},
                        {"field": "labels", "fromString": "", "toString": "x"},
                    ],
                },
                {
                    "created": "2026-05-11T15:00:00.000+0000",
                    "author": {"displayName": "Bob"},
                    "items": [
                        {"field": "status", "fromString": "In Progress",
                         "toString": "In Review"},
                    ],
                },
            ],
        },
    }
    with patch.object(adapter, "_request", AsyncMock(return_value=raw)):
        history = await adapter.get_status_history("PROJ-1")

    assert len(history) == 2  # non-status items skipped
    assert all(isinstance(h, StatusChange) for h in history)
    assert history[0].from_status == "To Do"
    assert history[0].to_status == "In Progress"
    assert history[0].created == "2026-05-10"
    assert history[1].to_status == "In Review"
```

- [ ] **Step 2: Run, expect failure**

```
pytest tests/integration/test_jira_adapter.py::test_get_status_history_returns_status_changes -v
```

- [ ] **Step 3: Implement in `JiraAdapter`**

```python
    async def get_status_history(self, ticket_id: str) -> list[StatusChange]:
        """Walk Jira's changelog and return only status transitions."""
        data = await self._request(
            "GET", f"/issue/{ticket_id}?expand=changelog&fields=comment",
        )
        histories = data.get("changelog", {}).get("histories", []) or []
        out: list[StatusChange] = []
        for h in histories:
            created = (h.get("created") or "")[:10]
            author = (h.get("author") or {}).get("displayName", "?")
            for item in h.get("items", []) or []:
                if item.get("field") != "status":
                    continue
                out.append(StatusChange(
                    created=created,
                    from_status=item.get("fromString", "?") or "?",
                    to_status=item.get("toString", "?") or "?",
                    author=author,
                ))
        return out
```

Update imports to include `StatusChange`.

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

```
git add integrations/jira/jira_adapter.py tests/integration/test_jira_adapter.py
git commit -m "feat(jira): implement get_status_history on JiraAdapter"
```

---

### Task A.4: Implement `download_attachment` on `JiraAdapter`

**Files:**
- Modify: `integrations/jira/jira_adapter.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_jira_adapter.py — APPEND
@pytest.mark.asyncio
async def test_download_attachment_returns_bytes(monkeypatch) -> None:
    adapter = JiraAdapter(url="https://x", email="e", token="t", project_key="P")
    response = MagicMock()
    response.status_code = 200
    response.content = b"file bytes"

    class DummyClient:
        def __init__(self, *a, **kw): ...
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url, headers=None, follow_redirects=False):
            return response

    monkeypatch.setattr("integrations.jira.jira_adapter.httpx.AsyncClient", DummyClient)
    data = await adapter.download_attachment("https://x/file")
    assert data == b"file bytes"
```

Add `from unittest.mock import MagicMock` to imports if missing.

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement in `JiraAdapter`**

```python
    async def download_attachment(self, url: str) -> bytes:
        """Fetch an attachment. Uses adapter-owned basic-auth credentials."""
        import base64
        creds = base64.b64encode(
            f"{self._email}:{self._token}".encode()
        ).decode()
        headers = {"Authorization": f"Basic {creds}"}
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(url, headers=headers, follow_redirects=True)
            if resp.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"attachment fetch HTTP {resp.status_code}",
                    request=resp.request, response=resp,
                )
            return resp.content
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

```
git add integrations/jira/jira_adapter.py tests/integration/test_jira_adapter.py
git commit -m "feat(jira): implement download_attachment on JiraAdapter"
```

---

### Task A.5: Implement `list_transitions` on `JiraAdapter`

**Files:**
- Modify: `integrations/jira/jira_adapter.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_jira_adapter.py — APPEND
@pytest.mark.asyncio
async def test_list_transitions_returns_to_names() -> None:
    adapter = JiraAdapter(url="https://x", email="e", token="t", project_key="P")
    raw = {"transitions": [
        {"id": "21", "name": "Start Review", "to": {"name": "In Review"}},
        {"id": "31", "name": "Send Back",    "to": {"name": "To Do"}},
        {"id": "41", "name": "Close",        "to": {"name": ""}},  # falls back to name
    ]}
    with patch.object(adapter, "_request", AsyncMock(return_value=raw)):
        names = await adapter.list_transitions("PROJ-1")
    assert names == ["In Review", "To Do", "Close"]
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement in `JiraAdapter`**

```python
    async def list_transitions(self, ticket_id: str) -> list[str]:
        """Return human-readable target names of currently available transitions.

        For each transition Jira returns both an action name (e.g. "Start
        Review") and a target state (`to.name`, e.g. "In Review"). Prefer the
        target name; fall back to the action name when the target is empty.
        """
        data = await self._request("GET", f"/issue/{ticket_id}/transitions")
        names: list[str] = []
        for t in data.get("transitions", []) or []:
            to_name = (t.get("to") or {}).get("name", "") or ""
            names.append(to_name or t.get("name", ""))
        return names
```

- [ ] **Step 4: Run, expect pass; full Jira adapter test file**

```
pytest tests/integration/test_jira_adapter.py -v
```
Expected: all new tests pass plus existing.

- [ ] **Step 5: Commit**

```
git add integrations/jira/jira_adapter.py tests/integration/test_jira_adapter.py
git commit -m "feat(jira): implement list_transitions on JiraAdapter"
```

---

### Task A.6: Move `_extract_adf_text` import from orchestrator

The lazy `from integrations.jira.jira_adapter import _extract_adf_text` inside `_refetch_ticket_data` (line 598) is now dead — `get_comments` returns plain-text bodies. Keep the helper in `jira_adapter.py` (still used by `_parse_ticket`); the orchestrator's import goes away with the call site in Task B.1.

No changes in this task — it's a note. Proceed to Phase B.

---

## Phase B: Cut orchestrator's direct `_request` / `_email` / `_token` access

### Task B.1: Replace `_refetch_ticket_data` comments + history block

**Files:**
- Modify: `orchestrator/orchestrator.py`

- [ ] **Step 1: Read the current block**

`orchestrator/orchestrator.py:569–653` (the `if self._tracker and hasattr(self._tracker, "_request"):` block).

- [ ] **Step 2: Replace with new tracker API**

Find:
```python
        if self._tracker and hasattr(self._tracker, "_request"):
            try:
                data = await self._tracker._request(
                    "GET", f"/issue/{ticket_id}?expand=changelog&fields=comment",
                )

                # Comments — write fresh on first run, append only new ones on rerun
                comments = (
                    data.get("fields", {}).get("comment", {}).get("comments", [])
                )
                if comments:
                    comments_file = workspace.meta_dir / "comments.md"
                    existing_ids: set[str] = set()
                    if comments_file.exists():
                        existing_ids = set(
                            re.findall(
                                r"<!-- comment:(\S+) -->",
                                comments_file.read_text(encoding="utf-8"),
                            )
                        )
                    new_lines: list[str] = []
                    for c in comments:
                        cid = str(c.get("id", ""))
                        if cid and cid in existing_ids:
                            continue
                        author = c.get("author", {}).get("displayName", "?")
                        created = c.get("created", "")[:10]
                        body = c.get("body", "")
                        if isinstance(body, dict):
                            from integrations.jira.jira_adapter import _extract_adf_text
                            body = _extract_adf_text(body)
                        marker = f"<!-- comment:{cid} -->\n" if cid else ""
                        new_lines.append(
                            f"{marker}## {author} ({created})\n\n{body}\n"
                        )
                    if new_lines:
                        block = "\n".join(new_lines)
                        if comments_file.exists():
                            comments_file.write_text(
                                comments_file.read_text(encoding="utf-8")
                                + "\n"
                                + block,
                                encoding="utf-8",
                            )
                        else:
                            comments_file.write_text(
                                "# Jira Comments\n\n" + block, encoding="utf-8"
                            )

                # History — write fresh on first run, append only new lines on rerun
                changelog = data.get("changelog", {}).get("histories", [])
                history_file = workspace.meta_dir / "history.md"
                existing_history = (
                    history_file.read_text(encoding="utf-8")
                    if history_file.exists()
                    else ""
                )
                new_changes: list[str] = []
                for h in changelog:
                    for item in h.get("items", []):
                        if item.get("field") == "status":
                            line = (
                                f"- {h.get('created', '')[:10]}: "
                                f"{item.get('fromString', '?')} → "
                                f"{item.get('toString', '?')} "
                                f"by {h.get('author', {}).get('displayName', '?')}"
                            )
                            if line not in existing_history:
                                new_changes.append(line)
                if new_changes:
                    new_block = "\n".join(new_changes) + "\n"
                    if history_file.exists():
                        history_file.write_text(
                            existing_history.rstrip() + "\n" + new_block,
                            encoding="utf-8",
                        )
                    else:
                        history_file.write_text(
                            "# Status History\n\n" + new_block, encoding="utf-8"
                        )

            except Exception as e:
                logger.warning(
                    "Failed to refetch comments/history for %s: %s", ticket_id, e
                )
```

Replace with:
```python
        if self._tracker:
            try:
                comments = await self._tracker.get_comments(ticket_id)
                if comments:
                    comments_file = workspace.meta_dir / "comments.md"
                    existing_ids: set[str] = set()
                    if comments_file.exists():
                        existing_ids = set(
                            re.findall(
                                r"<!-- comment:(\S+) -->",
                                comments_file.read_text(encoding="utf-8"),
                            )
                        )
                    new_lines: list[str] = []
                    for c in comments:
                        if c.id and c.id in existing_ids:
                            continue
                        marker = f"<!-- comment:{c.id} -->\n" if c.id else ""
                        new_lines.append(
                            f"{marker}## {c.author} ({c.created})\n\n{c.body}\n"
                        )
                    if new_lines:
                        block = "\n".join(new_lines)
                        if comments_file.exists():
                            comments_file.write_text(
                                comments_file.read_text(encoding="utf-8")
                                + "\n" + block,
                                encoding="utf-8",
                            )
                        else:
                            comments_file.write_text(
                                "# Ticket Comments\n\n" + block, encoding="utf-8",
                            )

                history = await self._tracker.get_status_history(ticket_id)
                history_file = workspace.meta_dir / "history.md"
                existing_history = (
                    history_file.read_text(encoding="utf-8")
                    if history_file.exists() else ""
                )
                new_changes: list[str] = []
                for h in history:
                    line = (
                        f"- {h.created}: {h.from_status} → "
                        f"{h.to_status} by {h.author}"
                    )
                    if line not in existing_history:
                        new_changes.append(line)
                if new_changes:
                    new_block = "\n".join(new_changes) + "\n"
                    if history_file.exists():
                        history_file.write_text(
                            existing_history.rstrip() + "\n" + new_block,
                            encoding="utf-8",
                        )
                    else:
                        history_file.write_text(
                            "# Status History\n\n" + new_block, encoding="utf-8",
                        )

            except Exception as e:
                logger.warning(
                    "Failed to refetch comments/history for %s: %s", ticket_id, e,
                )
```

Note the heading change: "# Jira Comments" → "# Ticket Comments". The old heading already exists in some workspaces; the append path keeps writing under whatever heading already exists, so this rename only affects newly-created files. **This is the one deliberate copy change** in the entire refactor; flag it as such in the commit message.

- [ ] **Step 3: Run characterization tests**

```
pytest tests/unit/test_orchestrator_refetch.py -v
```
Expected: existing tests for `_refetch_ticket_data` still pass (they test file shape, not the exact heading string).

If any existing test asserts `"# Jira Comments"` literally, update it to assert `"Comments"` (substring) and add a comment noting the rename.

- [ ] **Step 4: Commit**

```
git add orchestrator/orchestrator.py tests/unit/test_orchestrator_refetch.py
git commit -m "refactor(orchestrator): use tracker.get_comments/get_status_history (deprovincialize heading)"
```

---

### Task B.2: Replace `_refetch_ticket_data` attachment-download block

**Files:**
- Modify: `orchestrator/orchestrator.py`

- [ ] **Step 1: Find the block at line 658–691**

```python
        if self._tracker and ticket_obj is not None and ticket_obj.attachments:
            attachments_dir = workspace.meta_dir / "attachments"
            attachments_dir.mkdir(exist_ok=True)
            for att in ticket_obj.attachments:
                filename = att.get("filename", "attachment")
                mime = att.get("mime_type", "")
                if not _attachment_is_keepable(filename, mime):
                    continue
                if (attachments_dir / filename).exists():
                    continue  # already downloaded, skip
                try:
                    import httpx
                    headers = {}
                    if hasattr(self._tracker, '_email') and hasattr(self._tracker, '_token'):
                        import base64
                        creds = base64.b64encode(
                            f"{self._tracker._email}:{self._tracker._token}".encode()
                        ).decode()
                        headers = {"Authorization": f"Basic {creds}"}
                    async with httpx.AsyncClient(timeout=30) as client:
                        resp = await client.get(att["url"], headers=headers, follow_redirects=True)
                        if resp.status_code != 200:
                            logger.warning("Failed to download %s: HTTP %d", filename, resp.status_code)
                            continue
                        if len(resp.content) > _MAX_ATTACHMENT_BYTES:
                            logger.info(
                                "Skipping oversized attachment %s (%d bytes > %d)",
                                filename, len(resp.content), _MAX_ATTACHMENT_BYTES,
                            )
                            continue
                        (attachments_dir / filename).write_bytes(resp.content)
                        logger.info("Downloaded attachment %s for %s", filename, ticket_id)
                except Exception as e:
                    logger.warning("Failed to download attachment %s: %s", filename, e)
```

- [ ] **Step 2: Replace with tracker.download_attachment call**

```python
        if self._tracker and ticket_obj is not None and ticket_obj.attachments:
            attachments_dir = workspace.meta_dir / "attachments"
            attachments_dir.mkdir(exist_ok=True)
            for att in ticket_obj.attachments:
                filename = att.get("filename", "attachment")
                mime = att.get("mime_type", "")
                if not _attachment_is_keepable(filename, mime):
                    continue
                if (attachments_dir / filename).exists():
                    continue  # already downloaded, skip
                try:
                    content = await self._tracker.download_attachment(att["url"])
                    if len(content) > _MAX_ATTACHMENT_BYTES:
                        logger.info(
                            "Skipping oversized attachment %s (%d bytes > %d)",
                            filename, len(content), _MAX_ATTACHMENT_BYTES,
                        )
                        continue
                    (attachments_dir / filename).write_bytes(content)
                    logger.info("Downloaded attachment %s for %s", filename, ticket_id)
                except Exception as e:
                    logger.warning(
                        "Failed to download attachment %s: %s", filename, e,
                    )
```

- [ ] **Step 3: Run tests**

```
pytest tests/unit/test_orchestrator_refetch.py -v
```

- [ ] **Step 4: Commit**

```
git add orchestrator/orchestrator.py
git commit -m "refactor(orchestrator): use tracker.download_attachment (drop httpx leak)"
```

---

### Task B.3: Replace `_on_ticket_done` fuzzy-transition block

**Files:**
- Modify: `orchestrator/orchestrator.py`

- [ ] **Step 1: Find the block at orchestrator.py:2261–2308**

The block beginning with `if self._tracker:` inside `_on_ticket_done`.

- [ ] **Step 2: Replace with new tracker API**

```python
        # Transition tracker ticket to in-review status (Jira: "In Review",
        # Trello: a list-name match). Fuzzy keywords are pipeline policy and
        # stay on this side of the port.
        if self._tracker:
            project = self._projects.get(state.company_id)
            if project:
                target_status = project.config.jira.statuses.in_review
                if target_status:
                    try:
                        available = await self._tracker.list_transitions(
                            state.ticket_id,
                        )
                        matched = None
                        target_lower = target_status.lower()
                        for name in available:
                            if target_lower in name.lower():
                                matched = name
                                break
                        if matched is None:
                            for name in available:
                                if any(kw in name.lower() for kw in (
                                    "review", "qa", "verification", "ready for qa",
                                )):
                                    matched = name
                                    break
                        if matched is not None:
                            await self._tracker.transition_ticket(
                                state.ticket_id, matched,
                            )
                            logger.info(
                                "Transitioned %s to '%s'", state.ticket_id, matched,
                            )
                        else:
                            logger.warning(
                                "Cannot transition %s to '%s' — available: %s",
                                state.ticket_id, target_status, available,
                            )
                    except Exception as e:
                        logger.warning(
                            "Failed to transition %s on tracker: %s",
                            state.ticket_id, e,
                        )
```

- [ ] **Step 3: Update `tests/unit/test_orchestrator_finalize.py`**

Replace the `tracker._request.side_effect = [...]` setups with mocks of the new public methods:

```python
@pytest.mark.asyncio
async def test_exact_match_transition_fires() -> None:
    tracker = AsyncMock()
    tracker.list_transitions.return_value = ["In Review"]
    orc = _orc(tracker, _project("In Review"))
    await orc._on_ticket_done(_ws())
    tracker.transition_ticket.assert_awaited_once()
    assert tracker.transition_ticket.await_args.args[1] == "In Review"


@pytest.mark.asyncio
async def test_fuzzy_match_on_review_keyword() -> None:
    tracker = AsyncMock()
    tracker.list_transitions.return_value = ["Reviewing"]
    orc = _orc(tracker, _project("Nonexistent Status"))
    await orc._on_ticket_done(_ws())
    tracker.transition_ticket.assert_awaited_once()
    assert tracker.transition_ticket.await_args.args[1] == "Reviewing"


@pytest.mark.asyncio
async def test_no_matching_transition_does_nothing_fatal() -> None:
    tracker = AsyncMock()
    tracker.list_transitions.return_value = ["Closed"]
    orc = _orc(tracker, _project("In Review"))
    await orc._on_ticket_done(_ws())
    tracker.transition_ticket.assert_not_awaited()
```

- [ ] **Step 4: Run, expect pass**

```
pytest tests/unit/test_orchestrator_finalize.py -v
```

- [ ] **Step 5: Verify no `_request` / `_email` / `_token` left in orchestrator**

```
grep -n "_tracker\._request\|_tracker\._email\|_tracker\._token" orchestrator/orchestrator.py
```
Expected: no output.

- [ ] **Step 6: Commit**

```
git add orchestrator/orchestrator.py tests/unit/test_orchestrator_finalize.py
git commit -m "refactor(orchestrator): use tracker.list_transitions/transition_ticket in _on_ticket_done"
```

---

## Phase C: Config schema tightening

### Task C.1: Hoist `default_branch` / `branch_prefix` to `VCSConfig`

**Files:**
- Modify: `config/schemas.py`, `orchestrator/orchestrator.py`

- [ ] **Step 1: Edit `config/schemas.py`**

Find:
```python
@dataclass
class VCSConfig:
    """VCS provider selection. Only one sub-config is used based on provider."""
    provider: str = "github"  # "github" or "gitlab"
    github: GitHubConfig = field(default_factory=GitHubConfig)
    gitlab: GitLabConfig = field(default_factory=GitLabConfig)
    skip_pre_push_hook: bool = False
```

Replace with:
```python
@dataclass
class VCSConfig:
    """VCS provider selection. Only one sub-config is used based on provider.

    `default_branch` and `branch_prefix` are shared across all providers; the
    per-provider sub-configs keep them for backward compatibility with
    existing config-live/ files but should not be the primary source.
    """
    provider: str = "github"  # "github" or "gitlab"
    default_branch: str = "develop"
    branch_prefix: str = "feature"
    github: GitHubConfig = field(default_factory=GitHubConfig)
    gitlab: GitLabConfig = field(default_factory=GitLabConfig)
    skip_pre_push_hook: bool = False
```

- [ ] **Step 2: Edit `config/config_loader.py`**

Find where `VCSConfig` is constructed (search `VCSConfig(`). If config-live YAML files set `vcs.github.default_branch`, the loader should also copy that to the parent `vcs.default_branch` when the parent is empty. Add this fallback after construction:

```python
# Backward compat: if hoisted fields are blank but the provider sub-config
# has them set, copy up.
if not vcs.default_branch:
    sub = vcs.github if vcs.provider == "github" else vcs.gitlab
    vcs.default_branch = sub.default_branch or "develop"
if not vcs.branch_prefix:
    sub = vcs.github if vcs.provider == "github" else vcs.gitlab
    vcs.branch_prefix = sub.branch_prefix or "feature"
```

- [ ] **Step 3: Remove the provider ternaries in `_create_workspace_for_ticket`**

`orchestrator/orchestrator.py:706–711`. Replace:

```python
            default_branch=repo_config.vcs.github.default_branch
            if repo_config.vcs.provider == "github"
            else repo_config.vcs.gitlab.default_branch,
            branch_prefix=repo_config.vcs.github.branch_prefix
            if repo_config.vcs.provider == "github"
            else repo_config.vcs.gitlab.branch_prefix,
```

With:

```python
            default_branch=repo_config.vcs.default_branch,
            branch_prefix=repo_config.vcs.branch_prefix,
```

- [ ] **Step 4: Run tests**

```
pytest tests/ -k "config or schema or workspace_manager" -v
pytest tests/integration/test_project_create_flow.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add config/schemas.py config/config_loader.py orchestrator/orchestrator.py
git commit -m "refactor(config): hoist default_branch/branch_prefix to VCSConfig"
```

---

### Task C.2: Rename `jira_repo_label` → `tracker_label` with alias

**Files:**
- Modify: `config/schemas.py`, `config/config_loader.py`, `orchestrator/orchestrator.py`

- [ ] **Step 1: Edit `config/schemas.py`**

Find:
```python
    jira_repo_label: str = ""
```

Replace with:
```python
    tracker_label: str = ""
    # Deprecated alias — set by loader from `jira_repo_label`. Out of scope to
    # remove until all live config files have migrated.
    jira_repo_label: str = ""
```

- [ ] **Step 2: Edit `config/config_loader.py`**

After `RepoConfig` is constructed, add:

```python
# Backward-compat: accept the old `jira_repo_label` key.
if repo_cfg.jira_repo_label and not repo_cfg.tracker_label:
    repo_cfg.tracker_label = repo_cfg.jira_repo_label
```

- [ ] **Step 3: Update `_route_manual_ticket` in `orchestrator.py:259–267`**

Replace:
```python
                if repo_config.jira_repo_label and repo_config.jira_repo_label in ticket.labels:
```
With:
```python
                if repo_config.tracker_label and repo_config.tracker_label in ticket.labels:
```

Search the rest of `orchestrator.py` for other uses (`grep -n "jira_repo_label" orchestrator/orchestrator.py`); replace each, keeping behavior identical.

Also check `orchestrator/ticket_prioritizer.py` (routing logic referenced from the orchestrator) — if it accesses `jira_repo_label`, update to `tracker_label` and re-run prioritizer tests.

```
grep -rn "jira_repo_label" orchestrator/
```

- [ ] **Step 4: Run tests**

```
pytest tests/unit/test_ticket_prioritizer.py -v
pytest tests/unit/test_orchestrator_poll_create.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add config/schemas.py config/config_loader.py orchestrator/ticket_prioritizer.py orchestrator/orchestrator.py
git commit -m "refactor(config): rename jira_repo_label -> tracker_label (with backward-compat alias)"
```

---

## Phase D: Extract leaf modules

> **Pattern for every "extract" task:** copy the function body into a new module as a top-level `async def` (or `def`) with explicit parameters replacing each `self.X`. Inside `Orchestrator`, keep the method as a one-line shim that calls the new function. The shim disappears in Phase G.

### Task D.1: Extract `git_ops`

**Files:**
- Create: `orchestrator/git_ops.py`
- Modify: `orchestrator/orchestrator.py`

- [ ] **Step 1: Create `orchestrator/git_ops.py`**

```python
"""Git helper functions used across the pipeline.

These were previously @staticmethod helpers on the Orchestrator class. They
take a Workspace because they operate on workspace.source_dir.
"""
from __future__ import annotations

import subprocess

from workspace.workspace import Workspace


def git_diff_files(workspace: Workspace, since_sha: str = "") -> set[str]:
    """Return the set of files changed in `<since_sha>..HEAD` (or HEAD~1)."""
    diff_arg = f"{since_sha}..HEAD" if since_sha else "HEAD~1"
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace.source_dir),
             "diff", diff_arg, "--name-only"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return set(result.stdout.strip().splitlines())
    except Exception:
        pass
    return set()


def git_head_sha(workspace: Workspace) -> str:
    """Return the current HEAD sha, or 'unknown' on error."""
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace.source_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"
```

- [ ] **Step 2: Replace methods in `orchestrator/orchestrator.py`**

Find `_git_diff_files` and `_git_head_sha` (lines 312–344) and replace with thin shims:

```python
    @staticmethod
    def _git_diff_files(workspace: Workspace, since_sha: str = "") -> set[str]:
        from orchestrator.git_ops import git_diff_files
        return git_diff_files(workspace, since_sha)

    @staticmethod
    def _git_head_sha(workspace: Workspace) -> str:
        from orchestrator.git_ops import git_head_sha
        return git_head_sha(workspace)
```

- [ ] **Step 3: Run tests**

```
pytest tests/unit/test_orchestrator_squash.py tests/unit/test_orchestrator_stage_verify.py -v
```
Expected: PASS.

- [ ] **Step 4: Commit**

```
git add orchestrator/git_ops.py orchestrator/orchestrator.py
git commit -m "refactor(orchestrator): extract git_ops module"
```

---

### Task D.2: Extract `ticket_sync`

**Files:**
- Create: `orchestrator/ticket_sync.py`
- Modify: `orchestrator/orchestrator.py`

- [ ] **Step 1: Create `orchestrator/ticket_sync.py`**

Move three things into this file:
1. `_attachment_is_keepable` (orchestrator.py:2720)
2. `_ticket_to_markdown` (orchestrator.py:2746)
3. `_refetch_ticket_data` (orchestrator.py:540, now using new tracker API after Phase B)
4. The constants `_MAX_ATTACHMENT_BYTES` and `_TEXT_ATTACHMENT_EXTS`

```python
"""Keep ticket metadata files (ticket.md, comments.md, history.md, attachments) in sync.

Provider-neutral: uses TrackerInterface — no Jira/Trello-specific code lives here.
"""
from __future__ import annotations

import logging
import re

from integrations.base.tracker import TicketData, TrackerInterface
from workspace.workspace import Workspace

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 1_000_000

_TEXT_ATTACHMENT_EXTS = {
    ".txt", ".log", ".json", ".xml", ".yaml", ".yml", ".csv", ".tsv",
    ".md", ".html", ".htm", ".css",
    ".kt", ".kts", ".java", ".swift", ".m", ".mm", ".h", ".c", ".cc", ".cpp",
    ".py", ".rb", ".go", ".rs", ".js", ".jsx", ".ts", ".tsx",
    ".gradle", ".properties", ".toml", ".ini", ".conf", ".sh",
    ".stacktrace", ".trace", ".diff", ".patch",
}


def attachment_is_keepable(filename: str, mime: str) -> bool:
    """Decide whether to download an attachment for analysis."""
    mime = (mime or "").lower()
    if mime.startswith(("video/", "audio/")):
        return False
    if mime.startswith(("text/", "image/")):
        return True
    if mime in {
        "application/json", "application/xml",
        "application/yaml", "application/x-yaml",
        "application/javascript", "application/x-shellscript",
    }:
        return True
    ext = ""
    dot = filename.rfind(".")
    if dot >= 0:
        ext = filename[dot:].lower()
    return ext in _TEXT_ATTACHMENT_EXTS


def ticket_to_markdown(ticket: TicketData) -> str:
    """Convert TicketData to a markdown document."""
    lines = [
        f"# {ticket.id}: {ticket.summary}", "",
        f"**URL:** {ticket.url}",
        f"**Priority:** {ticket.priority}",
        f"**Reporter:** {ticket.reporter}",
    ]
    if ticket.assignee:
        lines.append(f"**Assignee:** {ticket.assignee}")
    if ticket.sprint:
        lines.append(f"**Sprint:** {ticket.sprint}")
    if ticket.labels:
        lines.append(f"**Labels:** {', '.join(ticket.labels)}")
    lines.extend(["", "## Description", "", ticket.description])
    if ticket.acceptance_criteria:
        lines.extend(["", "## Acceptance Criteria", "", ticket.acceptance_criteria])
    if ticket.linked_issues:
        lines.extend(["", "## Linked Issues", ""])
        for link in ticket.linked_issues:
            lines.append(f"- {link.get('type', 'related')}: {link.get('key', '')}")
    if ticket.attachments:
        lines.extend(["", "## Attachments", ""])
        for att in ticket.attachments:
            fname = att.get("filename", "?")
            mime = att.get("mime_type", "") or "?"
            note = "" if attachment_is_keepable(fname, mime) else " — skipped (binary/media)"
            lines.append(f"- `{fname}` ({mime}){note}")
    return "\n".join(lines)


async def refetch_ticket_data(
    workspace: Workspace, tracker: TrackerInterface | None,
) -> None:
    """Write or append ticket meta files (ticket.md, comments.md, history.md).

    First run (file absent): writes fresh content.
    Rerun (file exists): appends a timestamped refresh block so agents can
    see what changed between runs.
    """
    # [Body copied verbatim from orchestrator.py:540–691 with:
    #   self._tracker         → tracker
    #   self._attachment_is_keepable → attachment_is_keepable
    #   _attachment_is_keepable      → attachment_is_keepable
    #   _ticket_to_markdown          → ticket_to_markdown
    #   _MAX_ATTACHMENT_BYTES        → MAX_ATTACHMENT_BYTES
    # Reflect the Phase-B tracker API: get_comments, get_status_history,
    # download_attachment.]
```

> **Note for the implementer:** the body of `refetch_ticket_data` is the function from orchestrator.py:540–691 *as it stands after Phase B* (i.e. already using `tracker.get_comments` etc.). Copy verbatim, then run sed-style replacements as listed.

- [ ] **Step 2: Replace `_refetch_ticket_data` in `orchestrator.py`**

```python
    async def _refetch_ticket_data(self, workspace: Workspace) -> None:
        from orchestrator.ticket_sync import refetch_ticket_data
        await refetch_ticket_data(workspace, self._tracker)
```

Also replace the bare uses of `_attachment_is_keepable` and `_ticket_to_markdown` (search-replace at module level).

Delete the top-level `_attachment_is_keepable`, `_ticket_to_markdown`, `_MAX_ATTACHMENT_BYTES`, `_TEXT_ATTACHMENT_EXTS` from orchestrator.py.

- [ ] **Step 3: Run tests**

```
pytest tests/unit/test_orchestrator_refetch.py -v
```
Expected: PASS.

- [ ] **Step 4: Commit**

```
git add orchestrator/ticket_sync.py orchestrator/orchestrator.py
git commit -m "refactor(orchestrator): extract ticket_sync module"
```

---

### Task D.3: Extract `notify`

**Files:**
- Create: `orchestrator/notify.py`
- Modify: `orchestrator/orchestrator.py`

- [ ] **Step 1: Create `orchestrator/notify.py`**

Group all TG message-construction functions. The state `_quota_window_end` moves here as a module-level dict keyed on a sentinel (the Orchestrator passes it explicitly) — *or* stays as instance state on `Orchestrator` and is passed in. **Pick the latter** for minimal disruption.

```python
"""Telegram message factory functions, one per pipeline event.

These functions know how to build a message + buttons for one event type.
They take a NotifierInterface as a parameter — no module-level state.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from integrations.base.notifier import Button, NotifierInterface
from orchestrator import tg_format
from workspace.workspace import Workspace

logger = logging.getLogger(__name__)


# Public API — one function per event. Add new ones here as needed; each
# function takes the dependencies it needs and returns the msg_id (or None).
#
# Roster:
#   notify_deferred
#   notify_failed
#   notify_rerun
#   notify_verification_blocked
#   notify_pr_opened
#   notify_qa_warnings
#   notify_done
#   notify_escalation_summary
#
# Bodies: copy verbatim from the corresponding _notify_* methods in
# orchestrator.py with the substitutions:
#   self._notifier        → notifier
#   self._get_chat_id(ws) → chat_id   (passed as parameter)
#   self._quota_window_end → quota_window_end (passed by ref; see notify_deferred)

async def notify_deferred(
    notifier: NotifierInterface | None,
    chat_id: str,
    workspace: Workspace,
    retry_at: datetime,
    quota_window_end_holder: list[datetime | None],
    reason: str | None = None,
) -> None:
    """Implements the body of Orchestrator._notify_deferred.

    quota_window_end_holder is a length-1 list used to mutate the caller's
    quota_window_end across the call boundary. Caller passes
    `[self._quota_window_end]` and reads `quota_window_end_holder[0]` after.
    """
    # [BODY copied verbatim from orchestrator.py:1034–1108 with substitutions
    # listed above. Reads/writes self._quota_window_end become reads/writes
    # to quota_window_end_holder[0].]

# ... and so on for each notify_* function
```

The full set of functions to extract:

| Orchestrator method (line) | New function name |
|---|---|
| `_notify_deferred` (1034) | `notify_deferred` |
| `_notify_failed` (1109) | `notify_failed` |
| `_notify_rerun` (1157) | `notify_rerun` |
| `_notify_verification_blocked` (2464) | `notify_verification_blocked` |

Inline notifications (the `await self._notifier.send_message(...)` blocks inside other methods such as `_handle_agent_stage`, `_handle_action_stage`, `_on_ticket_done`, `_execute_review_decisions`) **stay where they are for now** — they are tightly coupled to the surrounding pipeline state, and moving them complicates the action-handler extractions in Phase D.7–D.9. They become candidates for a follow-up cleanup pass.

- [ ] **Step 2: Update `Orchestrator` methods to delegate**

For each of the 4 methods, replace the body with a 2-3 line shim:

```python
    async def _notify_deferred(
        self, workspace: Workspace, retry_at: datetime,
        reason: str | None = None,
    ) -> None:
        from orchestrator.notify import notify_deferred
        chat_id = self._get_chat_id(workspace)
        holder: list[datetime | None] = [self._quota_window_end]
        await notify_deferred(
            self._notifier, chat_id, workspace, retry_at, holder, reason,
        )
        self._quota_window_end = holder[0]
```

(Equivalent shims for the other three; they don't need the holder pattern.)

- [ ] **Step 3: Run tests**

```
pytest tests/unit/test_orchestrator_deferred.py tests/unit/test_orchestrator_notify_failed.py tests/unit/test_orchestrator_notify_misc.py -v
```
Expected: PASS.

- [ ] **Step 4: Commit**

```
git add orchestrator/notify.py orchestrator/orchestrator.py
git commit -m "refactor(orchestrator): extract notify module"
```

---

### Task D.4: Extract `approval_gate`

**Files:**
- Create: `orchestrator/approval_gate.py`
- Modify: `orchestrator/orchestrator.py`

- [ ] **Step 1: Create `orchestrator/approval_gate.py`**

```python
"""Manual-mode approval gate logic + auto-resume after mode switch.

A 'gate' is a state at which the pipeline waits for explicit operator approval
when running in manual mode. Auto mode bypasses gates entirely.
"""
from __future__ import annotations

import logging
from typing import Any

from integrations.telegram.handlers.mode import ModeHandler
from workspace.workspace import Stage

logger = logging.getLogger(__name__)

APPROVAL_GATE_STATES = {Stage.ANALYSIS, Stage.QA}
GATE_HAPPY_PATH_NEXT_STAGE = {
    Stage.ANALYSIS: "dev",
    Stage.QA: "push",
}


def should_approval_gate(
    mode_handler: ModeHandler | None,
    completed_state: str,
    next_stage: str | None = None,
) -> bool:
    """Should the workspace pause for approval after this state?

    Returns False if there is no mode_handler or mode is 'auto'. When
    `next_stage` is provided, gates only fire on happy-path transitions.
    """
    if not mode_handler or mode_handler.get_mode() != "manual":
        return False
    if completed_state not in APPROVAL_GATE_STATES:
        return False
    if next_stage is None:
        return True
    return next_stage == GATE_HAPPY_PATH_NEXT_STAGE.get(completed_state)
```

- [ ] **Step 2: Replace `_should_approval_gate` in `orchestrator.py`**

```python
    _APPROVAL_GATE_STATES = ...  # leave the existing class constant; some tests may import it
    _GATE_HAPPY_PATH_NEXT_STAGE = ...

    def _should_approval_gate(
        self, completed_state: str, next_stage: str | None = None,
    ) -> bool:
        from orchestrator.approval_gate import should_approval_gate
        return should_approval_gate(self._mode_handler, completed_state, next_stage)
```

- [ ] **Step 3: Run tests**

```
pytest tests/unit/test_orchestrator_modes.py -v
```
Expected: PASS.

- [ ] **Step 4: Commit**

```
git add orchestrator/approval_gate.py orchestrator/orchestrator.py
git commit -m "refactor(orchestrator): extract approval_gate module"
```

---

### Task D.5: Extract `escalation`

**Files:**
- Create: `orchestrator/escalation.py`
- Modify: `orchestrator/orchestrator.py`

- [ ] **Step 1: Create `orchestrator/escalation.py`**

Move: `_build_blocked_reason`, `_truncate_reason`, `_handle_escalate`, `_notify_verification_blocked`.

```python
"""Escalation / BLOCKED state assembly.

When the pipeline cannot progress automatically (verification failure, max
iterations, agent-reported escalate), this module formats the operator
message and updates workspace state.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from integrations.base.notifier import Button, NotifierInterface
from orchestrator import tg_format
from workspace.workspace import Stage, Workspace

logger = logging.getLogger(__name__)

BLOCKED_REASON_MAX_CHARS = 800

BOILERPLATE_LINE_PATTERNS = (
    re.compile(r"^-{3,}$"),
    re.compile(r"^={3,}$"),
    re.compile(r"^\*\*Attempt.*\*\*$"),
    re.compile(r"^## Decision:"),
)


def truncate_reason(text: str) -> str:
    # [body from orchestrator.py:2377–2380]


def build_blocked_reason(workspace: Workspace, stage_id: str) -> str:
    # [body from orchestrator.py:2319–2376]


async def handle_escalate(
    workspace: Workspace,
    notifier: NotifierInterface | None,
    chat_id: str,
    *,
    is_max_iterations: bool = False,
) -> None:
    # [body from orchestrator.py:2382–2463 with self._notifier→notifier,
    #  self._get_chat_id(ws)→chat_id]


async def notify_verification_blocked(
    workspace: Workspace,
    notifier: NotifierInterface | None,
    chat_id: str,
    stage_id: str,
    reason: str,
) -> None:
    # [body from orchestrator.py:2464–2510 with substitutions]
```

- [ ] **Step 2: Replace methods in `orchestrator.py` with shims**

```python
    def _build_blocked_reason(self, workspace: Any, stage_id: str) -> str:
        from orchestrator.escalation import build_blocked_reason
        return build_blocked_reason(workspace, stage_id)

    @classmethod
    def _truncate_reason(cls, text: str) -> str:
        from orchestrator.escalation import truncate_reason
        return truncate_reason(text)

    async def _handle_escalate(
        self, workspace: Workspace, *, is_max_iterations: bool = False,
    ) -> None:
        from orchestrator.escalation import handle_escalate
        chat_id = self._get_chat_id(workspace)
        await handle_escalate(
            workspace, self._notifier, chat_id, is_max_iterations=is_max_iterations,
        )

    async def _notify_verification_blocked(
        self, workspace: Workspace, stage_id: str, reason: str,
    ) -> None:
        from orchestrator.escalation import notify_verification_blocked
        chat_id = self._get_chat_id(workspace)
        await notify_verification_blocked(
            workspace, self._notifier, chat_id, stage_id, reason,
        )
```

- [ ] **Step 3: Run tests**

```
pytest tests/unit/test_orchestrator_blocked_reason.py tests/unit/test_orchestrator_escalate.py tests/unit/test_orchestrator_notify_misc.py -v
```
Expected: PASS.

- [ ] **Step 4: Commit**

```
git add orchestrator/escalation.py orchestrator/orchestrator.py
git commit -m "refactor(orchestrator): extract escalation module"
```

---

### Task D.6: Extract `push_and_open_pr` action

**Files:**
- Create: `orchestrator/pipeline/__init__.py` (empty)
- Create: `orchestrator/pipeline/actions/__init__.py` (empty)
- Create: `orchestrator/pipeline/actions/push_and_open_pr.py`
- Modify: `orchestrator/orchestrator.py`

- [ ] **Step 1: Create the package `__init__.py` files**

Both empty stubs.

- [ ] **Step 2: Create `orchestrator/pipeline/actions/push_and_open_pr.py`**

Move 4 methods: `_action_push_and_open_pr`, `_commit_pipeline_artifacts`, `_ensure_branch_has_commits`, `_squash_feature_commits`.

The signature change pattern for the action function:

```python
"""push_and_open_pr action: commit pipeline artifacts, squash feature
commits, push the branch, and open (or update) the PR."""
from __future__ import annotations

import logging
import subprocess

from config.schemas import RepoConfig
from integrations.base.notifier import Button, NotifierInterface
from integrations.base.vcs import VCSInterface
from orchestrator import tg_format
from orchestrator.git_ops import git_diff_files, git_head_sha
from orchestrator.pr_creation import create_pr
from orchestrator.stage_verifier import ActionResult
from workspace.workspace import Stage, Workspace

logger = logging.getLogger(__name__)


def commit_pipeline_artifacts(
    workspace: Workspace, repo_config: RepoConfig,
) -> None:
    """[body from orchestrator.py:1484–1547 unchanged]"""


def ensure_branch_has_commits(
    workspace: Workspace, repo_config: RepoConfig,
) -> None:
    """[body from orchestrator.py:1548–1621 unchanged]"""


def squash_feature_commits(
    workspace: Workspace, repo_config: RepoConfig,
) -> None:
    """[body from orchestrator.py:1622–1708 unchanged]"""


async def action_push_and_open_pr(
    workspace: Workspace,
    vcs: VCSInterface | None,
    repo_config: RepoConfig | None,
    notifier: NotifierInterface | None,
    chat_id: str,
) -> ActionResult:
    """[body from orchestrator.py:1376–1483 with substitutions:
       self._get_vcs_for_workspace(ws) → (vcs, repo_config) passed in
       self._git_diff_files(ws)        → git_diff_files(ws)
       self._git_head_sha(ws)          → git_head_sha(ws)
       self._commit_pipeline_artifacts → commit_pipeline_artifacts
       self._ensure_branch_has_commits → ensure_branch_has_commits
       self._squash_feature_commits    → squash_feature_commits
       self._notifier                  → notifier
       self._get_chat_id(ws)           → chat_id]"""
```

- [ ] **Step 3: Replace `_action_push_and_open_pr` in `orchestrator.py` with shim**

```python
    async def _action_push_and_open_pr(self, workspace: Workspace) -> ActionResult:
        from orchestrator.pipeline.actions.push_and_open_pr import action_push_and_open_pr
        vcs, repo_config = self._get_vcs_for_workspace(workspace)
        chat_id = self._get_chat_id(workspace)
        return await action_push_and_open_pr(
            workspace, vcs, repo_config, self._notifier, chat_id,
        )
```

Also replace the three helper methods (`_commit_pipeline_artifacts`, `_ensure_branch_has_commits`, `_squash_feature_commits`) with thin shims that delegate.

- [ ] **Step 4: Run tests**

```
pytest tests/unit/test_orchestrator_squash.py tests/unit/test_ensure_branch_has_commits.py tests/unit/test_action_stage.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add orchestrator/pipeline orchestrator/orchestrator.py
git commit -m "refactor(orchestrator): extract push_and_open_pr action"
```

---

### Task D.7: Extract `fetch_pr_comments` action

**Files:**
- Create: `orchestrator/pipeline/actions/fetch_pr_comments.py`
- Modify: `orchestrator/orchestrator.py`

This is the **highest-risk extraction.** Move:
- `_action_fetch_pr_comments` (orchestrator.py:1823–2087, ~265 lines)
- `_reinvestigate_pending` (orchestrator.py:1709–1822, ~114 lines)
- `_execute_review_decisions` (orchestrator.py:2102–2243, ~142 lines)
- `_send_escalated_comment_tg` (orchestrator.py:2088–2101)

- [ ] **Step 1: Create `orchestrator/pipeline/actions/fetch_pr_comments.py`**

Mirror the orchestrator method bodies as top-level async functions. Required parameters:

```python
async def action_fetch_pr_comments(
    workspace: Workspace,
    stage_def: Any,
    *,
    vcs: VCSInterface | None,
    repo_config: RepoConfig | None,
    tracker: TrackerInterface | None,
    notifier: NotifierInterface | None,
    chat_id: str,
) -> ActionResult:
    ...

async def reinvestigate_pending(
    workspace: Workspace,
    *,
    vcs: VCSInterface | None,
    repo_config: RepoConfig | None,
    notifier: NotifierInterface | None,
    chat_id: str,
) -> None:
    ...

async def execute_review_decisions(
    workspace: Workspace,
    *,
    vcs: VCSInterface | None,
    repo_config: RepoConfig | None,
    notifier: NotifierInterface | None,
    chat_id: str,
) -> ActionResult:
    ...

async def send_escalated_comment_tg(
    workspace: Workspace, cc: Any, pr_number: int,
    *,
    notifier: NotifierInterface | None,
    chat_id: str,
) -> int:
    ...
```

The function bodies are direct copies of the orchestrator methods, with each `self.X` replaced as listed above. **No logic changes.**

- [ ] **Step 2: Replace the four methods in `orchestrator.py` with shims**

```python
    async def _action_fetch_pr_comments(
        self, workspace: Workspace, stage_def: Any,
    ) -> ActionResult:
        from orchestrator.pipeline.actions.fetch_pr_comments import action_fetch_pr_comments
        vcs, repo_config = self._get_vcs_for_workspace(workspace)
        chat_id = self._get_chat_id(workspace)
        return await action_fetch_pr_comments(
            workspace, stage_def,
            vcs=vcs, repo_config=repo_config, tracker=self._tracker,
            notifier=self._notifier, chat_id=chat_id,
        )

    async def _reinvestigate_pending(self, workspace: Workspace) -> None:
        from orchestrator.pipeline.actions.fetch_pr_comments import reinvestigate_pending
        vcs, repo_config = self._get_vcs_for_workspace(workspace)
        chat_id = self._get_chat_id(workspace)
        await reinvestigate_pending(
            workspace,
            vcs=vcs, repo_config=repo_config,
            notifier=self._notifier, chat_id=chat_id,
        )

    async def _execute_review_decisions(self, workspace: Workspace) -> ActionResult:
        from orchestrator.pipeline.actions.fetch_pr_comments import execute_review_decisions
        vcs, repo_config = self._get_vcs_for_workspace(workspace)
        chat_id = self._get_chat_id(workspace)
        return await execute_review_decisions(
            workspace,
            vcs=vcs, repo_config=repo_config,
            notifier=self._notifier, chat_id=chat_id,
        )

    async def _send_escalated_comment_tg(
        self, workspace: Workspace, cc: Any, pr_number: int,
    ) -> int:
        from orchestrator.pipeline.actions.fetch_pr_comments import send_escalated_comment_tg
        chat_id = self._get_chat_id(workspace)
        return await send_escalated_comment_tg(
            workspace, cc, pr_number,
            notifier=self._notifier, chat_id=chat_id,
        )
```

- [ ] **Step 3: Run the PR-review-loop characterization tests**

```
pytest tests/unit/test_orchestrator_pr_review_loop.py tests/unit/test_action_stage.py tests/unit/test_comment_classifier.py tests/unit/test_resolution_report.py -v
```
Expected: PASS.

- [ ] **Step 4: Commit**

```
git add orchestrator/pipeline orchestrator/orchestrator.py
git commit -m "refactor(orchestrator): extract fetch_pr_comments action (highest-risk move)"
```

---

### Task D.8: Extract `finalize` action

**Files:**
- Create: `orchestrator/pipeline/actions/finalize.py`
- Modify: `orchestrator/orchestrator.py`

Move `_action_finalize` (orchestrator.py:2511–2541) and `_on_ticket_done` (orchestrator.py:2244–2308) into this module. Both are small.

- [ ] **Step 1: Create the file**

```python
"""finalize action and ticket-done handling."""
from __future__ import annotations

import logging
from typing import Any

from integrations.base.notifier import NotifierInterface
from integrations.base.tracker import TrackerInterface
from orchestrator import tg_format
from orchestrator.stage_verifier import ActionResult
from workspace.workspace import Stage, Workspace

logger = logging.getLogger(__name__)


async def action_finalize(
    workspace: Workspace,
    notifier: NotifierInterface | None,
    chat_id: str,
    tracker: TrackerInterface | None,
) -> ActionResult:
    """[body from orchestrator.py:2511–2541 with substitutions]"""


async def on_ticket_done(
    workspace: Workspace,
    notifier: NotifierInterface | None,
    chat_id: str,
    tracker: TrackerInterface | None,
    in_review_status: str,
) -> None:
    """[body from orchestrator.py:2244–2308 with substitutions and Phase-B
    tracker API (list_transitions / transition_ticket)]"""
```

- [ ] **Step 2: Replace methods in `orchestrator.py` with shims**

```python
    async def _action_finalize(self, workspace: Workspace) -> ActionResult:
        from orchestrator.pipeline.actions.finalize import action_finalize
        chat_id = self._get_chat_id(workspace)
        return await action_finalize(
            workspace, self._notifier, chat_id, self._tracker,
        )

    async def _on_ticket_done(self, workspace: Workspace) -> None:
        from orchestrator.pipeline.actions.finalize import on_ticket_done
        chat_id = self._get_chat_id(workspace)
        project = self._projects.get(workspace.state.company_id)
        in_review_status = (
            project.config.jira.statuses.in_review if project else ""
        )
        await on_ticket_done(
            workspace, self._notifier, chat_id, self._tracker, in_review_status,
        )
```

- [ ] **Step 3: Run tests**

```
pytest tests/unit/test_orchestrator_finalize.py tests/unit/test_orchestrator_done.py -v
```
Expected: PASS.

- [ ] **Step 4: Commit**

```
git add orchestrator/pipeline orchestrator/orchestrator.py
git commit -m "refactor(orchestrator): extract finalize action and on_ticket_done"
```

---

## Phase E: Extract pipeline driver and runtime

### Task E.1: Extract `agent_stage`

**Files:**
- Create: `orchestrator/pipeline/agent_stage.py`
- Modify: `orchestrator/orchestrator.py`

- [ ] **Step 1: Create `orchestrator/pipeline/agent_stage.py`**

Move `_handle_agent_stage` and `_parse_agent_outcome` (orchestrator.py:858–1021 + 2630–2665) and `_rollback_iteration` (1022–1033).

```python
"""Agent stage execution: dispatch agent, verify, parse outcome, advance."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from config.schemas import RepoConfig
from integrations.base.notifier import NotifierInterface
from orchestrator import stage_verifier, tg_format
from orchestrator.agent_runtime import AgentRuntime
from orchestrator.git_ops import git_head_sha
from orchestrator.workflow_router import WorkflowDefinition, get_next_stage
from workspace.workspace import Stage, Workspace

logger = logging.getLogger(__name__)

DEFAULT_QUOTA_RETRY_DELAY = timedelta(hours=1)


def rollback_iteration(workspace: Workspace, stage_id: str) -> None:
    """[body from orchestrator.py:1022–1033]"""


def parse_agent_outcome(stage_id: str, output: str, workspace: Workspace) -> str | None:
    """[body from orchestrator.py:2630–2665]"""


async def handle_agent_stage(
    workspace: Workspace,
    stage_id: str,
    stage_def: Any,
    *,
    workflow: WorkflowDefinition,
    agent_runtime: AgentRuntime,
    repo_config: RepoConfig | None,
    notifier: NotifierInterface | None,
    chat_id: str,
    dry_run: bool,
    on_escalate: Callable,                      # async callable (workspace, *, is_max_iterations=False)
    on_advance: Callable,                       # callable (workspace, next_state)
    on_done: Callable,                          # async callable (workspace)
    should_gate: Callable,                      # callable (completed_state, next_stage) -> bool
    build_gate_summary: Callable,               # callable (workspace, gate_state) -> (text, buttons)
    notify_verification_blocked: Callable,      # async callable (workspace, stage_id, reason)
    emit: Callable,                             # callable (event_type, message, **kwargs)
    log_pipeline: Callable,                     # callable (workspace, entry)
) -> None:
    """[body from orchestrator.py:858–1021 with substitutions:
       self._handle_escalate → on_escalate
       self._advance_to_stage → on_advance
       self._on_ticket_done → on_done
       self._should_approval_gate → should_gate
       self._build_gate_summary → build_gate_summary
       self._notify_verification_blocked → notify_verification_blocked
       self._emit → emit
       self._log_pipeline → log_pipeline
       self._workflow → workflow
       self._agent_runtime → agent_runtime
       self._notifier → notifier
       self._dry_run → dry_run
       self._get_repo_config(ws) → repo_config (passed in)
       self._get_chat_id(ws) → chat_id (passed in)
       self._git_head_sha → git_head_sha
       self._rollback_iteration → rollback_iteration
       self._parse_agent_outcome → parse_agent_outcome]"""
```

- [ ] **Step 2: Replace `_handle_agent_stage` in `orchestrator.py` with shim**

```python
    async def _handle_agent_stage(
        self, workspace: Workspace, stage_id: str, stage_def: Any,
    ) -> None:
        from orchestrator.pipeline.agent_stage import handle_agent_stage
        repo_config = self._get_repo_config(workspace)
        chat_id = self._get_chat_id(workspace)
        await handle_agent_stage(
            workspace, stage_id, stage_def,
            workflow=self._workflow,
            agent_runtime=self._agent_runtime,
            repo_config=repo_config,
            notifier=self._notifier,
            chat_id=chat_id,
            dry_run=self._dry_run,
            on_escalate=self._handle_escalate,
            on_advance=self._advance_to_stage,
            on_done=self._on_ticket_done,
            should_gate=self._should_approval_gate,
            build_gate_summary=self._build_gate_summary,
            notify_verification_blocked=self._notify_verification_blocked,
            emit=self._emit,
            log_pipeline=self._log_pipeline,
        )
```

Also shim `_rollback_iteration` and `_parse_agent_outcome`.

- [ ] **Step 3: Run tests**

```
pytest tests/unit/test_orchestrator_stage_verify.py tests/unit/test_orchestrator_deferred.py tests/unit/test_orchestrator_advance_happy.py -v
```
Expected: PASS.

- [ ] **Step 4: Commit**

```
git add orchestrator/pipeline orchestrator/orchestrator.py
git commit -m "refactor(orchestrator): extract agent_stage module"
```

---

### Task E.2: Extract `action_stage`

**Files:**
- Create: `orchestrator/pipeline/action_stage.py`
- Modify: `orchestrator/orchestrator.py`

Move `_handle_action_stage` (orchestrator.py:1244–1375).

- [ ] **Step 1: Create the file**

```python
"""Action stage dispatcher: route to the named action, verify, advance."""
from __future__ import annotations

import logging
from typing import Any, Callable

from integrations.base.notifier import NotifierInterface
from orchestrator import stage_verifier, tg_format
from orchestrator.workflow_router import WorkflowDefinition, get_next_stage
from workspace.workspace import Stage, Workspace

logger = logging.getLogger(__name__)


async def handle_action_stage(
    workspace: Workspace,
    stage_id: str,
    stage_def: Any,
    *,
    workflow: WorkflowDefinition,
    notifier: NotifierInterface | None,
    chat_id: str,
    dry_run: bool,
    on_action_push: Callable,         # async (workspace) → ActionResult
    on_action_fetch: Callable,        # async (workspace, stage_def) → ActionResult
    on_action_finalize: Callable,     # async (workspace) → ActionResult
    on_escalate: Callable,
    on_advance: Callable,
    on_done: Callable,
    should_gate: Callable,
    build_gate_summary: Callable,
    rollback_iteration: Callable,
    log_pipeline: Callable,
    emit: Callable,
) -> None:
    """[body from orchestrator.py:1244–1375 with self.X → injected callables]"""
```

- [ ] **Step 2: Replace `_handle_action_stage` in `orchestrator.py` with shim**

(equivalent to E.1, wiring all callables to `self._*` methods)

- [ ] **Step 3: Run tests**

```
pytest tests/unit/test_action_stage.py -v
```
Expected: PASS.

- [ ] **Step 4: Commit**

```
git add orchestrator/pipeline orchestrator/orchestrator.py
git commit -m "refactor(orchestrator): extract action_stage dispatcher"
```

---

### Task E.3: Extract `driver`

**Files:**
- Create: `orchestrator/pipeline/driver.py`
- Modify: `orchestrator/orchestrator.py`

Move `advance_workspace` (orchestrator.py:780–857) and `_advance_to_stage` (orchestrator.py:2555–2564) and `_build_gate_summary` (orchestrator.py:2565–2629).

- [ ] **Step 1: Create the file**

```python
"""Pipeline state-machine driver: advance one workspace one step."""
from __future__ import annotations

import logging
from typing import Any, Callable

from integrations.base.notifier import Button, NotifierInterface
from integrations.telegram.handlers.approval import APPROVAL_NEXT_STATE
from integrations.telegram.handlers.mode import ModeHandler
from orchestrator.workflow_router import (
    WorkflowDefinition, get_next_stage, should_escalate,
)
from workspace.workspace import Stage, Workspace

logger = logging.getLogger(__name__)


def _stage_to_state(stage_id: str) -> str | None:
    """[copied from orchestrator.py module level]"""


def _state_to_stage(state: str) -> str | None:
    """[copied from orchestrator.py module level]"""


async def advance_workspace(
    workspace: Workspace,
    *,
    workflow: WorkflowDefinition,
    mode_handler: ModeHandler | None,
    handle_agent_stage: Callable,
    handle_action_stage: Callable,
    handle_escalate: Callable,
    _resume_depth: int = 0,
) -> None:
    """[body from orchestrator.py:780–857]"""


def advance_to_stage(workspace: Workspace, stage_id: str) -> None:
    """[body from orchestrator.py:2555–2564]"""


def build_gate_summary(workspace: Workspace, gate_state: str) -> tuple[str, list[Button]]:
    """[body from orchestrator.py:2565–2629]"""
```

- [ ] **Step 2: Replace methods in `orchestrator.py` with shims**

```python
    async def advance_workspace(
        self, workspace: Workspace, _resume_depth: int = 0,
    ) -> None:
        from orchestrator.pipeline.driver import advance_workspace
        await advance_workspace(
            workspace,
            workflow=self._workflow,
            mode_handler=self._mode_handler,
            handle_agent_stage=self._handle_agent_stage,
            handle_action_stage=self._handle_action_stage,
            handle_escalate=self._handle_escalate,
            _resume_depth=_resume_depth,
        )

    def _advance_to_stage(self, workspace: Workspace, stage_id: str) -> None:
        from orchestrator.pipeline.driver import advance_to_stage
        advance_to_stage(workspace, stage_id)

    def _build_gate_summary(
        self, workspace: Workspace, gate_state: str,
    ) -> tuple[str, list[Button]]:
        from orchestrator.pipeline.driver import build_gate_summary
        return build_gate_summary(workspace, gate_state)
```

- [ ] **Step 3: Run tests**

```
pytest tests/unit/test_orchestrator_advance_happy.py tests/unit/test_orchestrator_modes.py tests/unit/test_orchestrator_manual_control.py -v
```
Expected: PASS.

- [ ] **Step 4: Commit**

```
git add orchestrator/pipeline orchestrator/orchestrator.py
git commit -m "refactor(orchestrator): extract pipeline driver"
```

---

### Task E.4: Extract `ingest`

**Files:**
- Create: `orchestrator/ingest.py`
- Modify: `orchestrator/orchestrator.py`

Move `_poll_and_create_workspaces` (orchestrator.py:458–539), `_create_workspace_for_ticket` (693–779), `_route_manual_ticket` (259–267), `analyze_ticket_ids` (198–257).

- [ ] **Step 1: Create the file**

```python
"""Ticket ingest: poll → filter → route → create workspace.

Provider-neutral; tracker is injected via TrackerInterface.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from config.schemas import GlobalConfig, LoadedProject, RepoConfig
from integrations.base.tracker import TicketData, TrackerInterface
from integrations.telegram.handlers.analyze import AnalyzeHandler
from orchestrator.model_resolver import resolve_ticket_model
from orchestrator.ticket_prioritizer import (
    PrioritizedTicket, filter_tickets, prioritize_tickets, route_tickets,
)
from orchestrator.ticket_sync import refetch_ticket_data, ticket_to_markdown
from workspace.workspace import Stage, Workspace
from workspace.workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)


def route_manual_ticket(
    ticket: TicketData, projects: dict[str, LoadedProject],
) -> PrioritizedTicket | None:
    """[body from orchestrator.py:259–267 with tracker_label]"""


async def poll_and_create_workspaces(
    *,
    tracker: TrackerInterface,
    projects: dict[str, LoadedProject],
    active_workspaces: list[Workspace],
    workspaces_base_dir: str,
    workspace_manager: WorkspaceManager,
    dry_run: bool,
    create_workspace: Callable,        # async (pt, project_id, repo_config) → Workspace
    emit: Callable,
) -> list[Workspace]:
    """Return newly-created workspaces (append to active_workspaces by caller)."""
    # [body from orchestrator.py:458–539]


async def create_workspace_for_ticket(
    pt: PrioritizedTicket,
    project_id: str,
    repo_config: RepoConfig,
    *,
    workspace_manager: WorkspaceManager,
    tracker: TrackerInterface | None,
    notifier_factory: Callable | None = None,   # for label-conflict notifications
) -> Workspace:
    """[body from orchestrator.py:693–779 with substitutions]"""


async def analyze_ticket_ids(
    ticket_ids: list[str],
    *,
    tracker: TrackerInterface,
    projects: dict[str, LoadedProject],
    active_workspaces: list[Workspace],
    dry_run: bool,
    create_workspace: Callable,
) -> dict[str, list[str]]:
    """[body from orchestrator.py:198–257]"""
```

- [ ] **Step 2: Replace methods in `orchestrator.py` with shims**

```python
    def _route_manual_ticket(self, ticket: TicketData) -> PrioritizedTicket | None:
        from orchestrator.ingest import route_manual_ticket
        return route_manual_ticket(ticket, self._projects)

    async def _poll_and_create_workspaces(self) -> None:
        from orchestrator.ingest import poll_and_create_workspaces
        new = await poll_and_create_workspaces(
            tracker=self._tracker,
            projects=self._projects,
            active_workspaces=self._active_workspaces,
            workspaces_base_dir=self._global_config.workspaces.base_dir,
            workspace_manager=self._workspace_manager,
            dry_run=self._dry_run,
            create_workspace=self._create_workspace_for_ticket,
            emit=self._emit,
        )
        self._active_workspaces.extend(new)

    async def _create_workspace_for_ticket(
        self, pt: PrioritizedTicket, project_id: str, repo_config: RepoConfig,
    ) -> Workspace:
        from orchestrator.ingest import create_workspace_for_ticket
        return await create_workspace_for_ticket(
            pt, project_id, repo_config,
            workspace_manager=self._workspace_manager,
            tracker=self._tracker,
        )

    async def analyze_ticket_ids(
        self, ticket_ids: list[str],
    ) -> dict[str, list[str]]:
        from orchestrator.ingest import analyze_ticket_ids
        return await analyze_ticket_ids(
            ticket_ids,
            tracker=self._tracker, projects=self._projects,
            active_workspaces=self._active_workspaces, dry_run=self._dry_run,
            create_workspace=self._create_workspace_for_ticket,
        )
```

- [ ] **Step 3: Run tests**

```
pytest tests/unit/test_orchestrator_poll_create.py tests/unit/test_orchestrator_model_label.py tests/integration/test_e2e_dry_run.py -v
```
Expected: PASS.

- [ ] **Step 4: Commit**

```
git add orchestrator/ingest.py orchestrator/orchestrator.py
git commit -m "refactor(orchestrator): extract ingest module"
```

---

### Task E.5: Extract `runtime`

**Files:**
- Create: `orchestrator/runtime.py`
- Modify: `orchestrator/orchestrator.py`

Move `run` (orchestrator.py:350–401), `poll_cycle` (402–457), `_reconcile_disk_workspaces` (1179–1207), `_sweep_deferred` (1208–1243), `shutdown` (2666–2669), `_handle_shutdown` (2670–2673), `_emit` (2674–2695).

- [ ] **Step 1: Create the file**

```python
"""Daemon runtime: signals, poll loop, semaphore, shutdown.

Owns: _active_workspaces, _recent_completions, _agent_semaphore,
_shutdown_event, _wake_event, signal handlers.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from config.schemas import GlobalConfig
from workspace.workspace import Stage, Workspace
from workspace.workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)


class Runtime:
    """Daemon shell. Owns the event loop, semaphore, and signal handling."""

    def __init__(
        self,
        global_config: GlobalConfig,
        workspace_manager: WorkspaceManager,
        *,
        poll_callback: Callable,         # async (active_workspaces) → None
        advance_callback: Callable,      # async (workspace) → None
        event_bus: Any | None = None,
    ) -> None:
        self._global_config = global_config
        self._workspace_manager = workspace_manager
        self._poll_callback = poll_callback
        self._advance_callback = advance_callback
        self._events = event_bus

        self._active_workspaces: list[Workspace] = []
        self._recent_completions: deque[tuple[str, str, float]] = deque(maxlen=20)
        self._shutdown_event = asyncio.Event()
        self._wake_event = asyncio.Event()

        try:
            max_parallel = int(global_config.defaults.max_parallel_tickets)
        except (TypeError, ValueError, AttributeError):
            max_parallel = 3
        self._agent_semaphore = asyncio.Semaphore(max_parallel)

    @property
    def active_workspaces(self) -> list[Workspace]:
        return self._active_workspaces

    @property
    def recent_completions(self) -> list[tuple[str, str, float]]:
        return list(self._recent_completions)

    def wake(self) -> None:
        self._wake_event.set()

    def shutdown(self) -> None:
        self._shutdown_event.set()

    def _handle_shutdown(self) -> None:
        logger.info("Shutdown signal received")
        self.shutdown()

    def emit(self, event_type: str, message: str, **kwargs: Any) -> None:
        """[body from orchestrator.py:2674–2695]"""

    def reconcile_disk_workspaces(self) -> None:
        """[body from orchestrator.py:1179–1207]"""

    async def sweep_deferred(self) -> None:
        """[body from orchestrator.py:1208–1243]"""

    async def run(self) -> None:
        """[body from orchestrator.py:350–401]"""

    async def poll_cycle(self) -> None:
        """[body from orchestrator.py:402–457 but using self._poll_callback
        and self._advance_callback instead of self._poll_and_create_workspaces
        and self.advance_workspace]"""
```

- [ ] **Step 2: Restructure `Orchestrator` to wrap `Runtime`**

In `orchestrator/orchestrator.py`, `Orchestrator.__init__` now constructs a `Runtime`:

```python
        self._runtime = Runtime(
            global_config=global_config,
            workspace_manager=workspace_manager,
            poll_callback=self._poll_and_create_workspaces,
            advance_callback=self.advance_workspace,
            event_bus=event_bus,
        )
```

Replace `run`, `poll_cycle`, `shutdown`, `_handle_shutdown`, `_emit`, `_reconcile_disk_workspaces`, `_sweep_deferred`:

```python
    async def run(self) -> None:
        await self._runtime.run()

    async def poll_cycle(self) -> None:
        await self._runtime.poll_cycle()

    def shutdown(self) -> None:
        self._runtime.shutdown()

    def _handle_shutdown(self) -> None:
        self._runtime._handle_shutdown()

    def _emit(self, event_type: str, message: str, **kwargs: Any) -> None:
        self._runtime.emit(event_type, message, **kwargs)

    def _reconcile_disk_workspaces(self) -> None:
        self._runtime.reconcile_disk_workspaces()

    async def _sweep_deferred(self) -> None:
        await self._runtime.sweep_deferred()
```

Replace `self._active_workspaces` with `self._runtime._active_workspaces` (or expose via a property on Orchestrator). Replace `self._recent_completions` similarly. **This is the most invasive change of the refactor;** scan with `grep -n "self\._active_workspaces\|self\._recent_completions" orchestrator/orchestrator.py` and update each reference.

- [ ] **Step 3: Run the full unit suite**

```
pytest tests/unit/ -x
```
Expected: PASS.

- [ ] **Step 4: Run integration tests**

```
pytest tests/integration/ -x
```
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add orchestrator/runtime.py orchestrator/orchestrator.py
git commit -m "refactor(orchestrator): extract Runtime class"
```

---

## Phase F: Test import cleanup

### Task F.1: Audit tests that mock the leaky tracker

- [ ] **Step 1: Grep for leftover `_request` / `_email` / `_token` mocks**

```
grep -rn "tracker\._request\|tracker\._email\|tracker\._token\|_tracker\._request" tests/
```

Expected: only `tests/unit/test_orchestrator_finalize.py` may still reference them in legacy tests that predate Phase B; the new tests use `list_transitions` / `transition_ticket`.

- [ ] **Step 2: Update any leftover tests to use the new API**

Replace `tracker._request.side_effect = [...]` with `tracker.list_transitions.return_value = [...]` plus optional `tracker.transition_ticket.assert_awaited_with(...)` as needed.

- [ ] **Step 3: Run full suite**

```
pytest tests/ -x
```
Expected: PASS.

- [ ] **Step 4: Commit if anything changed**

```
git add tests/
git commit -m "test(orchestrator): migrate remaining mocks to public tracker API"
```

If nothing changed, skip this step.

---

## Phase G: Cleanup

### Task G.1: End-to-end dry-run validation

- [ ] **Step 1: Run dry-run integration tests**

```
pytest tests/integration/test_e2e_dry_run.py tests/integration/test_manual_mode_flow.py -v
```
Expected: PASS.

- [ ] **Step 2: Run a real dry-run cycle on disk**

```
python -m main --dry-run --once 2>&1 | tee /tmp/dryrun.log
```

Inspect `/tmp/dryrun.log` for any error tracebacks. Expected: pipeline polls, makes no real changes, exits cleanly.

- [ ] **Step 3: No commit (validation only)**

---

### Task G.2: Remove orchestrator method shims (optional, scoped to safety)

The shims left by Phases D and E keep `Orchestrator.<method>` callable for tests and Telegram handler imports. If `grep -rn "orchestrator\.\(orchestrator\.\)\?Orchestrator\." tests/ integrations/` shows external callers, **keep the shims** — they're cheap.

- [ ] **Step 1: Audit external dependencies**

```
grep -rn "from orchestrator.orchestrator import\|orchestrator\._" integrations/ dashboard/ tasks/ workflows/ 2>/dev/null
```

- [ ] **Step 2: Only if no external callers exist for a shim, remove it**

E.g. if nothing outside `orchestrator/` calls `Orchestrator._git_diff_files`, that shim can go. Be conservative.

- [ ] **Step 3: Commit if anything removed**

```
git add orchestrator/orchestrator.py
git commit -m "refactor(orchestrator): remove unused method shims"
```

---

### Task G.3: Confirm size targets and update spec

- [ ] **Step 1: Measure post-refactor LOC**

```
wc -l orchestrator/orchestrator.py
```
Expected: ≤ 400 lines.

- [ ] **Step 2: Confirm no Jira leakage**

```
grep -n "_request\|_email\|_token\|jira_repo_label" orchestrator/orchestrator.py orchestrator/pipeline/
```
Expected: no output.

- [ ] **Step 3: Confirm no provider ternaries**

```
grep -n "vcs\.provider ==" orchestrator/
```
Expected: no output.

- [ ] **Step 4: If targets met, commit a summary note in the spec footer (or PR description)**

No code change; this is a verification step.

---

## Success criteria checklist

- [ ] `orchestrator/orchestrator.py` ≤ 400 LOC
- [ ] No `_request`, `_email`, `_token` references on `tracker` outside `integrations/jira/`
- [ ] No `provider == "github"` ternaries in `orchestrator/`
- [ ] Renamed `jira_repo_label` → `tracker_label` with backward-compat alias in loader
- [ ] All Phase-0 characterization tests pass before AND after the refactor
- [ ] `pytest tests/ -x` exits 0
- [ ] One full `python -m main --dry-run --once` cycle completes cleanly
