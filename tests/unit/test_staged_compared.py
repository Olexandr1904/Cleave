# tests/unit/test_staged_compared.py
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from config.schemas import GitHubConfig, LoadedProject, RepoConfig, RepoInfo, TelegramConfig, VCSConfig
from orchestrator import orchestrator as orchestrator_module
from orchestrator.orchestrator import Orchestrator
from orchestrator.pipeline import action_stage
from orchestrator.pipeline.action_stage import handle_action_stage
from orchestrator.pipeline.actions.fetch_pr_comments import action_fetch_pr_comments
from orchestrator.pr_creation import create_pr, tracker_writes_for_pr
from orchestrator.reviewer.db import connect
from orchestrator.skillflow.compare_store import ComparisonStore
from orchestrator.stage_verifier import ActionResult, VerifyResult
from orchestrator.tg_format import tg_header
from workspace.workspace import Stage, Workspace, WorkspaceState

NOW = "2026-11-18T10:00:00+00:00"


class FakeVCS:
    def __init__(self):
        self.pushed: list[str] = []
        self.opened: list[tuple[str, str, bool]] = []
        self.draft_passed: list[bool] = []     # whether open_pr got a draft argument at all
        self.comment_reads = 0

    async def push(self, source, branch, force=False, skip_hooks=False):
        self.pushed.append(branch)

    async def open_pr(self, title, body, head_branch, base_branch, **kwargs):
        self.opened.append((title, base_branch, kwargs.get("draft", False)))
        self.draft_passed.append("draft" in kwargs)
        return 7, "u"

    async def get_pr_comments(self, pr_number):
        self.comment_reads += 1
        return []


class FakeTracker:
    def __init__(self, fail=frozenset()):
        self.fail = fail
        self.calls: list[tuple[str, str, str]] = []

    async def transition_ticket(self, ticket_id, status):
        if "transition" in self.fail:
            raise RuntimeError("tracker said no")
        self.calls.append(("transition", ticket_id, status))

    async def add_comment(self, ticket_id, text):
        if "comment" in self.fail:
            raise RuntimeError("tracker said no")
        self.calls.append(("comment", ticket_id, text))


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, object]] = []

    async def send_message(self, chat_id, message, buttons=None):
        self.sent.append((message, buttons))
        return 99


def make_ws(tmp_path, summary="Fix login", **state):
    root = tmp_path / "ws"
    ws = Workspace(str(root), WorkspaceState(ticket_id="ACME-1", company_id="acme", repo_id="app",
                                             workspace_root=str(root), branch="feature/ACME-1", **state))
    for d in (ws.meta_dir, ws.reports_dir, ws.source_dir):
        d.mkdir(parents=True, exist_ok=True)
    (ws.meta_dir / "ticket.md").write_text(f"# ACME-1: {summary}\n", encoding="utf-8")
    (ws.meta_dir / "scope-certificate.md").write_text("PASS", encoding="utf-8")
    ws.save_state()
    return ws


@pytest.fixture
def ws(tmp_path):
    return make_ws(tmp_path)


@pytest.fixture
def ws_long_title(tmp_path):
    return make_ws(tmp_path, summary="Fix login " * 40)


@pytest.fixture
def ws_in_pr_review(tmp_path):
    return make_ws(tmp_path, current_state=Stage.PR_REVIEW, pr_number=7, pr_url="u")


@pytest.fixture
def vcs():
    return FakeVCS()


@pytest.fixture
def tracker():
    return FakeTracker()


@pytest.fixture
def repo_cfg():
    return RepoConfig(repo=RepoInfo(id="app"), vcs=VCSConfig(github=GitHubConfig(default_branch="develop")))


