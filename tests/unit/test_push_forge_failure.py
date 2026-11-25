"""A push the forge failed to take is retried later, not left for the operator.

GitHub's storage refused three tickets' packs within eleven seconds ("remote unpack failed: index-pack failed"). Each
ticket stopped BLOCKED, and the same push went through untouched once GitHub was back.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.pipeline.action_stage import handle_action_stage
from orchestrator.stage_verifier import ActionResult
from workspace.workspace import Stage

FORGE_ERROR = (
    "Git command failed: git -C /src push -u origin feature/T-1\n"
    "remote: fatal: unable to rename temporary '*.pack' file to '/data/repositories/x/pack-1.pack'\n"
    "error: remote unpack failed: index-pack failed\n"
    " ! [remote rejected] feature/T-1 -> feature/T-1 (failed)"
)
HOOK_ERROR = (
    "Git command failed: git -C /src push -u origin feature/T-1\n"
    "Execution failed for task ':app:detekt'.\n"
    "> Analysis failed with 3 weighted issues."
)


def _ws(tmp_path):
    state = SimpleNamespace(
        ticket_id="T-1", company_id="c", repo_id="r", current_state=Stage.PUSHED, previous_state=Stage.QA,
        retry_at=None, error=None, transient_error=None, transient_count=0, stage_iterations={},
    )
    ws = MagicMock()
    ws.state = state
    ws.reports_dir = tmp_path
    ws.update_state.side_effect = lambda **kw: state.__dict__.update(kw)
    ws.transition.side_effect = lambda to, retry_at=None: state.__dict__.update(current_state=to, retry_at=retry_at)
    ws.increment_iteration.side_effect = lambda sid: state.stage_iterations.__setitem__(
        sid, state.stage_iterations.get(sid, 0) + 1)
    return ws


async def _push(ws, error, deferred, *, success=False):
    with patch("orchestrator.pipeline.action_stage.stage_verifier") as sv, \
         patch("orchestrator.pipeline.action_stage.handle_escalate", new=AsyncMock()) as escalate:
        sv.capture_stage_start = MagicMock(return_value="abc123")
        await handle_action_stage(
            ws, "push", SimpleNamespace(action="push_and_open_pr"),
            workflow=None, notifier=None, mode_handler=None, get_chat_id=lambda: "", dry_run=False, event_bus=None,
            action_push_and_open_pr=AsyncMock(return_value=ActionResult(
                success=success, next_state=Stage.PR_REVIEW if success else "", error=error or "", metadata={})),
            action_fetch_pr_comments=AsyncMock(), action_finalize=AsyncMock(), advance_to_stage_fn=MagicMock(),
            on_ticket_done_fn=AsyncMock(), build_gate_summary_fn=MagicMock(), notify_deferred_fn=deferred,
        )
    return escalate


@pytest.mark.asyncio
async def test_a_push_the_forge_failed_to_take_is_deferred(tmp_path):
    ws, deferred = _ws(tmp_path), AsyncMock()

    escalate = await _push(ws, FORGE_ERROR, deferred)

    assert ws.state.current_state == Stage.DEFERRED
    wait = datetime.fromisoformat(ws.state.retry_at) - datetime.now(timezone.utc)
    assert timedelta(minutes=14) < wait <= timedelta(minutes=15)
    assert ws.state.stage_iterations["push"] == 0          # the attempt is not counted against the stage
    deferred.assert_awaited_once()
    assert "remote unpack failed" in deferred.await_args.kwargs["reason"]
    escalate.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_third_such_push_in_a_row_goes_to_the_operator(tmp_path):
    ws, deferred = _ws(tmp_path), AsyncMock()

    for _ in range(3):
        ws.state.current_state = Stage.PUSHED
        escalate = await _push(ws, FORGE_ERROR, deferred)

    assert deferred.await_count == 2
    escalate.assert_awaited_once()
    assert ws.state.error == FORGE_ERROR
    assert (ws.state.transient_error, ws.state.transient_count) == (None, 0)   # a Retry gets three more tries


@pytest.mark.asyncio
async def test_the_streak_holds_when_the_push_went_again_without_the_hook(tmp_path):
    ws, deferred = _ws(tmp_path), AsyncMock()
    no_verify = FORGE_ERROR.replace("push -u", "push --no-verify -u")

    for error in (FORGE_ERROR, no_verify, FORGE_ERROR):
        ws.state.current_state = Stage.PUSHED
        escalate = await _push(ws, error, deferred)

    assert deferred.await_count == 2
    escalate.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_push_the_hook_refused_still_goes_to_the_operator(tmp_path):
    ws, deferred = _ws(tmp_path), AsyncMock()

    escalate = await _push(ws, HOOK_ERROR, deferred)

    escalate.assert_awaited_once()
    deferred.assert_not_awaited()
    assert ws.state.current_state == Stage.PUSHED


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    "Git command failed: git push\nfatal: unable to access 'https://github.com/o/r.git/': Could not resolve host: github.com",
    "Git command failed: git push\nerror: RPC failed; HTTP 500 curl 22 The requested URL returned error: 500\n"
    "fatal: the remote end hung up unexpectedly",
    "Git command failed: git push\nerror: RPC failed; curl 56 Recv failure: Connection reset by peer\n"
    "send-pack: unexpected disconnect while reading sideband packet\nfatal: the remote end hung up unexpectedly",
])
async def test_git_saying_the_forge_or_the_network_failed_defers(tmp_path, error):
    ws, deferred = _ws(tmp_path), AsyncMock()

    escalate = await _push(ws, error, deferred)

    deferred.assert_awaited_once()
    escalate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    # A pre-push hook's own output: a failing test of the app's network code, not the push.
    "Git command failed: git push\n    Caused by: java.net.ConnectException: Connection refused\n"
    "FAILURE: Build failed with an exception.",
    "Git command failed: git push\nRetryPolicyTest > maps HTTP 503 to retry() FAILED",
    # A pack the forge will never take, however often it is sent.
    "Git command failed: git push\nerror: RPC failed; HTTP 413 curl 22 The requested URL returned error: 413\n"
    "fatal: the remote end hung up unexpectedly",
])
async def test_a_failure_git_does_not_blame_on_the_forge_goes_to_the_operator(tmp_path, error):
    ws, deferred = _ws(tmp_path), AsyncMock()

    escalate = await _push(ws, error, deferred)

    escalate.assert_awaited_once()
    deferred.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_push_stopped_for_another_reason_starts_the_streak_again(tmp_path):
    ws, deferred = _ws(tmp_path), AsyncMock()

    for error in (FORGE_ERROR, FORGE_ERROR, HOOK_ERROR, FORGE_ERROR):
        ws.state.current_state = Stage.PUSHED
        await _push(ws, error, deferred)

    assert deferred.await_count == 3
    assert ws.state.current_state == Stage.DEFERRED


@pytest.mark.asyncio
async def test_a_push_that_goes_through_ends_the_streak(tmp_path):
    ws, deferred = _ws(tmp_path), AsyncMock()

    for error in (FORGE_ERROR, FORGE_ERROR):
        ws.state.current_state = Stage.PUSHED
        await _push(ws, error, deferred)
    ws.state.current_state = Stage.PUSHED
    with patch("orchestrator.pipeline.action_stage.get_next_stage", return_value=None):
        await _push(ws, None, deferred, success=True)

    assert (ws.state.transient_error, ws.state.transient_count) == (None, 0)
