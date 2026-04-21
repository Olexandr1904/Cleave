# Project Health Checks + Stage Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the "agent ran but didn't commit" failure mode an edge case — mechanical post-stage verification transitions workspaces to BLOCKED instead of silently drifting to AWAITING_APPROVAL — and surface per-project health (Jira / vcs / git identity) on the dashboard.

**Architecture:** A new `health/` module holds pure validators that are consumed by (1) a dashboard `/api/projects/health` endpoint, (2) a new `orchestrator/stage_verifier.py` that asserts each stage produced its expected side-effect, and (3) a new `validate_git_identity` sandbox tool for the project-setup-agent. Validators never raise — they return structured `ValidatorResult` dataclasses.

**Tech Stack:** Python 3.10+, asyncio, starlette, httpx, pytest, pytest-playwright, subprocess for `git` commands.

**Spec:** [docs/superpowers/specs/2026-04-15-project-health-and-stage-verification-design.md](../specs/2026-04-15-project-health-and-stage-verification-design.md)

---

## File Structure

**New files:**
- `health/__init__.py` — module marker
- `health/validators.py` — pure functions returning `ValidatorResult`
- `health/runner.py` — project aggregation + 60s in-process cache
- `orchestrator/stage_verifier.py` — mechanical post-stage assertions
- `tests/unit/test_health_validators.py`
- `tests/unit/test_health_runner.py`
- `tests/unit/test_stage_verifier.py`
- `tests/unit/test_dashboard_health_api.py`
- `tests/e2e/test_dashboard_health.py`

**Modified files:**
- `orchestrator/orchestrator.py` — `_handle_agent_stage` calls stage verifier before outcome parsing
- `orchestrator/tool_sandbox.py` — register `validate_git_identity` tool
- `dashboard/web.py` — add `/api/projects/health` route
- `dashboard/static/js/board.js` — render health strip
- `dashboard/static/style.css` — health strip styles
- `dashboard/static/js/api.js` — add `loadHealth()` helper
- `main.py` — warm health cache at startup
- `agents/project-setup-agent.md` — checklist enforces all validators
- `docs/setup-guide.md` — Prerequisites section
- `docs/features/dashboard.md` — changelog entry
- `docs/features/index.md` — new feature entry

---

## Task 1: Validator library — `ValidatorResult` + module skeleton

**Files:**
- Create: `health/__init__.py`
- Create: `health/validators.py`
- Create: `tests/unit/test_health_validators.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_health_validators.py
from __future__ import annotations

from health.validators import ValidatorResult


def test_validator_result_ok_shape():
    r = ValidatorResult(ok=True, name="jira", target="ACME", reason="", fix_hint="")
    assert r.ok is True
    assert r.name == "jira"
    assert r.target == "ACME"
    assert r.reason == ""
    assert r.fix_hint == ""


def test_validator_result_failure_shape():
    r = ValidatorResult(
        ok=False,
        name="git_identity",
        target="/tmp/ws",
        reason="user.email not set",
        fix_hint="git config --global user.email <you@company>",
    )
    assert r.ok is False
    assert "user.email" in r.reason
    assert "git config" in r.fix_hint
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_health_validators.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'health'`

- [ ] **Step 3: Create `health/__init__.py`**

```python
# health/__init__.py
"""Project health check library — pure validators returning ValidatorResult."""
```

- [ ] **Step 4: Create `health/validators.py` with the dataclass**

```python
# health/validators.py
"""Pure validators for project health checks.

Every validator returns a ValidatorResult. Validators MUST NOT raise;
unexpected failures are caught and returned as ok=False with the
exception class in `reason`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ValidatorResult:
    """Structured result of a single health check.

    Attributes:
        ok: True if the check passed.
        name: Validator identifier (e.g. "jira", "github", "git_identity").
        target: What was checked (e.g. "ACME project", "/ws/acme/acme-app").
        reason: Human-readable error if ok=False, empty string otherwise.
        fix_hint: Copyable command or instruction to resolve the failure,
            empty string if ok or no actionable fix.
    """
    ok: bool
    name: str
    target: str
    reason: str
    fix_hint: str
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_health_validators.py -v`
Expected: PASS, 2 tests

- [ ] **Step 6: Commit**

```bash
git add health/__init__.py health/validators.py tests/unit/test_health_validators.py
git commit -m "feat(health): add ValidatorResult dataclass and health module skeleton"
```

---

## Task 2: Validators — `check_jira`, `check_github`, `check_gitlab` (wrappers over existing `config_tools`)

**Files:**
- Modify: `health/validators.py`
- Modify: `tests/unit/test_health_validators.py`

- [ ] **Step 1: Add failing tests for the three wrappers**

Append to `tests/unit/test_health_validators.py`:

```python
import pytest
from unittest.mock import patch, AsyncMock

from health.validators import check_jira, check_github, check_gitlab


class TestCheckJira:
    @pytest.mark.asyncio
    async def test_success(self):
        with patch("health.validators.config_tools.validate_jira",
                   new=AsyncMock(return_value={"success": True, "project_name": "Acme"})):
            r = await check_jira("https://acme.atlassian.net", "me@x", "tok", "ACME")
        assert r.ok is True
        assert r.name == "jira"
        assert r.target == "ACME"
        assert r.reason == ""

    @pytest.mark.asyncio
    async def test_auth_failure(self):
        with patch("health.validators.config_tools.validate_jira",
                   new=AsyncMock(return_value={"success": False, "error": "HTTP 401"})):
            r = await check_jira("https://x", "me@x", "bad", "ACME")
        assert r.ok is False
        assert "401" in r.reason
        assert r.fix_hint  # non-empty

    @pytest.mark.asyncio
    async def test_wrapper_does_not_raise(self):
        with patch("health.validators.config_tools.validate_jira",
                   new=AsyncMock(side_effect=RuntimeError("boom"))):
            r = await check_jira("https://x", "me@x", "tok", "ACME")
        assert r.ok is False
        assert "boom" in r.reason or "RuntimeError" in r.reason


class TestCheckGithub:
    @pytest.mark.asyncio
    async def test_success(self):
        with patch("health.validators.config_tools.validate_github",
                   new=AsyncMock(return_value={"success": True, "full_name": "acme/mb", "default_branch": "main"})):
            r = await check_github("tok", "acme", "mb")
        assert r.ok is True
        assert r.name == "github"
        assert r.target == "acme/mb"

    @pytest.mark.asyncio
    async def test_auth_failure(self):
        with patch("health.validators.config_tools.validate_github",
                   new=AsyncMock(return_value={"success": False, "error": "HTTP 401"})):
            r = await check_github("bad", "acme", "mb")
        assert r.ok is False
        assert "401" in r.reason


class TestCheckGitlab:
    @pytest.mark.asyncio
    async def test_success(self):
        with patch("health.validators.config_tools.validate_gitlab",
                   new=AsyncMock(return_value={"success": True, "project_name": "mb"})):
            r = await check_gitlab("tok", "123", "https://gitlab.example.com")
        assert r.ok is True
        assert r.name == "gitlab"
        assert r.target == "123"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_health_validators.py -v`
Expected: FAIL with `ImportError: cannot import name 'check_jira'`

- [ ] **Step 3: Add wrapper functions to `health/validators.py`**

Append:

```python
from integrations.config import config_tools


async def check_jira(url: str, email: str, token: str, project_key: str) -> ValidatorResult:
    """Check Jira API access for a project."""
    try:
        result = await config_tools.validate_jira(
            url=url, token=token, email=email, project_key=project_key,
        )
    except Exception as e:
        return ValidatorResult(
            ok=False, name="jira", target=project_key,
            reason=f"{type(e).__name__}: {e}",
            fix_hint="Check Jira URL, email, and token in project config",
        )
    if result.get("success"):
        return ValidatorResult(ok=True, name="jira", target=project_key, reason="", fix_hint="")
    return ValidatorResult(
        ok=False, name="jira", target=project_key,
        reason=result.get("error", "Jira check failed"),
        fix_hint="Verify Jira URL, email, token, and project key in project config",
    )


async def check_github(token: str, owner: str, repo: str) -> ValidatorResult:
    """Check GitHub API access for a repo."""
    target = f"{owner}/{repo}"
    try:
        result = await config_tools.validate_github(token=token, owner=owner, repo=repo)
    except Exception as e:
        return ValidatorResult(
            ok=False, name="github", target=target,
            reason=f"{type(e).__name__}: {e}",
            fix_hint="Check GitHub token and repo name in project config",
        )
    if result.get("success"):
        return ValidatorResult(ok=True, name="github", target=target, reason="", fix_hint="")
    return ValidatorResult(
        ok=False, name="github", target=target,
        reason=result.get("error", "GitHub check failed"),
        fix_hint=f"Verify GitHub token has access to {target}",
    )


async def check_gitlab(token: str, project_id: str, url: str = "https://gitlab.com") -> ValidatorResult:
    """Check GitLab API access for a project."""
    try:
        result = await config_tools.validate_gitlab(token=token, project_id=project_id, url=url)
    except Exception as e:
        return ValidatorResult(
            ok=False, name="gitlab", target=project_id,
            reason=f"{type(e).__name__}: {e}",
            fix_hint="Check GitLab token, URL, and project id in project config",
        )
    if result.get("success"):
        return ValidatorResult(ok=True, name="gitlab", target=project_id, reason="", fix_hint="")
    return ValidatorResult(
        ok=False, name="gitlab", target=project_id,
        reason=result.get("error", "GitLab check failed"),
        fix_hint=f"Verify GitLab token has access to project {project_id}",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_health_validators.py -v`
Expected: PASS, 9 tests total

- [ ] **Step 5: Commit**

```bash
git add health/validators.py tests/unit/test_health_validators.py
git commit -m "feat(health): add jira/github/gitlab validators over config_tools"
```

---

## Task 3: Validator — `check_git_identity`

**Files:**
- Modify: `health/validators.py`
- Modify: `tests/unit/test_health_validators.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_health_validators.py`:

```python
import subprocess
from pathlib import Path

from health.validators import check_git_identity


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


class TestCheckGitIdentity:
    def test_identity_set_via_local_config(self, tmp_path):
        repo = _init_repo(tmp_path)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=repo, check=True)
        r = check_git_identity(repo)
        assert r.ok is True
        assert r.name == "git_identity"
        assert r.reason == ""

    def test_identity_missing(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path)
        # Point HOME at empty dir so global config doesn't leak in.
        monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
        (tmp_path / "empty_home").mkdir()
        r = check_git_identity(repo)
        assert r.ok is False
        assert "user.email" in r.reason or "user.name" in r.reason
        assert "git config" in r.fix_hint

    def test_not_a_git_dir(self, tmp_path):
        r = check_git_identity(tmp_path)  # plain dir, not a repo
        assert r.ok is False
        assert "git" in r.reason.lower()

    def test_does_not_raise_on_missing_git_binary(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", "/nonexistent")
        r = check_git_identity(tmp_path)
        assert r.ok is False  # returns structured failure, does not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_health_validators.py::TestCheckGitIdentity -v`
Expected: FAIL with `ImportError: cannot import name 'check_git_identity'`

- [ ] **Step 3: Implement `check_git_identity` in `health/validators.py`**

Append:

```python
import subprocess
from pathlib import Path


def _git_config(workspace_root: Path, key: str) -> tuple[bool, str]:
    """Run `git -C <workspace> config <key>` and return (ok, value_or_error)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace_root), "config", key],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return False, "git binary not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "git config timed out"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if result.returncode == 0:
        return True, result.stdout.strip()
    stderr = result.stderr.strip() or f"exit {result.returncode}"
    return False, stderr


def check_git_identity(workspace_root: Path) -> ValidatorResult:
    """Check that git will accept commits in the given workspace.

    Reads user.name and user.email with `git config` (which resolves
    local → global → system in the same order git commit does).
    """
    target = str(workspace_root)
    fix_hint = (
        'git config --global user.name "Your Name" && '
        'git config --global user.email <you@company.com>'
    )

    if not workspace_root.exists():
        return ValidatorResult(
            ok=False, name="git_identity", target=target,
            reason="workspace directory does not exist", fix_hint=fix_hint,
        )

    name_ok, name_val = _git_config(workspace_root, "user.name")
    email_ok, email_val = _git_config(workspace_root, "user.email")

    if not name_ok or not name_val:
        return ValidatorResult(
            ok=False, name="git_identity", target=target,
            reason=f"git user.name not set ({name_val or 'empty'})",
            fix_hint=fix_hint,
        )
    if not email_ok or not email_val:
        return ValidatorResult(
            ok=False, name="git_identity", target=target,
            reason=f"git user.email not set ({email_val or 'empty'})",
            fix_hint=fix_hint,
        )
    return ValidatorResult(ok=True, name="git_identity", target=target, reason="", fix_hint="")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_health_validators.py::TestCheckGitIdentity -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit**

```bash
git add health/validators.py tests/unit/test_health_validators.py
git commit -m "feat(health): add check_git_identity validator"
```

---

## Task 4: Validator — `check_git_remote`

**Files:**
- Modify: `health/validators.py`
- Modify: `tests/unit/test_health_validators.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_health_validators.py`:

```python
from health.validators import check_git_remote