class ActionStageRig:
    """Drives handle_action_stage through a successful push_and_open_pr with a fake notifier."""

    def __init__(self, ws):
        self.ws = ws
        self.notifier = FakeNotifier()

    async def push(self, compared, reviewer_owns=False):
        async def push_and_open_pr(_ws):
            return ActionResult(success=True, next_state=Stage.PR_REVIEW, error="",
                                metadata={"pr_url": "u", "pr_number": 7})

        def unused(*_args):
            raise AssertionError("not called on a successful push")

        def is_compared(ws):
            # Asked just before the PR message, after the move to PR_REVIEW, which itself clears
            # human_input_reply (Workspace.transition): set here, the value shows what the message does to it.
            ws.state.human_input_reply = "reviewed"
            return compared

        await handle_action_stage(
            self.ws, "push", SimpleNamespace(action="push_and_open_pr"),
            workflow=None, notifier=self.notifier, mode_handler=None, get_chat_id=lambda: "-100",
            dry_run=False, event_bus=None,
            action_push_and_open_pr=push_and_open_pr, action_fetch_pr_comments=unused, action_finalize=unused,
            advance_to_stage_fn=unused, on_ticket_done_fn=unused, build_gate_summary_fn=unused,
            is_compared=is_compared, reviewer_owns=reviewer_owns,
        )


@pytest.fixture
def rig_action_stage(tmp_path, monkeypatch):
    # The push verifier asks the remote for the branch; there is no remote here.
    monkeypatch.setattr(action_stage.stage_verifier, "verify",
                        lambda stage_id, *_a, **_k: VerifyResult(ok=True, stage_id=stage_id, reason=""))
    return ActionStageRig(make_ws(tmp_path, current_state=Stage.PUSHED, escalation_msg_id=5, escalation_chat_id="-7"))


class CountingComparisons:
    """The real ComparisonStore, counting for_workspace lookups."""

    def __init__(self, store: ComparisonStore):
        self._store = store
        self.lookups = 0

    def for_workspace(self, scope, ticket_id, started_at):
        self.lookups += 1
        return self._store.for_workspace(scope, ticket_id, started_at)

    def add(self, *args, **kwargs):
        return self._store.add(*args, **kwargs)

    def claim(self, *args, **kwargs):
        return self._store.claim(*args, **kwargs)

    def open_tickets(self, scope):
        return self._store.open_tickets(scope)


@pytest.fixture
def comparisons(tmp_path):
    return CountingComparisons(ComparisonStore(connect(tmp_path / "reviewer.db")))


class OrchWith(Orchestrator):
    """An Orchestrator with the project "acme" and its repo "app" on ticket_flow "both"; `others` are
    projects listed before it."""

    def __init__(self, comparisons, others=None):  # the real __init__ builds a runtime these tests do not need
        self.repo_cfg = RepoConfig(repo=RepoInfo(id="app"), ticket_flow="both",
                                   telegram=TelegramConfig(default_chat_id="-100"))
        self._projects = {**(others or {}), "acme": LoadedProject(repos={"app": self.repo_cfg})}
        self._trackers, self._repo_vcs, self._vcs = {}, {}, None
        if comparisons is not None:
            self.set_comparisons(comparisons)

    def workspace(self, ticket_id, started_at, company_id="acme", repo_id="app"):
        return Workspace("/nonexistent", WorkspaceState(ticket_id=ticket_id, company_id=company_id, repo_id=repo_id,
                                                        workspace_root="/nonexistent", started_at=started_at))


@pytest.fixture
def orch_with(comparisons):
    return OrchWith(comparisons)


async def test_compared_pr_is_a_draft_with_the_mark_and_no_tracker_write(ws, vcs, tracker, repo_cfg):
    result = await create_pr(ws, vcs, tracker, repo_cfg, compared=True)
    assert result.success
    assert vcs.opened == [("ACME-1: Fix login [staged]", "develop", True)]   # (title, base, draft)
    assert tracker.calls == []

async def test_uncompared_pr_is_unchanged(ws, vcs, tracker, repo_cfg):
    await create_pr(ws, vcs, tracker, repo_cfg)
    assert vcs.opened == [("ACME-1: Fix login", "develop", False)]
    assert vcs.draft_passed == [False]                              # the call as today: no draft argument
    assert tracker.calls == [("transition", "ACME-1", "In Review"), ("comment", "ACME-1", "PR opened: u")]

async def test_long_title_is_cut_before_the_mark(ws_long_title, vcs, tracker, repo_cfg):
    await create_pr(ws_long_title, vcs, tracker, repo_cfg, compared=True)
    title = vcs.opened[0][0]
    assert len(title) == 256 and title.endswith("… [staged]")

