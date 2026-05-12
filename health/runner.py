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

    jira = project.config.tracker.jira
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


def _load_dotenv(path: "Path") -> None:
    """Merge VAR=value pairs from a .env file into os.environ.

    Accepts bare (``VAR=val``) and exported (``export VAR=val``) forms.
    Already-set environment variables win — file entries never override
    an explicit shell export.
    """
    import os

    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(name, value)


def _main() -> int:
    import argparse
    import asyncio
    import sys
    from pathlib import Path
    from config.config_loader import load_config, ConfigError

    parser = argparse.ArgumentParser(
        prog="python -m health.runner",
        description="Run project health checks without starting the daemon.",
    )
    parser.add_argument("--config", required=True, help="Path to config directory")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to a .env file to load (default: ./.env; missing file is ignored)",
    )
    args = parser.parse_args()

    _load_dotenv(Path(args.env_file))

    try:
        _, projects = load_config(args.config)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    if not projects:
        print("No projects found.")
        return 0

    results = asyncio.run(check_all(projects, force=True))
    any_bad = False
    for r in results:
        print(f"[{r.status.upper()}] {r.project_id} — {len(r.checks)} check(s)")
        for c in r.checks:
            mark = "  OK " if c.ok else "  FAIL"
            print(f"  {mark} {c.name}: {c.target}")
            if not c.ok:
                print(f"       reason: {c.reason}")
                if c.fix_hint:
                    print(f"       fix:    {c.fix_hint}")
                any_bad = True
    return 1 if any_bad else 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
