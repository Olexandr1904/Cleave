"""Tests for action-stage execution path in the orchestrator."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from orchestrator.stage_verifier import ActionResult
from workspace.workspace import Stage


def _fake_workspace(
    ticket_id: str = "T-1",
    state: str = Stage.PUSHED,
    previous: str | None = Stage.QA,
    branch: str = "feature/t-1",
    pr_url: str | None = None,
    pr_number: int | None = None,
) -> MagicMock:
    ws = MagicMock()
    ws.state = SimpleNamespace(
        ticket_id=ticket_id,
        company_id="test-co",
        repo_id="test-repo",
        current_state=state,
        previous_state=previous,
        branch=branch,
        pr_url=pr_url,
        pr_number=pr_number,
        stage_iterations={},
        error=None,
        human_input_reply=None,
        last_updated_at=None,
        pending_review_comments=None,
        review_cycle=0,
    )
    ws.source_dir = "/tmp/fake/source"
    ws.reports_dir = MagicMock()
    return ws


class TestActionPushAndOpenPr:
    @pytest.mark.asyncio
    async def test_returns_action_result_on_success(self):
        from orchestrator.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        orch._get_vcs_for_workspace = MagicMock(
            return_value=(MagicMock(), MagicMock()),
        )
        orch._tracker = MagicMock()
        orch._events = None
        pr_result = SimpleNamespace(
            success=True, pr_url="https://github.com/x/1", pr_number=42, error="",
        )
        with patch("orchestrator.orchestrator.create_pr", new=AsyncMock(return_value=pr_result)):
            result = await Orchestrator._action_push_and_open_pr(orch, _fake_workspace())

        assert isinstance(result, ActionResult)
        assert result.success is True
        assert result.next_state == Stage.PR_REVIEW
        assert result.metadata == {"pr_url": "https://github.com/x/1", "pr_number": 42}

    @pytest.mark.asyncio
    async def test_returns_failure_when_no_vcs(self):
        from orchestrator.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        orch._get_vcs_for_workspace = MagicMock(return_value=(None, None))

        result = await Orchestrator._action_push_and_open_pr(orch, _fake_workspace())

        assert isinstance(result, ActionResult)
        assert result.success is False
        assert "VCS" in result.error

    @pytest.mark.asyncio
    async def test_does_not_transition_state(self):
        from orchestrator.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        orch._get_vcs_for_workspace = MagicMock(
            return_value=(MagicMock(), MagicMock()),
        )
        orch._tracker = MagicMock()
        orch._events = None
        pr_result = SimpleNamespace(
            success=True, pr_url="https://github.com/x/1", pr_number=42, error="",
        )
        ws = _fake_workspace()
        with patch("orchestrator.orchestrator.create_pr", new=AsyncMock(return_value=pr_result)):
            await Orchestrator._action_push_and_open_pr(orch, ws)

        ws.transition.assert_not_called()


class TestActionFetchPrComments:
    def _make_stage_def(self, delay_minutes: int = 0) -> SimpleNamespace:
        return SimpleNamespace(delay_minutes=delay_minutes)

    @pytest.mark.asyncio
    async def test_returns_skipped_when_no_reviewed_signal(self):
        from orchestrator.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        ws = _fake_workspace(state=Stage.PR_REVIEW, pr_number=10)
        stage_def = self._make_stage_def()

        result = await Orchestrator._action_fetch_pr_comments(orch, ws, stage_def)

        assert isinstance(result, ActionResult)
        assert result.skipped is True

    @pytest.mark.asyncio
    async def test_returns_done_when_no_pr_number(self):
        from orchestrator.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        ws = _fake_workspace(state=Stage.PR_REVIEW, pr_number=None)

        result = await Orchestrator._action_fetch_pr_comments(orch, ws, self._make_stage_def())

        assert result.success is True
        assert result.next_state == Stage.DONE

    @pytest.mark.asyncio
    async def test_returns_done_when_no_comments(self):
        from orchestrator.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        orch._get_vcs_for_workspace = MagicMock(return_value=(MagicMock(), MagicMock()))
        vcs = orch._get_vcs_for_workspace.return_value[0]
        vcs.get_pr_comments = AsyncMock(return_value=[])
        orch._registry = MagicMock()
        orch._registry.get_agent = MagicMock(return_value=None)

        ws = _fake_workspace(state=Stage.PR_REVIEW, pr_number=10)
        ws.state.human_input_reply = "reviewed"
        result = await Orchestrator._action_fetch_pr_comments(orch, ws, self._make_stage_def())

        assert result.success is True
        assert result.next_state == Stage.DONE

    @pytest.mark.asyncio
    async def test_does_not_transition_state(self):
        from orchestrator.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        ws = _fake_workspace(state=Stage.PR_REVIEW, pr_number=None)

        await Orchestrator._action_fetch_pr_comments(orch, ws, self._make_stage_def())

        ws.transition.assert_not_called()


class TestActionFinalize:
    @pytest.mark.asyncio
    async def test_returns_done(self):
        from orchestrator.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        orch._notifier = None
        orch._tracker = None
        ws = _fake_workspace(state=Stage.PR_REVIEW, pr_url="https://github.com/x/1")

        result = await Orchestrator._action_finalize(orch, ws)

        assert isinstance(result, ActionResult)
        assert result.success is True
        assert result.next_state == Stage.DONE

    @pytest.mark.asyncio
    async def test_does_not_transition_state(self):
        from orchestrator.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        orch._notifier = None
        orch._tracker = None
        ws = _fake_workspace()

        await Orchestrator._action_finalize(orch, ws)

        ws.transition.assert_not_called()


class TestHandleActionStage:
    def _make_orchestrator(self):
        from orchestrator.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        orch._dry_run = False
        orch._emit = MagicMock()
        orch._mode_handler = None
        orch._should_approval_gate = MagicMock(return_value=False)
        orch._rollback_iteration = MagicMock()
        orch._notifier = None
        return orch

    def _make_stage_def(self, action: str) -> SimpleNamespace:
        return SimpleNamespace(action=action, delay_minutes=0, max_iterations=0)

    @pytest.mark.asyncio
    async def test_happy_path_push(self):
        from orchestrator.orchestrator import Orchestrator

        orch = self._make_orchestrator()
        ws = _fake_workspace()
        orch._action_push_and_open_pr = AsyncMock(return_value=ActionResult(
            success=True, next_state=Stage.PR_REVIEW, error="",
            metadata={"pr_url": "https://github.com/x/1", "pr_number": 42},
        ))

        with patch("orchestrator.orchestrator.stage_verifier") as sv:
            sv.capture_stage_start = MagicMock(return_value="abc123")
            sv.verify = MagicMock(return_value=SimpleNamespace(ok=True, stage_id="push", reason=""))
            await Orchestrator._handle_action_stage(orch, ws, "push", self._make_stage_def("push_and_open_pr"))

        ws.update_state.assert_any_call(pr_url="https://github.com/x/1", pr_number=42)
        ws.transition.assert_called_with(Stage.PR_REVIEW)
        orch._emit.assert_any_call(
            "action_completed", ANY,
            project_id="test-co", ticket_id="T-1",
            data={"stage": "push", "pr_url": "https://github.com/x/1", "pr_number": 42},
        )

    @pytest.mark.asyncio
    async def test_action_failure_transitions_to_failed(self):
        from orchestrator.orchestrator import Orchestrator

        orch = self._make_orchestrator()
        ws = _fake_workspace()
        orch._action_push_and_open_pr = AsyncMock(return_value=ActionResult(
            success=False, next_state="", error="No VCS configured", metadata={},
        ))

        with patch("orchestrator.orchestrator.stage_verifier") as sv:
            sv.capture_stage_start = MagicMock(return_value="abc123")
            await Orchestrator._handle_action_stage(orch, ws, "push", self._make_stage_def("push_and_open_pr"))
            sv.verify.assert_not_called()

        ws.transition.assert_called_once_with(Stage.FAILED)
        ws.update_state.assert_called_once_with(error="No VCS configured")

    @pytest.mark.asyncio
    async def test_verify_failure_transitions_to_blocked(self):
        from orchestrator.orchestrator import Orchestrator

        orch = self._make_orchestrator()
        ws = _fake_workspace()
        orch._action_push_and_open_pr = AsyncMock(return_value=ActionResult(
            success=True, next_state=Stage.PR_REVIEW, error="",
            metadata={"pr_url": "https://github.com/x/1", "pr_number": 42},
        ))

        with patch("orchestrator.orchestrator.stage_verifier") as sv:
            sv.capture_stage_start = MagicMock(return_value="abc123")
            sv.verify = MagicMock(return_value=SimpleNamespace(
                ok=False, stage_id="push",
                reason="remote has no ref refs/heads/feature/t-1 (branch not pushed)",
            ))
            await Orchestrator._handle_action_stage(orch, ws, "push", self._make_stage_def("push_and_open_pr"))

        ws.transition.assert_called_once_with(Stage.BLOCKED)
        assert "branch not pushed" in ws.update_state.call_args[1]["error"]
        orch._emit.assert_any_call(
            "stage_verification_failed", ANY,
            project_id="test-co", ticket_id="T-1",
            data={"stage": "push", "reason": "remote has no ref refs/heads/feature/t-1 (branch not pushed)"},
        )

    @pytest.mark.asyncio
    async def test_skipped_action_no_transition(self):
        from orchestrator.orchestrator import Orchestrator

        orch = self._make_orchestrator()
        ws = _fake_workspace(state=Stage.PR_REVIEW, pr_number=10)
        orch._action_fetch_pr_comments = AsyncMock(return_value=ActionResult(
            success=False, next_state="", error="", metadata={}, skipped=True,
        ))

        with patch("orchestrator.orchestrator.stage_verifier") as sv:
            sv.capture_stage_start = MagicMock(return_value=None)
            await Orchestrator._handle_action_stage(orch, ws, "pr_review", self._make_stage_def("fetch_pr_comments"))
            sv.verify.assert_not_called()

        ws.transition.assert_not_called()
        orch._rollback_iteration.assert_called_once_with(ws, "pr_review")

    @pytest.mark.asyncio
    async def test_approval_gate_after_verify(self):
        from orchestrator.orchestrator import Orchestrator

        orch = self._make_orchestrator()
        orch._should_approval_gate = MagicMock(return_value=True)
        orch._notifier = None
        ws = _fake_workspace(state=Stage.PR_REVIEW, pr_number=10)
        orch._action_fetch_pr_comments = AsyncMock(return_value=ActionResult(
            success=True, next_state=Stage.DONE, error="", metadata={},
        ))

        with patch("orchestrator.orchestrator.stage_verifier") as sv:
            sv.capture_stage_start = MagicMock(return_value=None)
            sv.verify = MagicMock(return_value=SimpleNamespace(ok=True, stage_id="pr_review", reason=""))
            await Orchestrator._handle_action_stage(orch, ws, "pr_review", self._make_stage_def("fetch_pr_comments"))

        ws.transition.assert_called_once_with(Stage.AWAITING_APPROVAL)
        orch._emit.assert_any_call(
            "approval_requested", ANY,
            project_id="test-co", ticket_id="T-1",
            data={"gate": "PR_REVIEW"},
        )