async def test_tracker_writes_report_each_failure(repo_cfg):
    tracker = FakeTracker(fail={"transition"})
    assert await tracker_writes_for_pr(tracker, repo_cfg, "ACME-1", "u") == ["move to In Review failed: RuntimeError"]
    assert tracker.calls == [("comment", "ACME-1", "PR opened: u")]

async def test_tracker_writes_report_a_failed_comment(repo_cfg):
    tracker = FakeTracker(fail={"comment"})
    assert await tracker_writes_for_pr(tracker, repo_cfg, "ACME-1", "u") == ['"PR opened" comment failed: RuntimeError']
    assert tracker.calls == [("transition", "ACME-1", "In Review")]

async def test_tracker_writes_report_both_failures_in_order(repo_cfg):
    tracker = FakeTracker(fail={"transition", "comment"})
    assert await tracker_writes_for_pr(tracker, repo_cfg, "ACME-1", "u") == [
        "move to In Review failed: RuntimeError", '"PR opened" comment failed: RuntimeError']
    assert tracker.calls == []

async def test_tracker_writes_report_nothing_when_both_land(repo_cfg, tracker):
    assert await tracker_writes_for_pr(tracker, repo_cfg, "ACME-1", "u") == []

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
    assert text.startswith(tg_header("🔗", "acme", "ACME-1", "Fix login") + "\n")
    state = rig_action_stage.ws.state
    assert (state.escalation_msg_id, state.escalation_chat_id, state.human_input_reply) == (5, "-7", "reviewed")

def test_is_compared_needs_both_and_a_row(orch_with, comparisons):
    ws = orch_with.workspace("ACME-1", started_at="T0")
    assert orch_with._is_compared(ws) is False                      # no row
    comparisons.add("acme/app", "ACME-1", "run1", NOW, workspace_started_at="T0")
    assert orch_with._is_compared(ws) is True
    orch_with.repo_cfg.ticket_flow = "staged"
    assert orch_with._is_compared(ws) is False                      # staged: no lookup
    assert comparisons.lookups == 2                                 # the two "both" calls only

def test_is_compared_is_false_without_a_store():
    orch = OrchWith(None)
    assert orch._is_compared(orch.workspace("ACME-1", started_at="T0")) is False

def test_a_store_error_propagates(orch_with):
    class BrokenStore:
        def for_workspace(self, *_args):
            raise sqlite3.OperationalError("database is locked")
    orch_with.set_comparisons(BrokenStore())
    with pytest.raises(sqlite3.OperationalError):
        orch_with._is_compared(orch_with.workspace("ACME-1", started_at="T0"))

def test_an_unknown_project_or_repo_is_not_compared(orch_with, comparisons):
    comparisons.add("acme/gone", "ACME-1", "run1", NOW, workspace_started_at="T0")
    comparisons.add("gone/app", "ACME-1", "run2", NOW, workspace_started_at="T0")
    assert orch_with._is_compared(orch_with.workspace("ACME-1", started_at="T0", repo_id="gone")) is False
    assert orch_with._is_compared(orch_with.workspace("ACME-1", started_at="T0", company_id="gone")) is False
    assert comparisons.lookups == 0

def test_the_repo_config_is_the_workspace_projects(comparisons):
    # "other" also has a repo "app", on staged, and is listed first.
    staged = RepoConfig(repo=RepoInfo(id="app"))
    orch = OrchWith(comparisons, others={"other": LoadedProject(repos={"app": staged})})
    comparisons.add("acme/app", "ACME-1", "run1", NOW, workspace_started_at="T0")
    assert orch._is_compared(orch.workspace("ACME-1", started_at="T0")) is True
    comparisons.add("other/app", "ACME-2", "run2", NOW, workspace_started_at="T0")
    assert orch._is_compared(orch.workspace("ACME-2", started_at="T0", company_id="other")) is False


