from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workspace.workspace import Stage


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "a.txt").write_text("a")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


@pytest.mark.asyncio
async def test_dev_stage_without_new_commit_goes_to_blocked(tmp_path):
    """Reproducer for silent-drift bug: dev agent ran, said 'Tests pass',
    but made no commit. Workspace must land in BLOCKED, not advance."""
    from orchestrator.orchestrator import Orchestrator

    repo = _init_repo(tmp_path)

    ws = MagicMock()
    ws.source_dir = repo
    ws.reports_dir = tmp_path / "reports"
    ws.reports_dir.mkdir()
    ws.state = SimpleNamespace(
        ticket_id="T-1",
        company_id="acme",
        repo_id="acme-app",
        current_state="DEV",
        previous_state="ANALYSIS",
        stage_iterations={},
        branch="feature/t-1",
        error=None,
    )
    ws.transition = MagicMock()
    ws.update_state = MagicMock()
    ws.increment_iteration = MagicMock()

    workflow = MagicMock()
    stage_def = SimpleNamespace(agent="dev-agent", action=None, max_iterations=3)
    workflow.stages = {"dev": stage_def}

    orch = Orchestrator.__new__(Orchestrator)
    orch._workflow = workflow
    orch._dry_run = False
    orch._events = None
    orch._notifier = None
    orch._agent_runtime = MagicMock()
    orch._agent_runtime.execute = AsyncMock(
        return_value=SimpleNamespace(
            success=True, output="Tests pass", duration_seconds=1.0,
            input_tokens=0, output_tokens=0, failure_kind=None, error=None, retry_at=None,
        )
    )
    orch._get_repo_config = MagicMock(return_value=None)
    orch._emit = MagicMock()
    orch._parse_agent_outcome = MagicMock(return_value="default")
    orch._should_approval_gate = MagicMock(return_value=False)
    orch._advance_to_stage = MagicMock()

    await orch._handle_agent_stage(ws, "dev", stage_def)

    ws.transition.assert_called_once()
    args, kwargs = ws.transition.call_args
    assert args[0] == "BLOCKED"
    orch._advance_to_stage.assert_not_called()
    assert ws.update_state.called
    error_arg = ws.update_state.call_args.kwargs.get("error", "")
    assert "commit" in error_arg.lower()


