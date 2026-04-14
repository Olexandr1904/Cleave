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
    """Manages workspace lifecycle: create, discover, cleanup.

    Directory layout (architecture-v2 §3.1):
        /<base_dir>/<company_id>/<repo_id>/tickets/<ticket_id>/
            meta/
            reports/
            logs/
            source/    (git clone — deleted after merge)
            state.json
    """

    def __init__(self, base_dir: str) -> None:
        self._base_dir = Path(base_dir)
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            raise WorkspaceError(
                f"Cannot create workspace directory '{base_dir}'. "
                f"Please create it and set ownership:\n"
                f"  sudo mkdir -p {base_dir}\n"
                f"  sudo chown $USER {base_dir}"
            )

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def create(
        self,
        company_id: str,
        repo_id: str,
        ticket_id: str,
        clone_url: str,
        clone_depth: int = 0,
        default_branch: str = "develop",
        branch_prefix: str = "feature",
    ) -> Workspace:
        """Create a new isolated workspace with a fresh git clone.

        Args:
            company_id: Company identifier (e.g., "acme").
            repo_id: Repository identifier (e.g., "acme-mobile").
            ticket_id: Ticket identifier (e.g., "ACME-14567").
            clone_url: Git URL to clone.
            clone_depth: Shallow clone depth (0 = full clone).
            default_branch: Branch to checkout after clone.
            branch_prefix: Prefix for feature branch.

        Returns:
            A Workspace object pointing to the new workspace.

        Raises:
            WorkspaceError: If clone fails (workspace is cleaned up).
        """
        workspace_root = (
            self._base_dir / company_id / repo_id / "tickets" / ticket_id
        )

        try:
            # Create workspace directories
            workspace_root.mkdir(parents=True, exist_ok=True)
            (workspace_root / "meta").mkdir(exist_ok=True)
            (workspace_root / "reports").mkdir(exist_ok=True)
            (workspace_root / "logs").mkdir(exist_ok=True)

            # Git clone into source/
            source_dir = workspace_root / "source"
            clone_cmd = ["git", "clone"]
            if clone_depth > 0:
                clone_cmd.extend(["--depth", str(clone_depth)])
            clone_cmd.extend([clone_url, str(source_dir)])

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

            # Checkout default branch and create feature branch
            slug = ticket_id.lower().replace(" ", "-")[:50]
            branch_name = f"{branch_prefix}/{ticket_id}-{slug}"

            subprocess.run(
                ["git", "checkout", default_branch],
                cwd=str(source_dir),
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT,
            )

            subprocess.run(
                ["git", "checkout", "-b", branch_name],
                cwd=str(source_dir),
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT,
            )

            # Create initial state
            state = WorkspaceState(
                ticket_id=ticket_id,
                company_id=company_id,
                repo_id=repo_id,
                workspace_root=str(workspace_root),
                branch=branch_name,
            )

            workspace = Workspace(str(workspace_root), state)
            workspace.save_state()

            logger.info(
                "Workspace created: %s/%s/%s at %s",
                company_id, repo_id, ticket_id, workspace_root,
            )
            return workspace

        except (WorkspaceError, subprocess.TimeoutExpired) as e:
            # Clean up on failure
            if workspace_root.exists():
                shutil.rmtree(workspace_root, ignore_errors=True)
            if isinstance(e, subprocess.TimeoutExpired):
                raise WorkspaceError(
                    f"Git clone timed out after {SUBPROCESS_TIMEOUT}s"
                ) from e
            raise

    def discover_workspaces(self) -> list[Workspace]:
        """Discover existing workspaces by scanning for state.json files.

        Returns workspaces with active states (not DONE/ARCHIVED).
        """
        active_workspaces: list[Workspace] = []

        if not self._base_dir.exists():
            return active_workspaces

        terminal_states = {"DONE", "ARCHIVED"}

        for state_file in self._base_dir.rglob("state.json"):
            workspace_root = state_file.parent
            try:
                ws = Workspace(str(workspace_root))
                current = ws.state.current_state
                if current not in terminal_states:
                    active_workspaces.append(ws)
                    logger.info(
                        "Discovered active workspace: %s (state=%s)",
                        ws.state.ticket_id, current,
                    )
            except Exception as e:
                logger.warning(
                    "Failed to load workspace at %s: %s", workspace_root, e
                )

        return active_workspaces

    def cleanup_source(self, workspace: Workspace) -> None:
        """Delete only the source/ directory (after merge).

        Preserves meta/, reports/, logs/, and state.json for history.
        """
        source_dir = workspace.source_dir
        if source_dir.exists():
            shutil.rmtree(source_dir, ignore_errors=True)
            logger.info(
                "Cleaned up source for workspace: %s", workspace.state.ticket_id
            )

    def cleanup_old_workspaces(self, max_age_days: int) -> list[str]:
        """Delete full workspace dirs with ARCHIVED state older than max_age_days.

        Active and DONE/FAILED workspaces are never fully deleted.
        Only ARCHIVED workspaces (source already cleaned) are removed.

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
                current = ws.state.current_state

                # Only fully delete ARCHIVED workspaces
                if current != "ARCHIVED":
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
