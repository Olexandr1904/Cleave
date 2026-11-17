# tests/unit/test_skillflow_intake.py
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from config.schemas import RepoConfig, RepoInfo, SkillFlowConfig, TelegramConfig
from integrations.base.tracker import TicketData
from orchestrator.reviewer.db import connect
from orchestrator.skillflow import intake as intake_module
from orchestrator.skillflow.intake import SkillIntake, choose_skill
from orchestrator.skillflow.store import SkillStore
from workspace.workspace import Stage

NOW = "2026-11-16T10:00:00+00:00"
T0 = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def rig(tmp_path):
    meta = tmp_path / "ws" / "meta"
    (meta / "attachments").mkdir(parents=True)
    (meta / "ticket.md").write_text("# ACME-1")
    (meta / "attachments" / "a.png").write_bytes(b"png")
    ws = SimpleNamespace(meta_dir=meta, state=SimpleNamespace(
        ticket_id="ACME-1", repo_id="app", branch="feature/ACME-1-acme-1", model="claude-opus-5-5",
        current_state=Stage.ANALYSIS))
    repo_cfg = RepoConfig(repo=RepoInfo(id="app"), ticket_flow="both",
                          telegram=TelegramConfig(default_chat_id="-100"),
                          skill_flow=SkillFlowConfig(bug_skill="fix-this", task_skill="implement-this"))
    r = SimpleNamespace(ws=ws, repo_cfg=repo_cfg, store=SkillStore(connect(tmp_path / "reviewer.db")),
                        runs_dir=tmp_path / "runs", outside=tmp_path, woken=0)

    def wake():
        r.woken += 1

    intake = SkillIntake(r.store, r.runs_dir, lambda cfg: cfg.telegram.default_chat_id, wake, now=lambda: T0)

    def create(**ticket_fields):
        ticket = TicketData(id="ACME-1", url="https://acme.example/browse/ACME-1", summary="Login crashes",
                            description="", assignee="Ann", status="To Do", **ticket_fields)
        return intake.create(ws, ticket, "acme", repo_cfg)

    r.create = create
    return r


def test_choose_skill():
    cfg = SkillFlowConfig(bug_skill="fix-this", task_skill="implement-this", bug_types=["Bug", "Defect"])
    assert [choose_skill(cfg, t) for t in ("Bug", "defect", "Story", "")] == ["fix-this", "fix-this", "implement-this", "implement-this"]

def test_both_creates_a_run_with_the_ticket_copy(rig):
    run = rig.create(issue_type="Bug")
    assert (run.state, run.skill, run.scope, run.ticket_id) == ("queued", "fix-this", "acme/app", "ACME-1")
    assert (run.model, run.summary, run.chat_id) == ("claude-opus-5-5", "Login crashes", "-100")
    assert run.facts == {"assignee": "Ann", "status": "To Do", "staged_branch": "feature/ACME-1-acme-1"}
    copy = rig.runs_dir / run.id / "ticket"
    assert (copy / "ticket.md").read_text() == "# ACME-1" and (copy / "attachments" / "a.png").exists()
    assert rig.woken == 1

def test_the_copy_is_a_snapshot(rig):
    run = rig.create()
    (rig.ws.meta_dir / "ba-analysis.md").write_text("staged agent output")
    assert not (rig.runs_dir / run.id / "ticket" / "ba-analysis.md").exists()

def test_staged_flow_creates_nothing(rig):
    rig.repo_cfg.ticket_flow = "staged"
    assert rig.create() is None and rig.store.runs_for_ticket("acme/app", "ACME-1") == []

def test_a_workspace_resumed_from_an_existing_pr_gets_no_run(rig):
    rig.ws.state.current_state = Stage.PR_REVIEW
    assert rig.create() is None

def test_a_repo_no_runner_serves_gets_no_run(rig):
    """A run no runner would ever take is never made: no row, no copy, no wake."""
    asked, woken = [], []
    intake = SkillIntake(rig.store, rig.runs_dir, lambda cfg: "-100", lambda: woken.append(1), now=lambda: T0,
                         serves=lambda scope: asked.append(scope) or False)
    ticket = TicketData(id="ACME-1", url="https://acme.example/browse/ACME-1", summary="Login crashes",
                        description="")
    assert intake.create(rig.ws, ticket, "acme", rig.repo_cfg) is None
    assert asked == ["acme/app"]
    assert rig.store.runs_for_ticket("acme/app", "ACME-1") == []
    assert not (rig.runs_dir.exists() and any(rig.runs_dir.iterdir())) and woken == []

@pytest.mark.parametrize("state, created", [("waiting", False), ("pr_open", False), ("no_change", False),
                                            ("dropped", True), ("failed", True)])
def test_one_live_run_per_ticket(rig, state, created):
    first = rig.create(); first.state = state; rig.store.save(first, NOW)
    assert (rig.create() is not None) is created

def test_the_copy_never_follows_a_symlink(rig):
    (rig.outside / "secret.txt").write_text("not a ticket file")
    (rig.outside / "secrets").mkdir()
    (rig.ws.meta_dir / "linked.md").symlink_to(rig.outside / "secret.txt")
    (rig.ws.meta_dir / "attachments" / "linked").symlink_to(rig.outside / "secrets")
    copy = rig.runs_dir / rig.create().id / "ticket"
    for name in ("linked.md", "attachments/linked"):
        assert not (copy / name).exists() and not (copy / name).is_symlink()
    assert (copy / "ticket.md").exists()

@pytest.mark.parametrize("failing", ["copy", "store"])
def test_a_failure_half_way_leaves_neither_a_run_nor_a_copy(rig, monkeypatch, failing):
    def boom(*_args, **_kwargs):
        raise OSError("disk full")
    if failing == "copy":
        monkeypatch.setattr(intake_module.shutil, "copytree", boom)
    else:
        monkeypatch.setattr(rig.store, "add_run", boom)
    with pytest.raises(OSError):
        rig.create()
    assert rig.store.runs_for_ticket("acme/app", "ACME-1") == []
    assert not (rig.runs_dir.exists() and any(rig.runs_dir.iterdir())) and rig.woken == 0

def test_a_copy_that_cannot_take_the_runs_name_ends_the_run(rig, monkeypatch):
    """The run is stored before its copy is renamed: a failed rename must not leave it queued without its files."""
    def boom(*_args, **_kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(intake_module.Path, "rename", boom)
    with pytest.raises(OSError, match="disk full"):
        rig.create()
    monkeypatch.undo()
    [run] = rig.store.runs_for_ticket("acme/app", "ACME-1")
    assert (run.state, run.reason) == ("failed", "ticket copy not stored: OSError")
    assert not any(rig.runs_dir.iterdir()) and rig.woken == 0
    assert rig.create() is not None                                     # an ended run lets the ticket in again
