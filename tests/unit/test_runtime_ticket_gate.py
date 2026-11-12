from __future__ import annotations
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from orchestrator.runtime import Runtime
from workspace.workspace import Stage

def _ws(repo):
    return SimpleNamespace(state=SimpleNamespace(company_id="acme", repo_id=repo, current_state=Stage.DEV, ticket_id=repo))

async def test_poll_cycle_does_not_advance_paused_repo():
    advance = AsyncMock()
    wm = MagicMock()
    wm.cleanup_old_workspaces.return_value = []
    rt = Runtime(SimpleNamespace(defaults=SimpleNamespace(max_parallel_tickets=3, max_parallel_builds=1, poll_interval_seconds=60),
                                 workspaces=SimpleNamespace(max_age_days=7)),
                 wm, poll_callback=AsyncMock(), advance_callback=advance,
                 ticket_gate=lambda p, r: r == "b")
    rt.reconcile_disk_workspaces = lambda: None
    rt._active_workspaces = [_ws("a"), _ws("b")]
    await rt.poll_cycle()
    assert [c.args[0].state.repo_id for c in advance.await_args_list] == ["b"]
