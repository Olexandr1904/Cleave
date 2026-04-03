"""Workspace manager — creates, discovers, and cleans up workspaces."""

from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from workspace.workspace import Workspace, WorkspaceState

logger = logging.getLogger(__name__)

SUBPROCESS_TIMEOUT = 300  # 5 minutes


class WorkspaceError(Exception):
    """Raised when workspace operations fail."""


class WorkspaceManager:
    """Manages workspace lifecycle: create, discover, cleanup."""

    def __init__(self, base_dir: str) -> None:
        self._base_dir = Path(base_dir)

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def create(
        self,
        project_id: str,
        repo_id: str,
        ticket_id: str,
        clone_url: str,
        clone_depth: int = 0,
    ) -> Workspace:
        """Create a new isolated workspace with a fresh git clone.

        Args:
            project_id: Project identifier.
            repo_id: Repository identifier.
            ticket_id: Ticket identifier (e.g., "ACME-123").
            clone_url: Git URL to clone.
            clone_depth: Shallow clone depth (0 = full clone).

        Returns:
            A Workspace object pointing to the new workspace.

        Raises:
            WorkspaceError: If clone fails (workspace is cleaned up).
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        workspace_name = f"{ticket_id}_{timestamp}"
        workspace_root = self._base_dir / project_id / repo_id / workspace_name

        try:
            # Create workspace directories
            workspace_root.mkdir(parents=True, exist_ok=True)
            (workspace_root / "context").mkdir()
            (workspace_root / "logs").mkdir()

            # Git clone
            repo_dir = workspace_root / "repo"
            clone_cmd = ["git", "clone"]
            if clone_depth > 0:
                clone_cmd.extend(["--depth", str(clone_depth)])
            clone_cmd.extend([clone_url, str(repo_dir)])

            result = subprocess.run(
                clone_cmd,
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT,
            )

            if result.returncode != 0:
                raise WorkspaceError(
                    f"Git clone failed: {result.stderr.strip()}"
                )

            # Create initial state
            state = WorkspaceState(
                ticket_id=ticket_id,
                project_id=project_id,
                repo_id=repo_id,
                workspace_root=str(workspace_root),
            )

            workspace = Workspace(str(workspace_root), state)
            workspace.save_state()

            logger.info(
                "Workspace created: %s/%s/%s at %s",
                project_id, repo_id, ticket_id, workspace_root,
            )
            return workspace

        except (WorkspaceError, subprocess.TimeoutExpired) as e:
            # Clean up on failure
            if workspace_root.exists():
                shutil.rmtree(workspace_root, ignore_errors=True)
            if isinstance(e, subprocess.TimeoutExpired):
                raise WorkspaceError(f"Git clone timed out after {SUBPROCESS_TIMEOUT}s") from e
            raise

    def discover_workspaces(self) -> list[Workspace]:
        """Discover existing workspaces by scanning the base directory.

        Returns workspaces with status 'running' or 'waiting_for_human'.
        Workspaces with 'completed' or 'failed' are skipped.
        """
        active_workspaces: list[Workspace] = []

        if not self._base_dir.exists():
            return active_workspaces

        for state_file in self._base_dir.rglob("state.json"):
            workspace_root = state_file.parent
            try:
                ws = Workspace(str(workspace_root))
                status = ws.state.status
                if status in ("running", "waiting_for_human"):
                    active_workspaces.append(ws)
                    logger.info(
                        "Discovered active workspace: %s (status=%s, stage=%s)",
                        ws.state.ticket_id, status, ws.state.current_stage,
                    )
            except Exception as e:
                logger.warning(
                    "Failed to load workspace at %s: %s", workspace_root, e
                )

        return active_workspaces

    def cleanup_old_workspaces(self, max_age_days: int) -> list[str]:
        """Delete workspaces with completed/failed status older than max_age_days.

        Running and waiting_for_human workspaces are never deleted.

        Returns list of deleted workspace paths.
        """
        deleted: list[str] = []

        if not self._base_dir.exists():
            return deleted

        now = datetime.now(timezone.utc)

        # Collect all state files first to avoid rglob issues during deletion
        state_files = list(self._base_dir.rglob("state.json"))

        for state_file in state_files:
            workspace_root = state_file.parent
            try:
                ws = Workspace(str(workspace_root))
                status = ws.state.status

                # Never delete active workspaces
                if status in ("running", "waiting_for_human", "pending"):
                    continue

                # Only delete completed/failed/archived
                if status not in ("completed", "failed", "archived"):
                    continue

                # Check age
                started = datetime.fromisoformat(ws.state.started_at)
                age_days = (now - started).days

                if age_days >= max_age_days:
                    shutil.rmtree(workspace_root, ignore_errors=True)
                    deleted.append(str(workspace_root))
                    logger.info(
                        "Cleaned up workspace: %s (age=%d days)",
                        ws.state.ticket_id, age_days,
                    )

            except Exception as e:
                logger.warning(
                    "Failed to process workspace at %s for cleanup: %s",
                    workspace_root, e,
                )

        return deleted
