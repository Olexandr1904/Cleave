"""POST /api/projects/create handler.

Wires together payload validation, .env writes, workspace creation,
Atlas supervision, and rollback.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse

from dashboard.atlas_runner import AtlasFn, schedule
from dashboard.env_writer import EnvCollisionError, append_vars, remove_vars
from dashboard.project_create_payload import (
    PayloadValidationError,
    derive_env_vars,
    redact_to_input_md,
    validate_payload,
)
from dashboard.setup_workspace import create_setup_workspace

logger = logging.getLogger(__name__)

_busy: bool = False
_active_workspace: str | None = None


def build_create_route(
    *,
    workspace_base_dir: Path,
    config_dir: Path,
    env_path: Path,
    atlas_fn: AtlasFn,
):
    async def create_project(request: Request) -> JSONResponse:
        global _busy, _active_workspace

        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid_json"}, status_code=400)

        try:
            validate_payload(payload)
        except PayloadValidationError as exc:
            return JSONResponse(
                {"error": "validation_failed", "fields": exc.field_errors},
                status_code=400,
            )

        if _busy:
            return JSONResponse(
                {"error": "busy", "active_workspace": _active_workspace},
                status_code=429,
            )

        project_id = payload["identity"]["project_id"]
        repo_id = payload["identity"]["repo_id"]

        project_config_dir = config_dir / "projects" / project_id
        if project_config_dir.exists():
            return JSONResponse(
                {"error": "project_exists", "project_id": project_id},
                status_code=409,
            )

        env_vars = derive_env_vars(payload)
        try:
            append_vars(env_path, env_vars)
        except EnvCollisionError as exc:
            return JSONResponse(
                {"error": "env_var_conflict", "vars": exc.vars},
                status_code=409,
            )

        for name, value in env_vars.items():
            os.environ[name] = value

        workspace = create_setup_workspace(
            base_dir=workspace_base_dir,
            project_id=project_id,
            repo_id=repo_id,
            redacted_input_md=redact_to_input_md(payload),
        )

        _busy = True
        _active_workspace = f"{project_id}/{repo_id}/setup"

        def rollback() -> None:
            if project_config_dir.exists():
                shutil.rmtree(project_config_dir, ignore_errors=True)
            remove_vars(env_path, list(env_vars.keys()))
            for name in env_vars:
                os.environ.pop(name, None)

        def clear_busy() -> None:
            global _busy, _active_workspace
            _busy = False
            _active_workspace = None

        schedule(workspace, config_dir, atlas_fn, rollback, clear_busy)

        return JSONResponse(
            {
                "workspace": f"{project_id}/{repo_id}/setup",
                "state": "SETUP_PENDING",
            },
            status_code=202,
        )

    return create_project
