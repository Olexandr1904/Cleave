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

logger = logging.getLogger(__name__)

TERMINAL_STATES = {"DONE", "ARCHIVED"}


def _find_workspace(orchestrator: Any, ticket_id: str) -> Any | None:
    """Find workspace by ticket_id in active workspaces."""
    for ws in orchestrator.get_active_workspaces():
        if ws.state.ticket_id == ticket_id:
            return ws
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
        ws = _find_workspace(orchestrator, ticket_id)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state != "AWAITING_APPROVAL":
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
        ws = _find_workspace(orchestrator, ticket_id)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state != "AWAITING_APPROVAL":
            return _error(f"Cannot reject: state is {ws.state.current_state}")

        previous = ws.state.previous_state or "ANALYSIS"
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
        ws = _find_workspace(orchestrator, ticket_id)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state not in ("BLOCKED", "FAILED"):
            return _error(f"Cannot retry: state is {ws.state.current_state}")

        target = ws.state.previous_state or "ANALYSIS"
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
        ws = _find_workspace(orchestrator, ticket_id)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state in TERMINAL_STATES | {"MANUAL_CONTROL"}:
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
            agent_runtime.cancel(ticket_id)

        # Transition to MANUAL_CONTROL atomically with timestamp
        ws.transition(
            "MANUAL_CONTROL",
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
        ws = _find_workspace(orchestrator, ticket_id)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state != "MANUAL_CONTROL":
            return _error(f"Cannot release: state is {ws.state.current_state}")

        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        comment = body.get("comment", "")

        ws.update_state(manual_control_comment=comment)
        ws.transition("ANALYSIS")

        if event_bus:
            event_bus.emit(
                "manual_control_released",
                f"Manual control released for {ticket_id}" + (f": {comment}" if comment else ""),
                ticket_id=ticket_id,
                data={"comment": comment},
            )
        return JSONResponse({"status": "ok", "new_state": "ANALYSIS"})

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
        blocked = sum(1 for ws in workspaces if ws.state.current_state == "BLOCKED")
        awaiting = sum(1 for ws in workspaces if ws.state.current_state == "AWAITING_APPROVAL")
        manual = sum(1 for ws in workspaces if ws.state.current_state == "MANUAL_CONTROL")

        mode = mode_handler.get_mode() if mode_handler else "auto"

        return JSONResponse({
            "mode": mode,
            "active": active,
            "blocked": blocked,
            "awaiting": awaiting,
            "manual_control": manual,
        })

    return [
        Route("/api/workspaces/{ticket_id:path}/approve", approve, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/reject", reject, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/retry", retry, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/take-control", take_control, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/release-control", release_control, methods=["POST"]),
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