class TestCheckGitRemote:
    def test_reachable_remote(self, tmp_path):
        # Create a bare repo to act as a remote, and clone it
        bare = tmp_path / "bare.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
        r = check_git_remote(clone)
        assert r.ok is True
        assert r.name == "git_remote"

    def test_unreachable_remote(self, tmp_path):
        repo = _init_repo(tmp_path)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://10.255.255.1/nonexistent.git"],
            cwd=repo, check=True,
        )
        r = check_git_remote(repo)
        assert r.ok is False
        assert r.reason  # non-empty

    def test_no_remote_configured(self, tmp_path):
        repo = _init_repo(tmp_path)
        r = check_git_remote(repo)
        assert r.ok is False
        assert "remote" in r.reason.lower() or "origin" in r.reason.lower()

    def test_not_a_git_dir(self, tmp_path):
        r = check_git_remote(tmp_path)
        assert r.ok is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_health_validators.py::TestCheckGitRemote -v`
Expected: FAIL with `ImportError: cannot import name 'check_git_remote'`

- [ ] **Step 3: Implement `check_git_remote` in `health/validators.py`**

Append:

```python
def check_git_remote(workspace_root: Path, remote: str = "origin") -> ValidatorResult:
    """Check that the configured git remote accepts read auth.

    Uses `git ls-remote <remote> HEAD` — this is a lightweight operation
    that validates network reach + auth without fetching any refs.
    """
    target = f"{workspace_root}:{remote}"
    fix_hint = (
        "Verify the remote is set and authentication works: "
        "git -C <workspace> remote -v && git -C <workspace> ls-remote origin HEAD"
    )

    if not workspace_root.exists():
        return ValidatorResult(
            ok=False, name="git_remote", target=target,
            reason="workspace directory does not exist", fix_hint=fix_hint,
        )

    try:
        result = subprocess.run(
            ["git", "-C", str(workspace_root), "ls-remote", remote, "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return ValidatorResult(
            ok=False, name="git_remote", target=target,
            reason="git binary not found on PATH", fix_hint=fix_hint,
        )
    except subprocess.TimeoutExpired:
        return ValidatorResult(
            ok=False, name="git_remote", target=target,
            reason=f"git ls-remote {remote} timed out", fix_hint=fix_hint,
        )
    except Exception as e:
        return ValidatorResult(
            ok=False, name="git_remote", target=target,
            reason=f"{type(e).__name__}: {e}", fix_hint=fix_hint,
        )

    if result.returncode == 0:
        return ValidatorResult(ok=True, name="git_remote", target=target, reason="", fix_hint="")

    stderr = result.stderr.strip().splitlines()
    last_line = stderr[-1] if stderr else f"exit {result.returncode}"
    return ValidatorResult(
        ok=False, name="git_remote", target=target,
        reason=last_line, fix_hint=fix_hint,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_health_validators.py::TestCheckGitRemote -v`
Expected: PASS, 4 tests (note: `test_unreachable_remote` may take up to 15s due to real network timeout — this is acceptable for a unit test that validates timeout behavior)

- [ ] **Step 5: Commit**

```bash
git add health/validators.py tests/unit/test_health_validators.py
git commit -m "feat(health): add check_git_remote validator"
```

---

## Task 5: Health runner — `ProjectHealth` + `check_project` + `check_all` with cache

**Files:**
- Create: `health/runner.py`
- Create: `tests/unit/test_health_runner.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_health_runner.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from health.runner import ProjectHealth, HealthRunner, check_project, check_all
from health.validators import ValidatorResult


def _make_project(company_id="acme", vcs_provider="github"):
    jira = SimpleNamespace(url="https://acme.atlassian.net", email="a@b", token="t", project_key="ACME")
    github = SimpleNamespace(token="gh_tok", owner="acme", repo="acme-app")
    gitlab = SimpleNamespace(token="", url="", project_id="")
    vcs = SimpleNamespace(provider=vcs_provider, github=github, gitlab=gitlab)
    repo_cfg = SimpleNamespace(vcs=vcs)
    config = SimpleNamespace(jira=jira)
    return SimpleNamespace(config=config, repos={"acme-app": repo_cfg})


class TestProjectHealthAggregation:
    def test_all_green(self):
        ph = ProjectHealth(
            project_id="acme",
            checks=[
                ValidatorResult(True, "jira", "ACME", "", ""),
                ValidatorResult(True, "github", "acme/acme-app", "", ""),
                ValidatorResult(True, "git_identity", "/ws", "", ""),
            ],
            checked_at=datetime.now(timezone.utc),
        )
        assert ph.status == "green"

    def test_jira_failing_is_red(self):
        ph = ProjectHealth(
            project_id="acme",
            checks=[ValidatorResult(False, "jira", "ACME", "401", "check token")],
            checked_at=datetime.now(timezone.utc),
        )
        assert ph.status == "red"

    def test_git_identity_failing_is_yellow(self):
        ph = ProjectHealth(
            project_id="acme",
            checks=[
                ValidatorResult(True, "jira", "ACME", "", ""),
                ValidatorResult(True, "github", "acme/mb", "", ""),
                ValidatorResult(False, "git_identity", "/ws", "missing", "git config ..."),
            ],
            checked_at=datetime.now(timezone.utc),
        )
        assert ph.status == "yellow"

    def test_red_beats_yellow(self):
        ph = ProjectHealth(
            project_id="acme",
            checks=[
                ValidatorResult(False, "jira", "ACME", "401", ""),
                ValidatorResult(False, "git_identity", "/ws", "missing", ""),
            ],
            checked_at=datetime.now(timezone.utc),
        )
        assert ph.status == "red"


class TestCheckProject:
    @pytest.mark.asyncio
    async def test_runs_jira_and_github(self):
        proj = _make_project(vcs_provider="github")
        with patch("health.runner.check_jira", new=AsyncMock(return_value=ValidatorResult(True, "jira", "ACME", "", ""))), \
             patch("health.runner.check_github", new=AsyncMock(return_value=ValidatorResult(True, "github", "acme/acme-app", "", ""))):
            ph = await check_project("acme", proj)
        assert ph.project_id == "acme"
        names = {c.name for c in ph.checks}
        assert "jira" in names
        assert "github" in names

    @pytest.mark.asyncio
    async def test_runs_gitlab_when_provider_is_gitlab(self):
        proj = _make_project(vcs_provider="gitlab")
        proj.repos["acme-app"].vcs.gitlab = SimpleNamespace(token="gl", url="https://gl", project_id="42")
        with patch("health.runner.check_jira", new=AsyncMock(return_value=ValidatorResult(True, "jira", "ACME", "", ""))), \
             patch("health.runner.check_gitlab", new=AsyncMock(return_value=ValidatorResult(True, "gitlab", "42", "", ""))):
            ph = await check_project("acme", proj)
        names = {c.name for c in ph.checks}
        assert "gitlab" in names
        assert "github" not in names


class TestCacheBehavior:
    @pytest.mark.asyncio
    async def test_cache_hits_within_ttl(self):
        proj = _make_project()
        call_count = {"jira": 0}

        async def fake_jira(*a, **kw):
            call_count["jira"] += 1
            return ValidatorResult(True, "jira", "ACME", "", "")

        with patch("health.runner.check_jira", side_effect=fake_jira), \
             patch("health.runner.check_github", new=AsyncMock(return_value=ValidatorResult(True, "github", "acme/mb", "", ""))):
            runner = HealthRunner(ttl_seconds=60)
            await runner.check_all({"acme": proj})
            await runner.check_all({"acme": proj})
        assert call_count["jira"] == 1  # second call came from cache

    @pytest.mark.asyncio
    async def test_force_refresh_bypasses_cache(self):
        proj = _make_project()
        call_count = {"jira": 0}

        async def fake_jira(*a, **kw):
            call_count["jira"] += 1
            return ValidatorResult(True, "jira", "ACME", "", "")

        with patch("health.runner.check_jira", side_effect=fake_jira), \
             patch("health.runner.check_github", new=AsyncMock(return_value=ValidatorResult(True, "github", "acme/mb", "", ""))):
            runner = HealthRunner(ttl_seconds=60)
            await runner.check_all({"acme": proj})
            await runner.check_all({"acme": proj}, force=True)
        assert call_count["jira"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_health_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'health.runner'`

- [ ] **Step 3: Implement `health/runner.py`**

```python
# health/runner.py
"""Project-level health aggregation with in-process caching.

Collects validator results per project and exposes a cached
snapshot for the dashboard and other consumers.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
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

    Cache key is the dict identity of the projects map. TTL defaults to 60s.
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


# Module-level singleton for the dashboard to share with main.py
_default_runner = HealthRunner()


async def check_all(projects: dict[str, Any], force: bool = False) -> list[ProjectHealth]:
    """Convenience wrapper around the default module-level runner."""
    return await _default_runner.check_all(projects, force=force)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_health_runner.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add health/runner.py tests/unit/test_health_runner.py
git commit -m "feat(health): add ProjectHealth aggregation and cached runner"
```

---

## Task 6: Stage verifier — `VerifyResult` and `dev` verifier

**Files:**
- Create: `orchestrator/stage_verifier.py`
- Create: `tests/unit/test_stage_verifier.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_stage_verifier.py
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.stage_verifier import VerifyResult, verify, capture_stage_start


def _init_repo_with_commit(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "a.txt").write_text("a")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _fake_workspace(source_dir: Path, reports_dir: Path | None = None) -> MagicMock:
    ws = MagicMock()
    ws.source_dir = source_dir
    ws.reports_dir = reports_dir or (source_dir.parent / "reports")
    return ws


class TestCaptureStageStart:
    def test_captures_current_head(self, tmp_path):
        repo = _init_repo_with_commit(tmp_path)
        ws = _fake_workspace(repo)
        sha = capture_stage_start(ws, "dev")
        assert sha is not None
        assert len(sha) == 40

    def test_returns_none_for_non_verifiable_stage(self, tmp_path):
        repo = _init_repo_with_commit(tmp_path)
        ws = _fake_workspace(repo)
        assert capture_stage_start(ws, "analysis") is None


class TestDevVerifier:
    def test_new_commit_passes(self, tmp_path):
        repo = _init_repo_with_commit(tmp_path)
        ws = _fake_workspace(repo)
        start = capture_stage_start(ws, "dev")
        # Simulate dev-agent making a commit
        (repo / "b.txt").write_text("b")
        subprocess.run(["git", "add", "b.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "feat"], cwd=repo, check=True)

        r = verify("dev", ws, start)
        assert r.ok is True
        assert r.stage_id == "dev"

    def test_no_new_commit_fails(self, tmp_path):
        repo = _init_repo_with_commit(tmp_path)
        ws = _fake_workspace(repo)
        start = capture_stage_start(ws, "dev")
        # No commit was made — simulating the ACME-14595 case

        r = verify("dev", ws, start)
        assert r.ok is False
        assert r.stage_id == "dev"
        assert "commit" in r.reason.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_stage_verifier.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.stage_verifier'`

- [ ] **Step 3: Implement `orchestrator/stage_verifier.py` (dev + skeleton)**

```python
# orchestrator/stage_verifier.py
"""Mechanical post-stage verification.

Each pipeline stage with an expected side-effect has a verifier that
asserts the effect actually happened (e.g. dev must produce a new
commit). The orchestrator calls `verify(...)` after the agent finishes
and transitions the workspace to BLOCKED if the check fails — regardless
of what the agent said in text output.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class VerifyResult:
    ok: bool
    stage_id: str
    reason: str  # empty if ok; human-readable error otherwise


# Stages that mechanically touch git; start commit must be captured for them.
_GIT_STAGES = {"dev", "push"}


def _git_rev_parse(source_dir: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.warning("git rev-parse failed in %s: %s", source_dir, e)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def capture_stage_start(workspace: Any, stage_id: str) -> str | None:
    """Capture HEAD before a stage runs. Returns None for non-verifiable stages."""
    if stage_id not in _GIT_STAGES:
        return None
    return _git_rev_parse(Path(workspace.source_dir))


def verify(stage_id: str, workspace: Any, stage_start_commit: str | None) -> VerifyResult:
    """Run the mechanical verifier for the given stage."""
    if stage_id == "dev":
        return _verify_dev(workspace, stage_start_commit)
    # Other stages added in later tasks
    return VerifyResult(ok=True, stage_id=stage_id, reason="")


def _verify_dev(workspace: Any, stage_start_commit: str | None) -> VerifyResult:
    current = _git_rev_parse(Path(workspace.source_dir))
    if current is None:
        return VerifyResult(
            ok=False, stage_id="dev",
            reason="could not read git HEAD from workspace source_dir",
        )
    if stage_start_commit is None:
        return VerifyResult(
            ok=False, stage_id="dev",
            reason="stage start commit was not captured",
        )
    if current == stage_start_commit:
        return VerifyResult(
            ok=False, stage_id="dev",
            reason=f"no new commit on feature branch (HEAD still at {current[:8]})",
        )
    return VerifyResult(ok=True, stage_id="dev", reason="")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_stage_verifier.py -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit**

```bash
git add orchestrator/stage_verifier.py tests/unit/test_stage_verifier.py
git commit -m "feat(orchestrator): add stage verifier skeleton with dev check"
```

---

## Task 7: Stage verifier — `push`, `scope_check`, `qa`, `pr_review`

**Files:**
- Modify: `orchestrator/stage_verifier.py`
- Modify: `tests/unit/test_stage_verifier.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_stage_verifier.py`:

```python
class TestScopeCheckVerifier:
    def test_report_file_exists_passes(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "scope-guard-agent-output.md").write_text("status: pass\n")
        ws = _fake_workspace(tmp_path / "src", reports)
        r = verify("scope_check", ws, None)
        assert r.ok is True

    def test_report_file_missing_fails(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        ws = _fake_workspace(tmp_path / "src", reports)
        r = verify("scope_check", ws, None)
        assert r.ok is False
        assert "scope-guard-agent-output.md" in r.reason


class TestQaVerifier:
    def test_report_file_exists_passes(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "qa-agent-output.md").write_text("all gates passed")
        ws = _fake_workspace(tmp_path / "src", reports)
        r = verify("qa", ws, None)
        assert r.ok is True

    def test_report_file_missing_fails(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        ws = _fake_workspace(tmp_path / "src", reports)
        r = verify("qa", ws, None)
        assert r.ok is False


class TestPushVerifier:
    def test_push_succeeded(self, tmp_path):
        # Bare remote + clone + commit + push
        bare = tmp_path / "bare.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        repo = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(bare), str(repo)], check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "m"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "-qb", "feature"], cwd=repo, check=True)
        (repo / "g.txt").write_text("y")
        subprocess.run(["git", "add", "g.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "f"], cwd=repo, check=True)
        subprocess.run(["git", "push", "-q", "origin", "feature"], cwd=repo, check=True)

        ws = _fake_workspace(repo)
        ws.state = SimpleNamespace(branch="feature")
        r = verify("push", ws, None)
        assert r.ok is True

    def test_push_not_done(self, tmp_path):
        bare = tmp_path / "bare.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        repo = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(bare), str(repo)], check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "m"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "-qb", "feature"], cwd=repo, check=True)
        (repo / "g.txt").write_text("y")
        subprocess.run(["git", "add", "g.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "f"], cwd=repo, check=True)
        # Note: no push

        ws = _fake_workspace(repo)
        ws.state = SimpleNamespace(branch="feature")
        r = verify("push", ws, None)
        assert r.ok is False
        assert "remote" in r.reason.lower() or "push" in r.reason.lower()


class TestPrReviewVerifier:
    def test_pr_exists_passes(self, tmp_path):
        ws = _fake_workspace(tmp_path / "src")
        ws.state = SimpleNamespace(pr_number=42)
        r = verify("pr_review", ws, None)
        assert r.ok is True

    def test_no_pr_number_fails(self, tmp_path):
        ws = _fake_workspace(tmp_path / "src")
        ws.state = SimpleNamespace(pr_number=None)
        r = verify("pr_review", ws, None)
        assert r.ok is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_stage_verifier.py -v`
Expected: 8 new tests fail; earlier 4 still pass.

- [ ] **Step 3: Add verifiers to `orchestrator/stage_verifier.py`**

Replace the body of `verify(...)` and add helper functions:

```python
def verify(stage_id: str, workspace: Any, stage_start_commit: str | None) -> VerifyResult:
    """Run the mechanical verifier for the given stage."""
    if stage_id == "dev":
        return _verify_dev(workspace, stage_start_commit)
    if stage_id == "scope_check":
        return _verify_report_exists("scope_check", workspace, "scope-guard-agent-output.md")
    if stage_id == "qa":
        return _verify_report_exists("qa", workspace, "qa-agent-output.md")
    if stage_id == "push":
        return _verify_push(workspace)
    if stage_id == "pr_review":
        return _verify_pr_review(workspace)
    return VerifyResult(ok=True, stage_id=stage_id, reason="")


def _verify_report_exists(stage_id: str, workspace: Any, filename: str) -> VerifyResult:
    reports_dir = Path(workspace.reports_dir)
    if not (reports_dir / filename).exists():
        return VerifyResult(
            ok=False, stage_id=stage_id,
            reason=f"{filename} was not produced in reports/",
        )
    return VerifyResult(ok=True, stage_id=stage_id, reason="")


def _verify_push(workspace: Any) -> VerifyResult:
    source = Path(workspace.source_dir)
    branch = getattr(workspace.state, "branch", None)
    if not branch:
        return VerifyResult(
            ok=False, stage_id="push",
            reason="no branch set on workspace state",
        )

    # Local HEAD on the branch
    local = _git_rev_parse(source)
    if local is None:
        return VerifyResult(
            ok=False, stage_id="push",
            reason="could not read local HEAD",
        )

    # Remote ref
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "ls-remote", "origin", f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return VerifyResult(
            ok=False, stage_id="push",
            reason=f"git ls-remote failed: {type(e).__name__}: {e}",
        )
    if result.returncode != 0:
        return VerifyResult(
            ok=False, stage_id="push",
            reason=f"git ls-remote origin failed: {result.stderr.strip()[:200]}",
        )

    line = result.stdout.strip()
    if not line:
        return VerifyResult(
            ok=False, stage_id="push",
            reason=f"remote has no ref refs/heads/{branch} (branch not pushed)",
        )
    remote_sha = line.split()[0]
    if remote_sha != local:
        return VerifyResult(
            ok=False, stage_id="push",
            reason=f"remote branch {branch} is at {remote_sha[:8]}, local at {local[:8]}",
        )
    return VerifyResult(ok=True, stage_id="push", reason="")


def _verify_pr_review(workspace: Any) -> VerifyResult:
    pr_number = getattr(workspace.state, "pr_number", None)
    if not pr_number:
        return VerifyResult(
            ok=False, stage_id="pr_review",
            reason="workspace state has no pr_number (PR not created)",
        )
    return VerifyResult(ok=True, stage_id="pr_review", reason="")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_stage_verifier.py -v`
Expected: PASS, 12 tests total.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/stage_verifier.py tests/unit/test_stage_verifier.py
git commit -m "feat(orchestrator): add scope_check/qa/push/pr_review stage verifiers"
```

---

## Task 8: Orchestrator integration — capture stage start + call verify + transition to BLOCKED

**Files:**
- Modify: `orchestrator/orchestrator.py`
- Create: `tests/unit/test_orchestrator_stage_verify.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/test_orchestrator_stage_verify.py
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "a.txt").write_text("a")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


@pytest.mark.asyncio
async def test_dev_stage_without_new_commit_goes_to_blocked(tmp_path):
    """Reproducer for ACME-14595: dev agent ran, said 'Tests pass',
    but made no commit. Workspace must land in BLOCKED, not advance."""
    from orchestrator.orchestrator import Orchestrator

    repo = _init_repo(tmp_path)

    # Minimal workspace mock
    ws = MagicMock()
    ws.source_dir = repo
    ws.reports_dir = tmp_path / "reports"
    ws.reports_dir.mkdir()
    ws.state = SimpleNamespace(
        ticket_id="T-1",
        company_id="acme",
        repo_id="acme-app",
        current_state="DEV",
        previous_state="ANALYSIS",
        stage_iterations={},
        branch="feature/t-1",
        error=None,
    )
    ws.transition = MagicMock()
    ws.update_state = MagicMock()
    ws.increment_iteration = MagicMock()

    # Minimal orchestrator dependencies
    workflow = MagicMock()
    stage_def = SimpleNamespace(agent="dev-agent", action=None, max_iterations=3)
    workflow.stages = {"dev": stage_def}

    orch = Orchestrator.__new__(Orchestrator)
    orch._workflow = workflow
    orch._dry_run = False
    orch._events = None
    orch._notifier = None
    orch._agent_runtime = MagicMock()
    orch._agent_runtime.execute = AsyncMock(
        return_value=SimpleNamespace(
            success=True, output="Tests pass", duration_seconds=1.0,
            input_tokens=0, output_tokens=0, failure_kind=None, error=None, retry_at=None,
        )
    )
    orch._get_repo_config = MagicMock(return_value=None)
    orch._emit = MagicMock()
    orch._parse_agent_outcome = MagicMock(return_value="default")
    orch._should_approval_gate = MagicMock(return_value=False)
    orch._advance_to_stage = MagicMock()

    await orch._handle_agent_stage(ws, "dev", stage_def)

    # Assert: transitioned to BLOCKED, error set, did NOT advance
    ws.transition.assert_called_once()
    args, kwargs = ws.transition.call_args
    assert args[0] == "BLOCKED"
    orch._advance_to_stage.assert_not_called()
    # Error should mention "no new commit"
    assert ws.update_state.called
    error_arg = ws.update_state.call_args.kwargs.get("error", "")
    assert "commit" in error_arg.lower()


@pytest.mark.asyncio
async def test_dev_stage_with_new_commit_advances_normally(tmp_path):
    from orchestrator.orchestrator import Orchestrator

    repo = _init_repo(tmp_path)

    # Set up so the dev-agent "makes a commit" mid-call
    async def fake_execute(*a, **kw):
        (repo / "b.txt").write_text("b")
        subprocess.run(["git", "add", "b.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "feat"], cwd=repo, check=True)
        return SimpleNamespace(
            success=True, output="Tests pass", duration_seconds=1.0,
            input_tokens=0, output_tokens=0, failure_kind=None, error=None, retry_at=None,
        )

    ws = MagicMock()
    ws.source_dir = repo
    ws.reports_dir = tmp_path / "reports"
    ws.reports_dir.mkdir()
    ws.state = SimpleNamespace(
        ticket_id="T-1", company_id="acme", repo_id="acme-app",
        current_state="DEV", previous_state="ANALYSIS",
        stage_iterations={}, branch="feature/t-1", error=None,
    )
    ws.transition = MagicMock()
    ws.update_state = MagicMock()
    ws.increment_iteration = MagicMock()

    workflow = MagicMock()
    stage_def = SimpleNamespace(agent="dev-agent", action=None, max_iterations=3)
    workflow.stages = {"dev": stage_def}

    orch = Orchestrator.__new__(Orchestrator)
    orch._workflow = workflow
    orch._dry_run = False
    orch._events = None
    orch._notifier = None
    orch._agent_runtime = MagicMock()
    orch._agent_runtime.execute = AsyncMock(side_effect=fake_execute)
    orch._get_repo_config = MagicMock(return_value=None)
    orch._emit = MagicMock()
    orch._parse_agent_outcome = MagicMock(return_value="default")
    orch._should_approval_gate = MagicMock(return_value=False)
    orch._advance_to_stage = MagicMock()

    with patch("orchestrator.orchestrator.get_next_stage", return_value="scope_check"):
        await orch._handle_agent_stage(ws, "dev", stage_def)

    # Assert: advanced to scope_check, NOT blocked
    orch._advance_to_stage.assert_called_once()
    blocked_calls = [c for c in ws.transition.call_args_list if c.args and c.args[0] == "BLOCKED"]
    assert blocked_calls == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_orchestrator_stage_verify.py -v`
Expected: FAIL — first test asserts BLOCKED but the current code advances normally.

- [ ] **Step 3: Modify `_handle_agent_stage` in `orchestrator/orchestrator.py`**

Open [orchestrator/orchestrator.py](../../../orchestrator/orchestrator.py). At the top of the file, add the import:

```python
from orchestrator import stage_verifier
```

Then in `_handle_agent_stage` (around line 496), modify to capture stage start *before* dispatch and verify *after* the agent returns successfully. Replace the section from `workspace.increment_iteration(stage_id)` through the `outcome = self._parse_agent_outcome(...)` line with:

```python
        workspace.increment_iteration(stage_id)

        # Capture git HEAD before the stage runs so we can verify side-effects.
        stage_start_commit = stage_verifier.capture_stage_start(workspace, stage_id)

        repo_config = self._get_repo_config(workspace)
        protected = repo_config.architecture.protected_files if repo_config else []

        self._emit("agent_dispatched", f"Dispatching {stage_def.agent} for {state.ticket_id}", project_id=state.company_id, ticket_id=state.ticket_id, agent_id=stage_def.agent, data={"stage": stage_id})
        result = await self._agent_runtime.execute(
            stage_def.agent, workspace, protected_files=protected,
        )

        if not result.success:
            # (existing failure-handling block unchanged)
            self._emit(
                "agent_failed",
                f"{stage_def.agent} failed for {state.ticket_id}: {result.error}",
                project_id=state.company_id, ticket_id=state.ticket_id,
                agent_id=stage_def.agent,
                data={"stage": stage_id, "error": result.error},
            )
            if result.failure_kind == "quota":
                self._rollback_iteration(workspace, stage_id)
                retry_at = result.retry_at or (
                    datetime.now(timezone.utc) + DEFAULT_QUOTA_RETRY_DELAY
                )
                workspace.transition("DEFERRED", retry_at=retry_at.isoformat())
                await self._notify_deferred(workspace, retry_at)
            else:
                workspace.transition("FAILED")
                workspace.update_state(error=result.error)
                await self._notify_failed(workspace, result.error or "")
            return

        self._emit("agent_completed", f"{stage_def.agent} completed for {state.ticket_id}", project_id=state.company_id, ticket_id=state.ticket_id, agent_id=stage_def.agent, data={"stage": stage_id, "duration": result.duration_seconds, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens})

        # Mechanical verification — hard gate before outcome routing.
        verify_result = stage_verifier.verify(stage_id, workspace, stage_start_commit)
        if not verify_result.ok:
            agent_snippet = (result.output or "")[:200].replace("\n", " ")
            error_msg = f"{stage_id}: {verify_result.reason} (agent said: {agent_snippet})"
            workspace.transition("BLOCKED")
            workspace.update_state(error=error_msg)
            self._emit(
                "stage_verification_failed",
                f"{stage_id} verification failed for {state.ticket_id}: {verify_result.reason}",
                project_id=state.company_id, ticket_id=state.ticket_id,
                data={"stage": stage_id, "reason": verify_result.reason},
            )
            return

        # Determine outcome from agent output
        outcome = self._parse_agent_outcome(stage_id, result.output, workspace)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_orchestrator_stage_verify.py -v`
Expected: PASS, 2 tests.

Also run the existing orchestrator tests to make sure nothing regressed:

Run: `pytest tests/unit/test_orchestrator*.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_orchestrator_stage_verify.py
git commit -m "feat(orchestrator): verify stage side-effects, transition to BLOCKED on fail"
```

---

## Task 9: Sandbox tool — `validate_git_identity`

**Files:**
- Modify: `orchestrator/tool_sandbox.py`
- Modify: `integrations/config/config_tools.py`
- Create: tests in `tests/unit/test_tool_sandbox_config.py` (append)

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_tool_sandbox_config.py`:

```python
class TestValidateGitIdentitySandbox:
    @pytest.mark.asyncio
    async def test_git_identity_set_via_sandbox(self, workspace, tmp_path):
        import subprocess
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)

        from orchestrator.tool_sandbox import ToolSandbox
        sandbox = ToolSandbox(str(workspace), ["validate_git_identity"])
        from tests.helpers import run
        result = run(sandbox.execute_tool(
            "validate_git_identity", {"workspace_root": str(repo)}
        ))
        data = json.loads(result)
        assert data["ok"] is True
        assert data["user_email"] == "t@t"

    @pytest.mark.asyncio
    async def test_git_identity_missing_via_sandbox(self, workspace, tmp_path, monkeypatch):
        import subprocess
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        monkeypatch.setenv("HOME", str(tmp_path / "empty"))
        (tmp_path / "empty").mkdir()

        from orchestrator.tool_sandbox import ToolSandbox
        sandbox = ToolSandbox(str(workspace), ["validate_git_identity"])
        from tests.helpers import run
        result = run(sandbox.execute_tool(
            "validate_git_identity", {"workspace_root": str(repo)}
        ))
        data = json.loads(result)
        assert data["ok"] is False
        assert "fix_hint" in data
```

Also update the top-of-module test that lists expected tools:

```python
def test_all_config_tools_registered():
    # Add "validate_git_identity" to the existing assertion
    expected = {
        "validate_jira", "validate_github", "validate_gitlab", "validate_jenkins",
        "validate_git_identity",
    }
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_tool_sandbox_config.py::TestValidateGitIdentitySandbox -v`
Expected: FAIL with `ToolError: Unknown tool 'validate_git_identity'`

- [ ] **Step 3: Register the tool in `orchestrator/tool_sandbox.py`**

Near the existing validator tools list (line ~24), add `"validate_git_identity"`:

```python
    # Config management tools (project-setup-agent)
    "validate_jira",
    "validate_github",
    "validate_gitlab",
    "validate_jenkins",
    "validate_git_identity",
```

Add the tool definition schema next to the other `validate_*` schemas:

```python
    "validate_git_identity": {
        "description": "Check that git user.name and user.email are set for a workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workspace_root": {"type": "string"},
            },
            "required": ["workspace_root"],
        },
    },
```

Add the dispatcher method next to `_tool_validate_jira`:

```python
    async def _tool_validate_git_identity(self, params: dict[str, Any]) -> str:
        workspace_root = params.get("workspace_root", "")
        if not workspace_root:
            raise ToolError("validate_git_identity requires 'workspace_root'")
        from pathlib import Path
        from health.validators import check_git_identity
        result = check_git_identity(Path(workspace_root))
        return json.dumps({
            "ok": result.ok,
            "user_name": "",  # filled in on success below
            "user_email": "",
            "reason": result.reason,
            "fix_hint": result.fix_hint,
        })
```

Wait — we need to return the actual user name/email values on success. Update `check_git_identity` to capture them, or have the sandbox tool call `_git_config` directly. Simplest: add a public helper in `health.validators`:

Add to `health/validators.py`:

```python
def read_git_identity(workspace_root: Path) -> tuple[str, str]:
    """Return (user_name, user_email), empty strings if unset or unreadable."""
    name_ok, name_val = _git_config(workspace_root, "user.name")
    email_ok, email_val = _git_config(workspace_root, "user.email")
    return (name_val if name_ok else "", email_val if email_ok else "")
```

Then in `_tool_validate_git_identity`:

```python
    async def _tool_validate_git_identity(self, params: dict[str, Any]) -> str:
        workspace_root = params.get("workspace_root", "")
        if not workspace_root:
            raise ToolError("validate_git_identity requires 'workspace_root'")
        from pathlib import Path
        from health.validators import check_git_identity, read_git_identity
        path = Path(workspace_root)
        result = check_git_identity(path)
        user_name, user_email = read_git_identity(path) if result.ok else ("", "")
        return json.dumps({
            "ok": result.ok,
            "user_name": user_name,
            "user_email": user_email,
            "reason": result.reason,
            "fix_hint": result.fix_hint,
        })
```

Register the dispatch in the existing `execute_tool` switch:

```python
        elif tool_name == "validate_git_identity":
            return await self._tool_validate_git_identity(params)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_tool_sandbox_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/tool_sandbox.py health/validators.py tests/unit/test_tool_sandbox_config.py
git commit -m "feat(sandbox): add validate_git_identity tool"
```

---

## Task 10: Dashboard API — `GET /api/projects/health`

**Files:**
- Modify: `dashboard/web.py`
- Create: `tests/unit/test_dashboard_health_api.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/test_dashboard_health_api.py
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from dashboard.event_store import EventStore
from dashboard.events import EventBus
from dashboard.web import create_app
from health.runner import ProjectHealth
from health.validators import ValidatorResult
from datetime import datetime, timezone


@pytest.fixture
async def store(tmp_path):
    s = EventStore(str(tmp_path / "test.db"))
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def bus():
    return EventBus()


def _fake_projects():
    jira = SimpleNamespace(url="https://x", email="a@b", token="t", project_key="ACME")
    github = SimpleNamespace(token="gh", owner="acme", repo="mb")
    gitlab = SimpleNamespace(token="", url="", project_id="")
    vcs = SimpleNamespace(provider="github", github=github, gitlab=gitlab)
    repo_cfg = SimpleNamespace(vcs=vcs)
    return {"acme": SimpleNamespace(config=SimpleNamespace(jira=jira), repos={"acme-app": repo_cfg})}


class TestProjectsHealthEndpoint:
    def test_returns_all_green(self, bus, store):
        app = create_app(bus, store, projects=_fake_projects())

        fake_result = ProjectHealth(
            project_id="acme",
            checks=[
                ValidatorResult(True, "jira", "ACME", "", ""),
                ValidatorResult(True, "github", "acme/mb", "", ""),
            ],
            checked_at=datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc),
        )
        with patch("dashboard.web.check_all", new=AsyncMock(return_value=[fake_result])):
            client = TestClient(app)
            resp = client.get("/api/projects/health")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["projects"]) == 1
        p = data["projects"][0]
        assert p["project_id"] == "acme"
        assert p["status"] == "green"
        assert len(p["checks"]) == 2

    def test_returns_red_when_jira_fails(self, bus, store):
        app = create_app(bus, store, projects=_fake_projects())

        fake_result = ProjectHealth(
            project_id="acme",
            checks=[
                ValidatorResult(False, "jira", "ACME", "HTTP 401", "check token"),
                ValidatorResult(True, "github", "acme/mb", "", ""),
            ],
            checked_at=datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc),
        )
        with patch("dashboard.web.check_all", new=AsyncMock(return_value=[fake_result])):
            client = TestClient(app)
            resp = client.get("/api/projects/health")

        data = resp.json()
        assert data["projects"][0]["status"] == "red"
        assert data["projects"][0]["checks"][0]["reason"] == "HTTP 401"

    def test_refresh_param_forces_cache_bypass(self, bus, store):
        app = create_app(bus, store, projects=_fake_projects())
        mock = AsyncMock(return_value=[])
        with patch("dashboard.web.check_all", new=mock):
            client = TestClient(app)
            client.get("/api/projects/health?refresh=1")
        mock.assert_called_once()
        assert mock.call_args.kwargs.get("force") is True

    def test_empty_when_no_projects(self, bus, store):
        app = create_app(bus, store, projects={})
        client = TestClient(app)
        resp = client.get("/api/projects/health")
        assert resp.status_code == 200
        assert resp.json()["projects"] == []

    def test_missing_projects_returns_empty(self, bus, store):
        """No projects= wired in (dashboard without config)."""
        app = create_app(bus, store)
        client = TestClient(app)
        resp = client.get("/api/projects/health")
        assert resp.status_code == 200
        assert resp.json()["projects"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_dashboard_health_api.py -v`
Expected: FAIL with 404 (route not registered).

- [ ] **Step 3: Add the route to `dashboard/web.py`**

At the top of `dashboard/web.py`, add:

```python
from health.runner import check_all as health_check_all
from health.runner import ProjectHealth
```

Inside `create_app`, register the new route. Add a handler function near the other `/api/*` handlers:

```python
    async def projects_health(request):
        from starlette.responses import JSONResponse
        force = request.query_params.get("refresh") == "1"
        if not projects:
            return JSONResponse({"projects": []})
        results = await check_all(projects, force=force)
        return JSONResponse({
            "projects": [
                {
                    "project_id": r.project_id,
                    "status": r.status,
                    "checks": [
                        {
                            "name": c.name, "target": c.target,
                            "ok": c.ok, "reason": c.reason, "fix_hint": c.fix_hint,
                        }
                        for c in r.checks
                    ],
                    "checked_at": r.checked_at.isoformat(),
                }
                for r in results
            ]
        })
```

Register in the `routes` list:

```python
        Route("/api/projects/health", projects_health),
```

Note: the test patches `dashboard.web.check_all`, so the import must be `from health.runner import check_all` (not aliased). Adjust the import at the top of `dashboard/web.py`:

```python
from health.runner import check_all
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_dashboard_health_api.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add dashboard/web.py tests/unit/test_dashboard_health_api.py
git commit -m "feat(dashboard): add /api/projects/health endpoint"
```

---

## Task 11: Warm health cache at daemon startup

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Modify `main.py` to warm the cache**

In `_run_all` in [main.py](../../../main.py), right after the dashboard server is started (around line 352), add:

```python
        # Warm the health-check cache so the first dashboard load is instant.
        if projects:
            try:
                from health.runner import check_all as _warm_health
                await _warm_health(projects)
                print("  Health check: warmed cache")
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "Failed to warm health cache: %s", e,
                )
```

- [ ] **Step 2: Verify the daemon still starts cleanly**

Run: `pytest tests/unit/test_dashboard_health_api.py tests/unit/test_health_runner.py -v`
Expected: all pass (no regressions).

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat(daemon): warm health cache at startup"
```

---

## Task 12: Dashboard frontend — health strip UI

**Files:**
- Modify: `dashboard/static/js/api.js`
- Modify: `dashboard/static/js/board.js`
- Modify: `dashboard/static/style.css`
- Create: `tests/e2e/test_dashboard_health.py`

- [ ] **Step 1: Write failing e2e test**

```python
# tests/e2e/test_dashboard_health.py
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.e2e.conftest import goto_and_wait_for_board, make_fake_projects


def _broken_git_projects():
    """Like make_fake_projects but with an intentionally broken git identity."""
    proj = make_fake_projects()
    # We'll override with a stub in the fixture so no real network happens.
    return proj


class TestDashboardHealthStrip:
    def test_all_green_shows_collapsed_pill(self, page, dashboard_server_custom, monkeypatch):
        from health.runner import ProjectHealth
        from health.validators import ValidatorResult
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock

        ctx = dashboard_server_custom(projects=make_fake_projects())

        async def fake_check_all(projects, force=False):
            return [ProjectHealth(
                project_id="acme",
                checks=[
                    ValidatorResult(True, "jira", "ACME", "", ""),
                    ValidatorResult(True, "github", "acme/acme-app", "", ""),
                ],
                checked_at=datetime.now(timezone.utc),
            )]

        import dashboard.web
        monkeypatch.setattr(dashboard.web, "check_all", fake_check_all)

        goto_and_wait_for_board(page, ctx.url)
        pill = page.locator(".health-strip.green")
        assert pill.is_visible()
        assert "healthy" in pill.inner_text().lower()

    def test_red_shows_expanded_with_fix_hint(self, page, dashboard_server_custom, monkeypatch):
        from health.runner import ProjectHealth
        from health.validators import ValidatorResult
        from datetime import datetime, timezone

        ctx = dashboard_server_custom(projects=make_fake_projects())

        async def fake_check_all(projects, force=False):
            return [ProjectHealth(
                project_id="acme",
                checks=[
                    ValidatorResult(False, "jira", "ACME", "HTTP 401", "Check Jira token"),
                ],
                checked_at=datetime.now(timezone.utc),
            )]

        import dashboard.web
        monkeypatch.setattr(dashboard.web, "check_all", fake_check_all)

        goto_and_wait_for_board(page, ctx.url)
        strip = page.locator(".health-strip.red")
        assert strip.is_visible()
        assert "HTTP 401" in strip.inner_text()
        assert "Check Jira token" in strip.inner_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/e2e/test_dashboard_health.py -v`
Expected: FAIL — `.health-strip` element does not exist.

- [ ] **Step 3: Add `loadHealth()` to `dashboard/static/js/api.js`**

Append to `api.js`:

```javascript
export async function loadHealth(force = false) {
  const qs = force ? '?refresh=1' : '';
  const resp = await fetch(`/api/projects/health${qs}`);
  if (!resp.ok) throw new Error(`health fetch failed: ${resp.status}`);
  return resp.json();
}
```

- [ ] **Step 4: Render the health strip in `dashboard/static/js/board.js`**

Find the `renderBoard` function (or equivalent top-level render). At the top of the content render, before the project grid, call a new `renderHealthStrip()` helper. Add:

```javascript
import { loadHealth } from './api.js';
import { esc } from './helpers.js';

async function renderHealthStrip(container) {
  let data;
  try {
    data = await loadHealth();
  } catch (e) {
    container.innerHTML = '';
    return;
  }

  const projects = data.projects || [];
  if (projects.length === 0) {
    container.innerHTML = '';
    return;
  }

  const unhealthy = projects.filter(p => p.status !== 'green');
  if (unhealthy.length === 0) {
    container.innerHTML = `
      <div class="health-strip green">
        <span class="health-dot green"></span>
        <span>All projects healthy</span>
        <button class="health-refresh" id="health-refresh">&#x21bb;</button>
      </div>`;
    bindHealthRefresh(container);
    return;
  }

  const rowsHtml = unhealthy.map(p => {
    const badCHecks = p.checks.filter(c => !c.ok);
    const checksHtml = badCHecks.map(c => `
      <div class="health-check">
        <span class="health-check-name">${esc(c.name)}</span>
        <span class="health-check-target">${esc(c.target)}</span>
        <span class="health-check-reason">${esc(c.reason)}</span>
        ${c.fix_hint ? `<code class="health-check-fix">${esc(c.fix_hint)}</code>` : ''}
      </div>`).join('');
    return `
      <div class="health-row">
        <span class="health-dot ${esc(p.status)}"></span>
        <span class="health-project">${esc(p.project_id)}</span>
        <div class="health-checks">${checksHtml}</div>
      </div>`;
  }).join('');

  const topStatus = unhealthy.some(p => p.status === 'red') ? 'red' : 'yellow';
  container.innerHTML = `
    <div class="health-strip ${topStatus}">
      <div class="health-strip-header">
        <span class="health-dot ${topStatus}"></span>
        <span>${unhealthy.length} project(s) need attention</span>
        <button class="health-refresh" id="health-refresh">&#x21bb;</button>
      </div>
      ${rowsHtml}
    </div>`;
  bindHealthRefresh(container);
}

function bindHealthRefresh(container) {
  const btn = container.querySelector('#health-refresh');
  if (btn) {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        await loadHealth(true);
      } finally {
        btn.disabled = false;
        renderHealthStrip(container);
      }
    });
  }
}
```

In the existing `renderBoard` (or wherever the board view is rendered), insert a health-strip container before the project grid:

```javascript
// Inside the main render function, when building the HTML for the board:
content.innerHTML = `
  <div id="health-strip-container"></div>
  <div id="board-grid">...</div>`;
// Then after setting innerHTML:
await renderHealthStrip(document.getElementById('health-strip-container'));
```

Note: the exact insertion point depends on the current `board.js` structure. If the board's render function is async, `await` it; if not, use `.then()`. The health strip must render above the project grid.

- [ ] **Step 5: Add CSS to `dashboard/static/style.css`**

Append:

```css
/* Project health strip */
.health-strip {
  border-radius: 8px;
  padding: 10px 14px;
  margin-bottom: 14px;
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.health-strip.green {
  background: #0d1f17;
  border: 1px solid #238636;
  color: #56d364;
  flex-direction: row;
  align-items: center;
  gap: 8px;
}
.health-strip.yellow {
  background: #2d2418;
  border: 1px solid #9e6a03;
}
.health-strip.red {
  background: #2d1414;
  border: 1px solid #da3633;
}
.health-strip-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  color: #f0f6fc;
}
.health-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
}
.health-dot.green { background: #238636; }
.health-dot.yellow { background: #e3b341; }
.health-dot.red { background: #da3633; }
.health-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 8px 0;
  border-top: 1px solid #30363d;
}
.health-project {
  font-weight: 600;
  color: #f0f6fc;
  min-width: 80px;
}
.health-checks {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.health-check {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.health-check-name {
  color: #f0f6fc;
  font-weight: 600;
}
.health-check-target {
  color: #8b949e;
  font-size: 11px;
}
.health-check-reason {
  color: #f85149;
}
.health-check-fix {
  display: block;
  width: 100%;
  background: #0d1117;
  border: 1px solid #30363d;
  border-radius: 4px;
  padding: 4px 6px;
  color: #79c0ff;
  font-size: 11px;
  user-select: all;
}
.health-refresh {
  margin-left: auto;
  background: none;
  border: 1px solid #30363d;
  color: #8b949e;
  border-radius: 4px;
  padding: 2px 8px;
  cursor: pointer;
  font-size: 14px;
}
.health-refresh:hover { border-color: #58a6ff; color: #58a6ff; }
```

- [ ] **Step 6: Run the e2e test**

Run: `pytest tests/e2e/test_dashboard_health.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 7: Run full e2e + unit suite to catch regressions**

Run: `pytest tests/ -v`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add dashboard/static/js/api.js dashboard/static/js/board.js dashboard/static/style.css tests/e2e/test_dashboard_health.py
git commit -m "feat(dashboard): render health strip on board view"
```

---

## Task 13: project-setup-agent — enforce validator checklist

**Files:**
- Modify: `agents/project-setup-agent.md`

- [ ] **Step 1: Read the current file**

Read [agents/project-setup-agent.md](../../../agents/project-setup-agent.md) so you match its existing structure and tone.

- [ ] **Step 2: Add a "Health checks" section to the agent's checklist**

Insert before the final output section:

```markdown
## Mandatory health checks

Before declaring setup complete, you MUST run all applicable health
checks and include the results in your output report. Do not proceed
past any failing check.

1. Call `validate_jira` with the project's Jira URL, email, token, and project key.
2. If `vcs.provider == "github"`, call `validate_github` with the repo's token, owner, and repo name.
3. If `vcs.provider == "gitlab"`, call `validate_gitlab` with the repo's token, URL, and project id.
4. Call `validate_git_identity` with the planned workspace root for each repo.
5. If any check returns `ok: false`, print its `reason` and `fix_hint`
   exactly as returned, then stop. Ask the operator to fix the issue
   and re-run setup. Do not declare setup successful.

## Output contract

Your final report MUST include a "Health checks" section listing each
validator run, its result, and (on failure) the fix hint. At the very
end of the report, include exactly one of these lines:

- `status: pass` — all health checks passed.
- `status: fail` — one or more checks failed.

The orchestrator parses this line to determine stage outcome.
```

- [ ] **Step 3: Commit**

```bash
git add agents/project-setup-agent.md
git commit -m "feat(agents): project-setup-agent must run all health checks"
```

---

## Task 14: Setup manual — Prerequisites section

**Files:**
- Modify: `docs/setup-guide.md`

- [ ] **Step 1: Read the existing setup guide**

Read [docs/setup-guide.md](../../../docs/setup-guide.md) to match its structure and headings.

- [ ] **Step 2: Add a Prerequisites section near the top**

Add immediately after the intro / before the first install step:

```markdown
## Prerequisites

Before running the daemon, configure these on the host machine. The
daemon verifies them at startup and the dashboard shows a health strip
if anything is broken.

### Git identity

The dev-agent commits code on behalf of the operator. Git needs a
name and email or `git commit` will refuse to run.

```bash
git config --global user.name "Your Name"
git config --global user.email "you@company.com"
```

Per-workspace overrides also work (`git config user.email ...` inside
the workspace dir). The daemon reads the effective value the same way
`git commit` does.

### Git remote auth

If `git push` works from this shell against your configured repo, the
daemon's push step will too. See [GitHub integration](features/github-integration.md)
or [GitLab integration](features/gitlab-integration.md) for SSH key and
HTTPS credential-helper setup.

### Jira token

Already covered in the [project configuration](#project-configuration)
section below.

### Verifying setup before first run

Run the health check from the CLI without starting the daemon:

```bash
python -m health.runner --config config-live
```

This prints each project's validator results — use it in CI or for
smoke-testing a new environment.
```

- [ ] **Step 3: Commit**

```bash
git add docs/setup-guide.md
git commit -m "docs: add Prerequisites section to setup guide"
```

---

## Task 15: `health.runner` CLI entry point

**Files:**
- Modify: `health/runner.py`

- [ ] **Step 1: Add a `__main__` block to `health/runner.py`**

Append:

```python
def _main() -> int:
    import argparse
    import asyncio
    import sys
    from config.config_loader import load_config, ConfigError

    parser = argparse.ArgumentParser(
        prog="python -m health.runner",
        description="Run project health checks without starting the daemon.",
    )
    parser.add_argument("--config", required=True, help="Path to config directory")
    args = parser.parse_args()

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
```

- [ ] **Step 2: Manual smoke test (operator will run this; no automated test — it's a thin CLI glue layer over already-tested code)**

- [ ] **Step 3: Commit**

```bash
git add health/runner.py
git commit -m "feat(health): add CLI entry point for standalone health checks"
```

---

## Task 16: Feature docs — `docs/features/dashboard.md` + `docs/features/index.md`

**Files:**
- Modify: `docs/features/dashboard.md`
- Modify: `docs/features/index.md`

- [ ] **Step 1: Update `docs/features/dashboard.md`**

Add to the Requirements list:

```markdown
- FR16: Per-project health panel (Jira, vcs, git identity, git remote) with fix hints
```

Add to the Acceptance Criteria list:

```markdown
- [x] Dashboard shows a health strip on the board; red/yellow expands with fix hints; refresh button works
- [x] Stage verifier transitions workspace to BLOCKED when a stage produces no mechanical effect
```

Add to the Change Log:

```markdown
| 2026-04-15 | Project health strip + stage verifier; prevents silent drift to AWAITING_APPROVAL when agent can't commit |
```

- [ ] **Step 2: Update `docs/features/index.md`**

Add a new row for the health feature:

```markdown
| N | [Project Health + Stage Verification](project-health.md) | Implemented | Per-project validators + mechanical post-stage checks → BLOCKED on failure |
```

Note: We are not creating `project-health.md` as a separate feature doc — the functionality is part of the dashboard and orchestrator. The index entry links to the dashboard doc instead:

```markdown
| N | [Project Health + Stage Verification](dashboard.md#fr16) | Implemented | Per-project validators + mechanical post-stage checks |
```

Use whichever row format matches the existing table.

- [ ] **Step 3: Commit**

```bash
git add docs/features/dashboard.md docs/features/index.md
git commit -m "docs: document project health + stage verification in feature index"
```

---

## Task 17: End-to-end regression check

- [ ] **Step 1: Run the full test suite**

Run: `pytest tests/ -v`
Expected: all tests pass.

- [ ] **Step 2: Manual smoke on a live daemon**

Start the daemon (`./run.sh` or equivalent) and:

1. Open the dashboard. Confirm the health strip appears at the top of the board.
2. Intentionally break git identity in one workspace (`git config user.email ""` inside it).
3. Refresh the health strip — the affected project should go yellow with the fix hint visible.
4. Fix the identity and click refresh — strip returns to green.

- [ ] **Step 3: Final commit (only if anything needs tweaking after manual smoke)**

If everything is clean, no commit needed. Otherwise make a follow-up fix and commit normally.

---

## Self-Review Checklist

Before handing off, the plan author must verify:

- [ ] Every spec section has at least one task (validators library → Tasks 1–5; stage verifier → Tasks 6–8; dashboard → Tasks 10, 12; sandbox tool → Task 9; project-setup-agent → Task 13; setup manual → Tasks 14, 15; feature docs → Task 16).
- [ ] No placeholders, TODOs, or "similar to Task N" references.
- [ ] Method/class names are consistent across tasks (`ValidatorResult`, `ProjectHealth`, `HealthRunner`, `check_all`, `check_project`, `VerifyResult`, `verify`, `capture_stage_start`).
- [ ] Every code step shows the actual code.
- [ ] Every test step shows actual test code and the expected pass/fail.
- [ ] Every task ends with a commit.
- [ ] The dev-stage reproducer for ACME-14595 is in Task 8 and asserts the ticket lands in BLOCKED.
