"""Project-level health aggregation with in-process caching.

Collects validator results per project and exposes a cached
snapshot for the dashboard and other consumers.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from health.validators import (
    ValidatorResult,
    check_jira,
    check_github,
    check_gitlab,
)


@dataclass
class ProjectHealth:
    project_id: str
    checks: list[ValidatorResult]
    checked_at: datetime

    @property
    def status(self) -> str:
        """Aggregate status: 'red' | 'yellow' | 'green'."""
        red_names = {"jira", "github", "gitlab"}
        yellow_names = {"git_identity", "git_remote"}
        has_red = any(not c.ok and c.name in red_names for c in self.checks)
        has_yellow = any(not c.ok and c.name in yellow_names for c in self.checks)
        if has_red:
            return "red"
        if has_yellow:
            return "yellow"
        return "green"


async def check_project(project_id: str, project: Any) -> ProjectHealth:
    """Run all applicable validators for a single project."""
    checks: list[ValidatorResult] = []

    jira = project.config.jira
    if getattr(jira, "url", ""):
        checks.append(await check_jira(
            url=jira.url,
            email=getattr(jira, "email", ""),
            token=getattr(jira, "token", ""),
            project_key=getattr(jira, "project_key", ""),
        ))

    for _, repo_cfg in project.repos.items():
        vcs = repo_cfg.vcs
        provider = getattr(vcs, "provider", "")
        if provider == "github":
            gh = vcs.github
            if getattr(gh, "token", ""):
                checks.append(await check_github(
                    token=gh.token, owner=gh.owner, repo=gh.repo,
                ))
        elif provider == "gitlab":
            gl = vcs.gitlab
            if getattr(gl, "token", ""):
                checks.append(await check_gitlab(
                    token=gl.token,
                    project_id=getattr(gl, "project_id", ""),
                    url=getattr(gl, "url", "https://gitlab.com"),
                ))

    return ProjectHealth(
        project_id=project_id,
        checks=checks,
        checked_at=datetime.now(timezone.utc),
    )


class HealthRunner:
    """Cached, concurrent project health runner.

    TTL defaults to 60s.
    """

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self._ttl = ttl_seconds
        self._cache: list[ProjectHealth] | None = None
        self._cache_at: float = 0.0
        self._lock = asyncio.Lock()

    async def check_all(
        self, projects: dict[str, Any], force: bool = False,
    ) -> list[ProjectHealth]:
        async with self._lock:
            now = time.monotonic()
            if not force and self._cache is not None and now - self._cache_at < self._ttl:
                return self._cache

            results = await asyncio.gather(
                *[check_project(pid, proj) for pid, proj in projects.items()]
            )
            self._cache = list(results)
            self._cache_at = now
            return self._cache


_default_runner = HealthRunner()


async def check_all(projects: dict[str, Any], force: bool = False) -> list[ProjectHealth]:
    """Convenience wrapper around the default module-level runner."""
    return await _default_runner.check_all(projects, force=force)
