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
        from orchestrator.pipeline.actions.push_and_open_pr import action_push_and_open_pr

        pr_result = SimpleNamespace(
            success=True, pr_url="https://github.com/x/1", pr_number=42, error="",
        )
        mod = "orchestrator.pipeline.actions.push_and_open_pr"
        with patch(f"{mod}.create_pr", new=AsyncMock(return_value=pr_result)), \
             patch(f"{mod}.ensure_branch_has_commits"), \
             patch(f"{mod}.commit_pipeline_artifacts"), \
             patch(f"{mod}.squash_feature_commits"):
            result = await action_push_and_open_pr(
                _fake_workspace(), MagicMock(), MagicMock(), None, "", MagicMock(), None,
            )

        assert isinstance(result, ActionResult)
        assert result.success is True
        assert result.next_state == Stage.PR_REVIEW
        assert result.metadata == {"pr_url": "https://github.com/x/1", "pr_number": 42}

    @pytest.mark.asyncio
    async def test_returns_failure_when_no_vcs(self):
        from orchestrator.pipeline.actions.push_and_open_pr import action_push_and_open_pr

        result = await action_push_and_open_pr(
            _fake_workspace(), None, None, None, "", MagicMock(), None,
        )

        assert isinstance(result, ActionResult)
        assert result.success is False
        assert "VCS" in result.error

    @pytest.mark.asyncio
    async def test_does_not_transition_state(self):
        from orchestrator.pipeline.actions.push_and_open_pr import action_push_and_open_pr

        pr_result = SimpleNamespace(
            success=True, pr_url="https://github.com/x/1", pr_number=42, error="",
        )
        ws = _fake_workspace()
        mod = "orchestrator.pipeline.actions.push_and_open_pr"
        with patch(f"{mod}.create_pr", new=AsyncMock(return_value=pr_result)), \
             patch(f"{mod}.ensure_branch_has_commits"), \
             patch(f"{mod}.commit_pipeline_artifacts"), \
             patch(f"{mod}.squash_feature_commits"):
            await action_push_and_open_pr(
                ws, MagicMock(), MagicMock(), None, "", MagicMock(), None,
            )

        ws.transition.assert_not_called()


class TestActionFetchPrComments:
    def _make_stage_def(self, delay_minutes: int = 0) -> SimpleNamespace:
        return SimpleNamespace(delay_minutes=delay_minutes)

    def _invoke(self, ws, stage_def, **overrides):
        """Call action_fetch_pr_comments with sensible test defaults."""
        from orchestrator.pipeline.actions.fetch_pr_comments import action_fetch_pr_comments
        defaults = dict(
            get_vcs=lambda: (None, None),
            get_chat_id=lambda: "",
            tracker=None,
            notifier=None,
            agent_runtime=None,
            event_bus=None,
        )
        defaults.update(overrides)
        return action_fetch_pr_comments(ws, stage_def, **defaults)

    @pytest.mark.asyncio
    async def test_returns_skipped_when_no_reviewed_signal(self):
        ws = _fake_workspace(state=Stage.PR_REVIEW, pr_number=10)
        stage_def = self._make_stage_def()

        result = await self._invoke(ws, stage_def)

        assert isinstance(result, ActionResult)
        assert result.skipped is True

    @pytest.mark.asyncio
    async def test_returns_done_when_no_pr_number(self):
        ws = _fake_workspace(state=Stage.PR_REVIEW, pr_number=None)

        result = await self._invoke(ws, self._make_stage_def())

        assert result.success is True
        assert result.next_state == Stage.DONE

    @pytest.mark.asyncio
    async def test_returns_done_when_no_comments(self):
        vcs = MagicMock()
        vcs.get_pr_comments = AsyncMock(return_value=[])

        ws = _fake_workspace(state=Stage.PR_REVIEW, pr_number=10)
        ws.state.human_input_reply = "reviewed"
        result = await self._invoke(
            ws, self._make_stage_def(),
            get_vcs=lambda: (vcs, MagicMock()),
        )

        assert result.success is True
        assert result.next_state == Stage.DONE

    @pytest.mark.asyncio
    async def test_does_not_transition_state(self):
        ws = _fake_workspace(state=Stage.PR_REVIEW, pr_number=None)

        await self._invoke(ws, self._make_stage_def())

        ws.transition.assert_not_called()