@pytest.fixture
def wired(orch_with, monkeypatch):
    """_handle_action_stage with handle_action_stage replaced by one that keeps its arguments, and the two
    actions replaced where the orchestrator's shims import them by ones that keep `compared`."""
    seen = {"handle": {}, "push": [], "fetch": []}

    async def handle(workspace, stage_id, stage_def, **kwargs):
        seen["handle"] = kwargs

    async def push(*_args, compared):
        seen["push"].append(compared)

    async def fetch(*_args, compared, **_kwargs):
        seen["fetch"].append(compared)

    monkeypatch.setattr(action_stage, "handle_action_stage", handle)
    monkeypatch.setattr(orchestrator_module, "action_push_and_open_pr", push)
    monkeypatch.setattr(orchestrator_module, "action_fetch_pr_comments", fetch)
    monkeypatch.setattr(orchestrator_module, "hand_over_to_reviewer", lambda ws: seen["fetch"].append("handed over"))

    async def run(ws):
        stage_def = SimpleNamespace(action="push_and_open_pr")
        await orch_with._handle_action_stage(ws, "push", stage_def)
        await seen["handle"]["action_push_and_open_pr"](ws)
        await seen["handle"]["action_fetch_pr_comments"](ws, stage_def)
        return seen
    return run

@pytest.mark.parametrize("flow, row, compared", [("both", True, True), ("both", False, False),
                                                 ("staged", True, False)])
async def test_the_shims_pass_whether_the_ticket_is_compared(orch_with, comparisons, wired, flow, row, compared):
    orch_with.repo_cfg.ticket_flow = flow
    if row:
        comparisons.add("acme/app", "ACME-1", "run1", NOW, workspace_started_at="T0")
    seen = await wired(orch_with.workspace("ACME-1", started_at="T0"))
    assert seen["push"] == [compared] and seen["fetch"] == [compared]
    assert seen["handle"]["is_compared"] == orch_with._is_compared


# --- the code-review fix wave ---

@pytest.mark.parametrize("state, compared", [("open", True), ("deciding", True), ("stuck", True), ("closed", False)])
async def test_a_workspace_is_compared_only_until_its_verdict_closes(orch_with, comparisons, wired, state, compared):
    # A closed comparison's workspace taken up again (a dashboard Rerun, same started_at) is the staged flow's
    # again: a ready PR with its tracker writes, and its review comments processed.
    c = comparisons.add("acme/app", "ACME-1", "run1", NOW, workspace_started_at="T0")
    if state != "open":
        comparisons.claim(c.id, ("open",), NOW, state, verdict="staged")
    ws = orch_with.workspace("ACME-1", started_at="T0")
    assert orch_with._is_compared(ws) is compared
    seen = await wired(ws)
    assert seen["push"] == [compared] and seen["fetch"] == [compared]


# --- reviewer mode owns the repo's PRs (merged from master) ---

@pytest.mark.parametrize("row, fetched", [(True, [True]), (False, ["handed over"])])
async def test_a_compared_ticket_is_not_handed_over_to_reviewer_mode(orch_with, comparisons, wired, row, fetched):
    orch_with.set_reviewed_repos(frozenset({"acme/app"}))
    if row:
        comparisons.add("acme/app", "ACME-1", "run1", NOW, workspace_started_at="T0")
    seen = await wired(orch_with.workspace("ACME-1", started_at="T0"))
    assert seen["fetch"] == fetched
    assert seen["handle"]["reviewer_owns"] is True


@pytest.mark.parametrize("compared, sent", [(True, True), (False, False)])
async def test_a_compared_pr_message_is_sent_where_reviewer_mode_owns_the_prs(rig_action_stage, compared, sent):
    await rig_action_stage.push(compared=compared, reviewer_owns=True)
    texts = [text for text, _buttons in rig_action_stage.notifier.sent]
    assert any("Draft PR opened: u" in t for t in texts) is sent
    assert not any("Review Complete" in t for t in texts)


def test_compared_tickets_are_the_open_comparisons_ones(orch_with, comparisons):
    comparisons.add("acme/app", "ACME-1", "run1", NOW, workspace_started_at="T0")
    assert orch_with.compared_tickets("acme/app") == frozenset({"ACME-1"})
    assert OrchWith(None).compared_tickets("acme/app") == frozenset()
