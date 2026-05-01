"""Dashboard action endpoints — POST handlers for workspace actions."""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from orchestrator.constants import REPORT_BA, REPORT_DEV, REPORT_QA, REPORT_SCOPE_GUARD
from workspace.workspace import Stage

logger = logging.getLogger(__name__)

TERMINAL_STATES = {Stage.DONE, Stage.ARCHIVED}


def _find_workspace(
    orchestrator: Any,
    ticket_id: str,
    global_config: Any | None = None,
) -> Any | None:
    """Find workspace by ticket_id, scanning disk if not in the active list.

    Workspaces can fall out of `_active_workspaces` (e.g. after escalation,
    before the next poll-cycle reconcile). The dashboard surfaces them via a
    disk scan, so action endpoints must do the same — otherwise pause/unpause
    on a re-listed but un-adopted workspace returns 404 even though the user
    sees the card.

    When found on disk, the workspace is re-adopted into the active list so
    subsequent poll-cycle logic sees it.
    """
    for ws in orchestrator.get_active_workspaces():
        if ws.state.ticket_id == ticket_id:
            return ws

    base_dir = None
    if global_config is not None:
        try:
            base_dir = Path(global_config.workspaces.base_dir)
        except AttributeError:
            base_dir = None
    if base_dir is None or not base_dir.exists():
        return None

    from workspace.workspace import Workspace
    for state_file in base_dir.rglob("state.json"):
        try:
            ws = Workspace(str(state_file.parent))
            if ws.state.ticket_id == ticket_id:
                # Re-adopt so the orchestrator's poll cycle sees it.
                try:
                    orchestrator._active_workspaces.append(ws)
                    logger.info(
                        "Re-adopted workspace %s from disk for action handler",
                        ticket_id,
                    )
                except AttributeError:
                    pass
                return ws
        except Exception:
            continue
    return None


def _error(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"status": "error", "message": msg}, status_code=status)