@pytest.mark.asyncio
async def test_dev_stage_with_new_commit_advances_normally(tmp_path):
    from orchestrator.orchestrator import Orchestrator

    repo = _init_repo(tmp_path)

    async def fake_execute(*a, **kw):
        (repo / "b.txt").write_text("b")
        subprocess.run(["git", "add", "b.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "feat"], cwd=repo, check=True)
        return SimpleNamespace(
            success=True, output="Tests pass", duration_seconds=1.0,
            input_tokens=0, output_tokens=0, failure_kind=None, error=None, retry_at=None,
        )

    ws = MagicMock()
    ws.source_dir = repo
    ws.reports_dir = tmp_path / "reports"
    ws.reports_dir.mkdir()
    ws.state = SimpleNamespace(
        ticket_id="T-1", company_id="acme", repo_id="acme-app",
        current_state="DEV", previous_state="ANALYSIS",
        stage_iterations={}, branch="feature/t-1", error=None,
    )
    ws.transition = MagicMock()
    ws.update_state = MagicMock()
    ws.increment_iteration = MagicMock()

    workflow = MagicMock()
    stage_def = SimpleNamespace(agent="dev-agent", action=None, max_iterations=3)
    workflow.stages = {"dev": stage_def}

    orch = Orchestrator.__new__(Orchestrator)
    orch._workflow = workflow
    orch._dry_run = False
    orch._events = None
    orch._notifier = None
    orch._agent_runtime = MagicMock()
    orch._agent_runtime.execute = AsyncMock(side_effect=fake_execute)
    orch._get_repo_config = MagicMock(return_value=None)
    orch._emit = MagicMock()
    orch._parse_agent_outcome = MagicMock(return_value="default")
    orch._should_approval_gate = MagicMock(return_value=False)
    orch._advance_to_stage = MagicMock()

    with patch("orchestrator.orchestrator.get_next_stage", return_value="scope_check"):
        await orch._handle_agent_stage(ws, "dev", stage_def)

    orch._advance_to_stage.assert_called_once()
    blocked_calls = [c for c in ws.transition.call_args_list if c.args and c.args[0] == "BLOCKED"]
    assert blocked_calls == []


@pytest.mark.asyncio
async def test_verification_fail_notifies_telegram_and_sets_escalation_fields(tmp_path):
    """When stage verification fails, the workspace goes BLOCKED and a TG
    notification is sent with escalation_msg_id/chat_id populated so the
    existing reply flow can unblock it."""
    from orchestrator.orchestrator import Orchestrator

    repo = _init_repo(tmp_path)

    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "dev-agent-output.md").write_text(
        "---\n**Attempt: 2026-04-24 14:00 UTC**\n## Decision: Proceed\n"
        "\nTests pass but no commit was made.\n"
    )

    ws = MagicMock()
    ws.source_dir = repo
    ws.reports_dir = reports_dir
    ws.meta_dir = tmp_path / "meta"
    ws.meta_dir.mkdir()
    ws.state = SimpleNamespace(
        ticket_id="T-1",
        company_id="acme",
        repo_id="acme-app",
        current_state="DEV",
        previous_state="ANALYSIS",
        stage_iterations={},
        branch="feature/t-1",
        error=None,
        escalation_msg_id=None,
        escalation_chat_id=None,
        human_input_question=None,
    )
    ws.transition = MagicMock()
    ws.update_state = MagicMock()
    ws.increment_iteration = MagicMock()
    ws.save_state = MagicMock()

    workflow = MagicMock()
    stage_def = SimpleNamespace(agent="dev-agent", action=None, max_iterations=3)
    workflow.stages = {"dev": stage_def}

    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=777)

    orch = Orchestrator.__new__(Orchestrator)
    orch._workflow = workflow
    orch._dry_run = False
    orch._events = None
    orch._notifier = notifier
    orch._agent_runtime = MagicMock()
    orch._agent_runtime.execute = AsyncMock(
        return_value=SimpleNamespace(
            success=True, output="Tests pass", duration_seconds=1.0,
            input_tokens=0, output_tokens=0, failure_kind=None, error=None, retry_at=None,
        )
    )
    orch._get_repo_config = MagicMock(return_value=None)
    orch._emit = MagicMock()
    orch._parse_agent_outcome = MagicMock(return_value="default")
    orch._should_approval_gate = MagicMock(return_value=False)
    orch._advance_to_stage = MagicMock()
    orch._get_chat_id = MagicMock(return_value="chat-1")
    orch._get_ticket_title = MagicMock(return_value="A ticket")
    orch._tg_header = MagicMock(return_value="⚠️ [acme/acme-app] T-1")
    orch._log_pipeline = MagicMock()

    await orch._handle_agent_stage(ws, "dev", stage_def)

    # Workspace transitioned to BLOCKED.
    blocked_calls = [c for c in ws.transition.call_args_list if c.args and c.args[0] == "BLOCKED"]
    assert blocked_calls, "workspace must transition to BLOCKED"

    # TG notification sent.
    notifier.send_message.assert_awaited_once()
    args, kwargs = notifier.send_message.call_args
    message = args[1] if len(args) >= 2 else kwargs.get("message", "")
    assert "verification failed" in message.lower()
    assert kwargs.get("buttons") is None or "buttons" not in kwargs

    # Escalation fields populated on the workspace for reply routing.
    assert ws.state.escalation_msg_id == 777
    assert ws.state.escalation_chat_id == "chat-1"
    human_input_calls = [
        c for c in ws.update_state.call_args_list
        if "human_input_question" in c.kwargs
    ]
    assert human_input_calls, "update_state must set human_input_question"
    stored = human_input_calls[-1].kwargs["human_input_question"]
    assert "verification failed" in stored.lower()
