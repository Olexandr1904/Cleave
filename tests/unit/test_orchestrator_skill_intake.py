# tests/unit/test_orchestrator_skill_intake.py
from __future__ import annotations

from types import SimpleNamespace

import pytest

from config.schemas import RepoConfig, RepoInfo
from integrations.base.tracker import TicketData
from orchestrator import ingest
from orchestrator.orchestrator import Orchestrator
from orchestrator.ticket_prioritizer import PrioritizedTicket

PT = PrioritizedTicket(TicketData(id="ACME-1", url="https://acme.example/browse/ACME-1", summary="Login crashes",
                                  description=""), repo_id="app", project_id="acme")
REPO_CFG = RepoConfig(repo=RepoInfo(id="app"), ticket_flow="both")
WS = SimpleNamespace(name="the new workspace")


@pytest.fixture
def orch(monkeypatch):
    async def create_workspace_for_ticket(*_args, **_kwargs):
        return WS
    monkeypatch.setattr(ingest, "create_workspace_for_ticket", create_workspace_for_ticket)
    orch = Orchestrator.__new__(Orchestrator)
    orch._workspace_manager = None
    orch._trackers = {}
    return orch


def boom(*_args):
    raise OSError("disk full")


async def test_create_workspace_calls_the_intake(orch, monkeypatch):
    seen = []
    orch.set_skill_intake(SimpleNamespace(create=lambda ws, t, p, r: seen.append((ws, t.id, p))))
    ws = await orch._create_workspace(PT, "acme", REPO_CFG)        # create_workspace_for_ticket patched to return WS
    assert seen == [(ws, "ACME-1", "acme")]

async def test_a_failing_intake_does_not_fail_the_workspace(orch, caplog):
    orch.set_skill_intake(SimpleNamespace(create=boom))
    assert await orch._create_workspace(PT, "acme", REPO_CFG) is WS
    assert "Skill run not created for ACME-1" in caplog.text

async def test_without_an_intake_the_workspace_is_still_created(orch):
    assert await orch._create_workspace(PT, "acme", REPO_CFG) is WS