def build_action_routes(
    orchestrator: Any,
    mode_handler: Any,
    event_bus: Any | None = None,
    global_config: Any | None = None,
) -> list:
    """Build Starlette Route objects for all action endpoints."""
    from starlette.routing import Route

    async def approve(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        ws = _find_workspace(orchestrator, ticket_id, global_config)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state != Stage.AWAITING_APPROVAL:
            return _error(f"Cannot approve: state is {ws.state.current_state}")

        from integrations.telegram.handlers.approval import ApprovalHandler
        handler = ApprovalHandler()
        next_state = handler.resolve_next_state(ws)
        ws.transition(next_state)
        if event_bus:
            event_bus.emit(
                "dashboard_approve",
                f"Approved {ticket_id} via dashboard → {next_state}",
                ticket_id=ticket_id,
                data={"new_state": next_state},
            )
        return JSONResponse({"status": "ok", "new_state": next_state})

    async def reject(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        ws = _find_workspace(orchestrator, ticket_id, global_config)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state != Stage.AWAITING_APPROVAL:
            return _error(f"Cannot reject: state is {ws.state.current_state}")

        previous = ws.state.previous_state or Stage.ANALYSIS
        ws.transition(previous)
        if event_bus:
            event_bus.emit(
                "dashboard_reject",
                f"Rejected {ticket_id} via dashboard → back to {previous}",
                ticket_id=ticket_id,
                data={"new_state": previous},
            )
        return JSONResponse({"status": "ok", "new_state": previous})

    async def retry(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        ws = _find_workspace(orchestrator, ticket_id, global_config)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state not in (Stage.BLOCKED, Stage.FAILED):
            return _error(f"Cannot retry: state is {ws.state.current_state}")

        # Smart retry: detect furthest completed stage from existing artifacts
        target = ws.state.previous_state or Stage.ANALYSIS
        reports = Path(ws.reports_dir)
        if (reports / REPORT_QA).exists():
            target = Stage.PUSHED
        elif (reports / REPORT_SCOPE_GUARD).exists():
            target = Stage.QA
        elif (reports / REPORT_DEV).exists():
            target = Stage.SCOPE_CHECK
        elif (reports / REPORT_BA).exists():
            target = Stage.DEV

        ws.state.human_input_pending = False
        ws.state.error = None
        ws.transition(target)
        ws.save_state()
        if event_bus:
            event_bus.emit(
                "dashboard_retry",
                f"Retried {ticket_id} via dashboard → {target}",
                ticket_id=ticket_id,
                data={"new_state": target},
            )
        return JSONResponse({"status": "ok", "new_state": target})

    async def take_control(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        ws = _find_workspace(orchestrator, ticket_id, global_config)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        BLOCKS_TAKE_CONTROL = {Stage.DONE, Stage.ARCHIVED, Stage.MANUAL_CONTROL}
        if ws.state.current_state in BLOCKS_TAKE_CONTROL:
            return _error(f"Cannot take control: state is {ws.state.current_state}")

        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        confirm = body.get("confirm", False)

        # Check if agent is running
        agent_runtime = orchestrator._agent_runtime
        running = agent_runtime.get_running(ticket_id)
        if running and not confirm:
            elapsed = time.time() - running.get("started_at", time.time())
            return JSONResponse({
                "status": "agent_running",
                "agent": running["agent_id"],
                "started_ago": f"{int(elapsed)}s",
            })

        # Kill agent if running
        if running:
            await agent_runtime.cancel(ticket_id)

        # Transition to MANUAL_CONTROL atomically with timestamp
        ws.transition(
            Stage.MANUAL_CONTROL,
            manual_control_started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

        # Write a launcher script — avoids all shell-quoting issues.
        # All file I/O is best-effort: if it fails (broken meta dir, mocked
        # workspace in tests, etc.) we still complete the state transition.
        full_cmd = ""
        try:
            prompt = _build_claude_prompt(ws)
            prompt_path = Path(ws.meta_dir) / "manual_control_prompt.txt"
            prompt_path.write_text(prompt)
            script_path = Path(ws.meta_dir) / "manual_control_launch.sh"
            script_path.write_text(
                "#!/bin/bash\n"
                f"cd {shlex.quote(str(ws.source_dir))}\n"
                f"claude \"$(cat {shlex.quote(str(prompt_path))})\"\n"
                "exec bash\n"
            )
            script_path.chmod(0o755)

            terminal_cmd = "gnome-terminal -- bash -c"
            if global_config and hasattr(global_config, "dashboard"):
                terminal_cmd = getattr(global_config.dashboard, "terminal_command", terminal_cmd)
            full_cmd = f"{terminal_cmd} {shlex.quote(str(script_path))}"
            logger.info("take_control launching: %s", full_cmd)
            subprocess.Popen(full_cmd, shell=True)
        except Exception as e:
            logger.warning("Failed to launch manual-control terminal: %s", e)

        if event_bus:
            event_bus.emit(
                "manual_control_started",
                f"Manual control taken for {ticket_id}",
                ticket_id=ticket_id,
                data={"previous_state": ws.state.previous_state},
            )
        return JSONResponse({"status": "ok", "command": full_cmd})

    async def release_control(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        ws = _find_workspace(orchestrator, ticket_id, global_config)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state != Stage.MANUAL_CONTROL:
            return _error(f"Cannot release: state is {ws.state.current_state}")

        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        comment = body.get("comment", "")

        ws.update_state(manual_control_comment=comment)
        ws.transition(Stage.ANALYSIS)

        if event_bus:
            event_bus.emit(
                "manual_control_released",
                f"Manual control released for {ticket_id}" + (f": {comment}" if comment else ""),
                ticket_id=ticket_id,
                data={"comment": comment},
            )
        return JSONResponse({"status": "ok", "new_state": Stage.ANALYSIS})

    async def resume(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        ws = _find_workspace(orchestrator, ticket_id, global_config)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state != Stage.DEFERRED:
            return _error(f"Cannot resume: state is {ws.state.current_state}")

        target = ws.state.previous_state or Stage.ANALYSIS
        ws.transition(target)
        if event_bus:
            event_bus.emit(
                "deferred_resumed",
                f"Resumed {ticket_id} via dashboard \u2192 {target}",
                ticket_id=ticket_id,
                data={"new_state": target, "trigger": "dashboard"},
            )
        return JSONResponse({"status": "ok", "new_state": target})

    async def pause(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        logger.info("pause: request for ticket_id=%r", ticket_id)
        ws = _find_workspace(orchestrator, ticket_id, global_config)
        if ws is None:
            active_ids = [w.state.ticket_id for w in orchestrator.get_active_workspaces()]
            logger.warning("pause: workspace not found for %r (active=%s)", ticket_id, active_ids)
            return _error(f"Workspace not found: {ticket_id}", 404)

        logger.info("pause: %s current_state=%s", ticket_id, ws.state.current_state)
        PAUSEABLE = {Stage.ANALYSIS, Stage.DEV, Stage.SCOPE_CHECK,
                     Stage.QA, Stage.PUSHED, Stage.PR_REVIEW}
        if ws.state.current_state not in PAUSEABLE:
            logger.warning("pause: %s rejected, state %s not pauseable", ticket_id, ws.state.current_state)
            return _error(f"Cannot pause: state is {ws.state.current_state}")

        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        confirm = body.get("confirm", False)

        agent_runtime = orchestrator._agent_runtime
        running = agent_runtime.get_running(ticket_id)
        if running and not confirm:
            elapsed = time.time() - running.get("started_at", time.time())
            return JSONResponse({
                "status": "agent_running",
                "agent": running["agent_id"],
                "started_ago": f"{int(elapsed)}s",
            })

        if running:
            await agent_runtime.cancel(ticket_id)

        previous = ws.state.current_state
        ws.transition(Stage.PAUSED)

        if event_bus:
            event_bus.emit(
                "workspace_paused",
                f"Paused {ticket_id} from {previous} via dashboard",
                ticket_id=ticket_id,
                data={"previous_state": previous},
            )
        return JSONResponse({"status": "ok", "new_state": Stage.PAUSED})

    async def unpause(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        logger.info("unpause: request for ticket_id=%r", ticket_id)
        ws = _find_workspace(orchestrator, ticket_id, global_config)
        if ws is None:
            active_ids = [w.state.ticket_id for w in orchestrator.get_active_workspaces()]
            logger.warning("unpause: workspace not found for %r (active=%s)", ticket_id, active_ids)
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state != Stage.PAUSED:
            logger.warning("unpause: %s rejected, state is %s not PAUSED", ticket_id, ws.state.current_state)
            return _error(f"Cannot unpause: state is {ws.state.current_state}")

        target = ws.state.previous_state or Stage.ANALYSIS
        ws.transition(target)

        if event_bus:
            event_bus.emit(
                "workspace_unpaused",
                f"Unpaused {ticket_id} via dashboard → {target}",
                ticket_id=ticket_id,
                data={"new_state": target},
            )
        return JSONResponse({"status": "ok", "new_state": target})

    async def archive(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        ws = _find_workspace(orchestrator, ticket_id, global_config)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state not in (Stage.FAILED, Stage.DONE, Stage.DEFERRED):
            return _error(f"Cannot archive: state is {ws.state.current_state}")

        # DEFERRED -> ARCHIVED is not a valid direct transition; hop via FAILED.
        if ws.state.current_state == Stage.DEFERRED:
            ws.transition(Stage.FAILED)
        ws.transition(Stage.ARCHIVED)

        if event_bus:
            event_bus.emit(
                "workspace_archived",
                f"Archived {ticket_id} via dashboard",
                ticket_id=ticket_id,
            )
        return JSONResponse({"status": "ok", "new_state": Stage.ARCHIVED})

    async def set_mode(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return _error("Invalid JSON body")
        mode = body.get("mode", "")
        try:
            mode_handler.set_mode(mode)
        except ValueError as e:
            return _error(str(e))
        if event_bus:
            event_bus.emit("mode_changed", f"Mode set to {mode} via dashboard", data={"mode": mode})
        return JSONResponse({"status": "ok", "mode": mode})

    async def daemon_status(request: Request) -> JSONResponse:
        workspaces = orchestrator.get_active_workspaces()
        active = len(workspaces)
        blocked = sum(1 for ws in workspaces if ws.state.current_state == Stage.BLOCKED)
        awaiting = sum(1 for ws in workspaces if ws.state.current_state == Stage.AWAITING_APPROVAL)
        manual = sum(1 for ws in workspaces if ws.state.current_state == Stage.MANUAL_CONTROL)

        mode = mode_handler.get_mode() if mode_handler else "auto"

        return JSONResponse({
            "mode": mode,
            "active": active,
            "blocked": blocked,
            "awaiting": awaiting,
            "manual_control": manual,
        })

    async def delete_workspace(request: Request) -> JSONResponse:
        """Delete a workspace entirely — removes from disk and active list."""
        import shutil
        from workspace.workspace import Workspace
        ticket_id = request.path_params["ticket_id"]

        # Find workspace — check active list first, then scan disk
        ws = _find_workspace(orchestrator, ticket_id, global_config)
        ws_root = None
        if ws:
            ws_root = Path(ws.state.workspace_root)
        else:
            # Not in active list — scan disk for it
            base = Path(global_config.workspaces.base_dir) if global_config else None
            if base:
                for state_file in base.rglob("state.json"):
                    try:
                        w = Workspace(str(state_file.parent))
                        if w.state.ticket_id == ticket_id:
                            ws_root = state_file.parent
                            break
                    except Exception:
                        pass
        if not ws_root or not ws_root.exists():
            return _error(f"Workspace not found: {ticket_id}", 404)

        try:
            shutil.rmtree(ws_root)
        except Exception as e:
            return _error(f"Failed to delete: {e}", 500)

        # Remove from active list if present
        orchestrator._active_workspaces = [
            w for w in orchestrator._active_workspaces
            if w.state.ticket_id != ticket_id
        ]

        if event_bus:
            event_bus.emit(
                "workspace_deleted",
                f"Deleted workspace for {ticket_id}",
                ticket_id=ticket_id,
            )
        return JSONResponse({"status": "ok", "deleted": ticket_id})

    async def clean_source(request: Request) -> JSONResponse:
        """Remove source/ dir from a DONE workspace to free disk space."""
        import shutil
        from workspace.workspace import Workspace
        ticket_id = request.path_params["ticket_id"]

        ws = _find_workspace(orchestrator, ticket_id, global_config)
        if not ws:
            # Scan disk
            base = Path(global_config.workspaces.base_dir) if global_config else None
            if base:
                for state_file in base.rglob("state.json"):
                    try:
                        w = Workspace(str(state_file.parent))
                        if w.state.ticket_id == ticket_id:
                            ws = w
                            break
                    except Exception:
                        pass
        if not ws:
            return _error(f"Workspace not found: {ticket_id}", 404)

        if ws.state.current_state not in ("DONE", "ARCHIVED", "SETUP_DONE", "FAILED"):
            return _error("Can only clean source for completed tickets", 400)

        source_dir = ws.source_dir
        if not source_dir.exists():
            return JSONResponse({"status": "ok", "message": "Already clean"})

        try:
            shutil.rmtree(source_dir)
        except Exception as e:
            return _error(f"Failed to clean source: {e}", 500)

        if event_bus:
            event_bus.emit(
                "workspace_cleaned",
                f"Cleaned source for {ticket_id}",
                ticket_id=ticket_id,
            )
        return JSONResponse({"status": "ok", "cleaned": ticket_id})

    async def rerun(request: Request) -> JSONResponse:
        from datetime import datetime, timezone
        ticket_id = request.path_params["ticket_id"]
        ws = _find_workspace(orchestrator, ticket_id, global_config)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state != Stage.DONE:
            return _error(f"Cannot rerun: state is {ws.state.current_state}")

        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        reason = (body.get("reason") or "").strip()
        if not reason:
            return _error("reason is required and must be non-empty")

        # Resolve repo config
        repo_config = None
        try:
            project = orchestrator._projects.get(ws.state.company_id)
            if project:
                repo_config = project.repos.get(ws.state.repo_id)
        except Exception:
            pass
        if repo_config is None:
            return _error(
                f"Repo config not found for {ws.state.company_id}/{ws.state.repo_id}",
                500,
            )

        clone_url = repo_config.git.clone_url
        default_branch = (
            repo_config.vcs.github.default_branch
            if repo_config.vcs.provider == "github"
            else repo_config.vcs.gitlab.default_branch
        )

        # Refresh ticket data (appends to meta files)
        try:
            await orchestrator._refetch_ticket_data(ws)
        except Exception as e:
            logger.warning("Failed to refetch ticket data for %s: %s", ticket_id, e)

        # Re-clone source
        try:
            branch = orchestrator._workspace_manager.reset_source(
                ws, clone_url, default_branch
            )
        except Exception as e:
            return _error(f"Failed to reset source: {e}", 500)

        # Sync branch state if fallback occurred
        ws.state.branch = branch

        # Append rerun entry to meta/rerun_history.md
        rerun_file = Path(ws.meta_dir) / "rerun_history.md"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = f"\n## Rerun {ts}\n\n{reason}\n"
        existing = (
            rerun_file.read_text(encoding="utf-8")
            if rerun_file.exists()
            else "# Rerun History\n"
        )
        rerun_file.write_text(existing + entry, encoding="utf-8")

        # Clear stale state fields and transition
        ws.state.pr_number = None
        ws.state.pr_url = None
        ws.state.last_verified_sha = ""
        ws.state.review_cycle = 0
        ws.state.pending_review_comments = None
        ws.state.error = None
        ws.state.stage_iterations = {}
        ws.transition(Stage.ANALYSIS)

        # Telegram notification
        try:
            await orchestrator._notify_rerun(ws, branch, reason)
        except Exception as e:
            logger.warning(
                "Failed to send rerun notification for %s: %s", ticket_id, e
            )

        if event_bus:
            event_bus.emit(
                "dashboard_rerun",
                f"Rerun {ticket_id} via dashboard — {reason[:60]}",
                ticket_id=ticket_id,
                data={"new_state": Stage.ANALYSIS, "branch": branch, "reason": reason},
            )

        return JSONResponse(
            {"status": "ok", "new_state": Stage.ANALYSIS, "branch": branch}
        )

    async def clear_gradle_and_retry(request: Request) -> JSONResponse:
        """Wipe Gradle transforms cache and retry the ticket.

        Surfaced only when the ticket's failure error matches the AAPT2 cache
        corruption signature — see `orchestrator.gradle_remediation`. Same retry
        logic as the regular retry endpoint, just with a cache wipe first.
        """
        from orchestrator.gradle_remediation import (
            clear_gradle_transforms,
            looks_like_gradle_cache_corruption,
        )

        ticket_id = request.path_params["ticket_id"]
        ws = _find_workspace(orchestrator, ticket_id, global_config)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state != Stage.FAILED:
            return _error(
                f"Cannot clear-and-retry: state is {ws.state.current_state} (expected FAILED)"
            )
        if not looks_like_gradle_cache_corruption(ws.state.error or ""):
            # Defensive: refuse to wipe the cache for unrelated failures even
            # if the operator somehow hits this endpoint manually. The intent
            # is "fix the AAPT2 corruption you just saw," not a generic reset.
            return _error("Failure does not match Gradle cache corruption signature")

        try:
            freed = clear_gradle_transforms()
        except Exception as e:
            logger.exception("Gradle cache clear failed for %s", ticket_id)
            return _error(f"Failed to clear Gradle cache: {e}", 500)

        target = ws.state.previous_state or Stage.ANALYSIS
        ws.state.human_input_pending = False
        ws.state.error = None
        ws.transition(target)
        ws.save_state()
        if event_bus:
            event_bus.emit(
                "gradle_cache_cleared",
                f"Cleared Gradle cache for {ticket_id} ({freed} bytes)",
                ticket_id=ticket_id,
                data={"bytes_freed": freed, "new_state": target},
            )
        return JSONResponse({
            "status": "ok",
            "bytes_freed": freed,
            "new_state": target,
        })

    return [
        Route("/api/workspaces/{ticket_id:path}/approve", approve, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/reject", reject, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/retry", retry, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/clear-gradle-and-retry", clear_gradle_and_retry, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/take-control", take_control, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/release-control", release_control, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/resume", resume, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/pause", pause, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/unpause", unpause, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/archive", archive, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/clean", clean_source, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/rerun", rerun, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/delete", delete_workspace, methods=["POST"]),
        Route("/api/daemon/mode", set_mode, methods=["POST"]),
        Route("/api/daemon/status", daemon_status),
    ]


def _build_claude_prompt(ws: Any) -> str:
    """Build the initial prompt to hand to interactive claude."""
    state = ws.state
    reports_dir = str(ws.reports_dir)
    meta_dir = str(ws.meta_dir)

    # List available reports
    reports = []
    try:
        for f in Path(reports_dir).iterdir():
            if f.is_file():
                reports.append(f"  {f.name}")
    except (OSError, FileNotFoundError):
        pass

    # List available meta files
    meta = []
    try:
        for f in Path(meta_dir).iterdir():
            if f.is_file():
                meta.append(f"  {f.name}")
    except (OSError, FileNotFoundError):
        pass

    parts = [
        f"You are resuming work on ticket {state.ticket_id}.",
        f"Previous state: {state.previous_state or 'unknown'} (iteration history: {state.stage_iterations})",
    ]
    if state.error:
        parts.append(f"Error/escalation: {state.error}")
    if reports:
        parts.append("Reports available in ../reports/:\n" + "\n".join(reports))
    if meta:
        parts.append("Meta files in ../meta/:\n" + "\n".join(meta))
    parts.append(
        "The operator has taken manual control. Ask them what they want to do."
    )

    return "\n\n".join(parts)
