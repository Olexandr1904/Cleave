"""Spawn and supervise the Atlas project-setup agent as a background task."""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path

from dashboard.setup_workspace import SetupWorkspace, write_state

logger = logging.getLogger(__name__)

AtlasFn = Callable[[SetupWorkspace, Path], Awaitable[None]]


async def run_supervised(
    workspace: SetupWorkspace,
    config_dir: Path,
    atlas_fn: AtlasFn,
    on_failure: Callable[[], None],
    on_complete: Callable[[], None],
) -> None:
    """Run Atlas in VALIDATING → WRITING → SETUP_DONE.

    On any exception, writes SETUP_FAILED with the exception, appends a
    failure report to reports/project-setup-output.md, and calls on_failure
    for rollback (removing env vars, partial configs, etc.).

    Calls on_complete in a finally block for both success and failure paths.
    """
    try:
        try:
            write_state(workspace, "VALIDATING")
            await atlas_fn(workspace, config_dir)
            write_state(workspace, "SETUP_DONE")
        except Exception as exc:
            logger.exception("Atlas run failed for %s/%s",
                             workspace.project_id, workspace.repo_id)
            report = workspace.setup_dir / "reports" / "project-setup-output.md"
            existing = report.read_text(encoding="utf-8") if report.exists() else ""
            report.write_text(
                existing
                + "\n\n## Failure\n\n"
                + f"```\n{traceback.format_exc()}\n```\n",
                encoding="utf-8",
            )
            write_state(workspace, "SETUP_FAILED", error=str(exc))
            try:
                on_failure()
            except Exception:
                logger.exception("Rollback failed for %s/%s",
                                 workspace.project_id, workspace.repo_id)
    finally:
        try:
            on_complete()
        except Exception:
            logger.exception("on_complete callback failed for %s/%s",
                             workspace.project_id, workspace.repo_id)


def schedule(
    workspace: SetupWorkspace,
    config_dir: Path,
    atlas_fn: AtlasFn,
    on_failure: Callable[[], None],
    on_complete: Callable[[], None],
) -> asyncio.Task:
    return asyncio.create_task(
        run_supervised(workspace, config_dir, atlas_fn, on_failure, on_complete)
    )