class TestActionFinalize:
    @pytest.mark.asyncio
    async def test_returns_done(self):
        from orchestrator.pipeline.actions.finalize import action_finalize

        ws = _fake_workspace(state=Stage.PR_REVIEW, pr_url="https://github.com/x/1")

        result = await action_finalize(ws, None, "", None)

        assert isinstance(result, ActionResult)
        assert result.success is True
        assert result.next_state == Stage.DONE

    @pytest.mark.asyncio
    async def test_does_not_transition_state(self):
        from orchestrator.pipeline.actions.finalize import action_finalize

        ws = _fake_workspace()

        await action_finalize(ws, None, "", None)

        ws.transition.assert_not_called()


class TestHandleActionStage:
    def _make_orchestrator(self):
        from orchestrator.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        orch._dry_run = False
        orch._workflow = None
        orch._events = MagicMock()
        orch._mode_handler = None
        orch._notifier = None
        orch._tracker = None
        orch._get_vcs_for_workspace = MagicMock(return_value=(MagicMock(), MagicMock()))
        orch._get_chat_id = MagicMock(return_value="")
        return orch

    def _make_stage_def(self, action: str) -> SimpleNamespace:
        return SimpleNamespace(action=action, delay_minutes=0, max_iterations=0)

    @pytest.mark.asyncio
    async def test_happy_path_push(self):
        from orchestrator.orchestrator import Orchestrator

        orch = self._make_orchestrator()
        ws = _fake_workspace()
        push_mock = AsyncMock(return_value=ActionResult(
            success=True, next_state=Stage.PR_REVIEW, error="",
            metadata={"pr_url": "https://github.com/x/1", "pr_number": 42},
        ))

        with patch("orchestrator.pipeline.action_stage.stage_verifier") as sv, \
             patch("orchestrator.pipeline.action_stage.should_approval_gate", return_value=False), \
             patch("orchestrator.orchestrator.action_push_and_open_pr", new=push_mock):
            sv.capture_stage_start = MagicMock(return_value="abc123")
            sv.verify = MagicMock(return_value=SimpleNamespace(ok=True, stage_id="push", reason=""))
            await Orchestrator._handle_action_stage(orch, ws, "push", self._make_stage_def("push_and_open_pr"))

        ws.update_state.assert_any_call(pr_url="https://github.com/x/1", pr_number=42)
        ws.transition.assert_called_with(Stage.PR_REVIEW)
        orch._events.emit.assert_any_call(
            "action_completed", ANY,
            project_id="test-co", ticket_id="T-1",
            data={"stage": "push", "pr_url": "https://github.com/x/1", "pr_number": 42},
        )

    @pytest.mark.asyncio
    async def test_push_failure_escalates_not_failed(self):
        from orchestrator.orchestrator import Orchestrator

        orch = self._make_orchestrator()
        ws = _fake_workspace()
        push_mock = AsyncMock(return_value=ActionResult(
            success=False, next_state="", error="No VCS configured", metadata={},
        ))

        with patch("orchestrator.pipeline.action_stage.stage_verifier") as sv, \
             patch("orchestrator.pipeline.action_stage.handle_escalate", new=AsyncMock()) as esc, \
             patch("orchestrator.orchestrator.action_push_and_open_pr", new=push_mock):
            sv.capture_stage_start = MagicMock(return_value="abc123")
            await Orchestrator._handle_action_stage(orch, ws, "push", self._make_stage_def("push_and_open_pr"))
            sv.verify.assert_not_called()

        ws.update_state.assert_called_once_with(error="No VCS configured")
        esc.assert_awaited_once()
        ws.transition.assert_not_called()

    @pytest.mark.asyncio
    async def test_verify_failure_transitions_to_blocked(self):
        from orchestrator.orchestrator import Orchestrator

        orch = self._make_orchestrator()
        ws = _fake_workspace()
        push_mock = AsyncMock(return_value=ActionResult(
            success=True, next_state=Stage.PR_REVIEW, error="",
            metadata={"pr_url": "https://github.com/x/1", "pr_number": 42},
        ))

        with patch("orchestrator.pipeline.action_stage.stage_verifier") as sv, \
             patch("orchestrator.orchestrator.action_push_and_open_pr", new=push_mock):
            sv.capture_stage_start = MagicMock(return_value="abc123")
            sv.verify = MagicMock(return_value=SimpleNamespace(
                ok=False, stage_id="push",
                reason="remote has no ref refs/heads/feature/t-1 (branch not pushed)",
            ))
            await Orchestrator._handle_action_stage(orch, ws, "push", self._make_stage_def("push_and_open_pr"))

        ws.transition.assert_called_once_with(Stage.BLOCKED)
        assert "branch not pushed" in ws.update_state.call_args[1]["error"]
        orch._events.emit.assert_any_call(
            "stage_verification_failed", ANY,
            project_id="test-co", ticket_id="T-1",
            data={"stage": "push", "reason": "remote has no ref refs/heads/feature/t-1 (branch not pushed)"},
        )

    @pytest.mark.asyncio
    async def test_skipped_action_no_transition(self):
        from orchestrator.orchestrator import Orchestrator

        orch = self._make_orchestrator()
        ws = _fake_workspace(state=Stage.PR_REVIEW, pr_number=10)
        fetch_mock = AsyncMock(return_value=ActionResult(
            success=False, next_state="", error="", metadata={}, skipped=True,
        ))

        with patch("orchestrator.pipeline.action_stage.stage_verifier") as sv, \
             patch("orchestrator.pipeline.action_stage.rollback_iteration") as rb, \
             patch("orchestrator.orchestrator.action_fetch_pr_comments", new=fetch_mock):
            sv.capture_stage_start = MagicMock(return_value=None)
            await Orchestrator._handle_action_stage(orch, ws, "pr_review", self._make_stage_def("fetch_pr_comments"))
            sv.verify.assert_not_called()

        ws.transition.assert_not_called()
        rb.assert_called_once_with(ws, "pr_review")

    @pytest.mark.asyncio
    async def test_approval_gate_after_verify(self):
        from orchestrator.orchestrator import Orchestrator

        orch = self._make_orchestrator()
        orch._notifier = None
        ws = _fake_workspace(state=Stage.PR_REVIEW, pr_number=10)
        fetch_mock = AsyncMock(return_value=ActionResult(
            success=True, next_state=Stage.DONE, error="", metadata={},
        ))

        with patch("orchestrator.pipeline.action_stage.stage_verifier") as sv, \
             patch("orchestrator.pipeline.action_stage.should_approval_gate", return_value=True), \
             patch("orchestrator.orchestrator.action_fetch_pr_comments", new=fetch_mock):
            sv.capture_stage_start = MagicMock(return_value=None)
            sv.verify = MagicMock(return_value=SimpleNamespace(ok=True, stage_id="pr_review", reason=""))
            await Orchestrator._handle_action_stage(orch, ws, "pr_review", self._make_stage_def("fetch_pr_comments"))

        ws.transition.assert_called_once_with(Stage.AWAITING_APPROVAL)
        orch._events.emit.assert_any_call(
            "approval_requested", ANY,
            project_id="test-co", ticket_id="T-1",
            data={"gate": "PR_REVIEW"},
        )
