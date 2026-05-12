# GitLab VCS Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `GitLabAdapter(VCSInterface)` so projects created with `vcs.provider: gitlab` run the full pipeline end-to-end.

**Architecture:** Single-file adapter at `integrations/gitlab/gitlab_adapter.py` mirroring `GitHubAdapter` shape. Direct GitLab REST v4 via httpx. Private `_discussion_cache` maps `note_id → discussion_id` per MR so the VCSInterface comment-ID contract stays an int. `main.py` learns one `elif provider == "gitlab"` branch through a new `_build_vcs_adapter` helper extracted to remove duplication.

**Tech Stack:** Python 3.10+, httpx (async REST), git CLI subprocess, pytest + pytest-asyncio + unittest.mock (no respx in unit tests — adapter `_request` is patched directly, matching existing GitHub adapter test pattern).

**Reference spec:** [docs/superpowers/specs/2026-05-12-gitlab-vcs-adapter-design.md](../specs/2026-05-12-gitlab-vcs-adapter-design.md)

**Pre-flight check:** Run `pytest -x` before starting. The plan assumes a green baseline. If anything fails, fix or note it before Task 1.

---

## Task 1: Scaffold `integrations/gitlab/` package and adapter skeleton

**Files:**
- Create: `integrations/gitlab/__init__.py`
- Create: `integrations/gitlab/gitlab_adapter.py`
- Create: `tests/unit/test_gitlab_adapter_init.py`

- [ ] **Step 1: Create the empty package marker**

Create `integrations/gitlab/__init__.py` with no content.

- [ ] **Step 2: Write the failing init test**

Create `tests/unit/test_gitlab_adapter_init.py`:

```python
"""Tests for GitLabAdapter.__init__ — field storage and httpx client setup."""

from __future__ import annotations

import pytest

from integrations.gitlab.gitlab_adapter import GitLabAdapter


def test_init_stores_token_project_and_url():
    adapter = GitLabAdapter(
        token="glpat_xyz",
        project_id="group/proj",
        url="https://gitlab.example.com",
    )
    assert adapter._token == "glpat_xyz"
    assert adapter._project_id == "group/proj"
    assert adapter._url == "https://gitlab.example.com"


def test_init_defaults_url_to_gitlab_com():
    adapter = GitLabAdapter(token="t", project_id="123")
    assert adapter._url == "https://gitlab.com"


def test_init_strips_trailing_slash_from_url():
    adapter = GitLabAdapter(token="t", project_id="123", url="https://gitlab.com/")
    assert adapter._url == "https://gitlab.com"


def test_init_urlencodes_project_id_for_paths():
    """Adapter must URL-encode project_id so namespaced paths like
    'group/proj' work in {url}/api/v4/projects/{id} routes."""
    adapter = GitLabAdapter(token="t", project_id="group/sub/proj")
    assert adapter._project_path == "group%2Fsub%2Fproj"


def test_init_numeric_project_id_passes_through():
    adapter = GitLabAdapter(token="t", project_id="12345")
    # quote() leaves digits alone
    assert adapter._project_path == "12345"
```

- [ ] **Step 3: Run tests; expect failures (module not yet defined)**

Run: `pytest tests/unit/test_gitlab_adapter_init.py -v`
Expected: ImportError or `GitLabAdapter` undefined.

- [ ] **Step 4: Implement the adapter skeleton**

Create `integrations/gitlab/gitlab_adapter.py`:

```python
"""GitLab adapter implementing VCSInterface (REST API v4 + git CLI)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx

from integrations.base.vcs import PRComment, PRStatus, VCSInterface

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF = [1, 2, 4]
TIMEOUT = 30
SUBPROCESS_TIMEOUT = 300


class GitLabAdapter(VCSInterface):
    """GitLab REST API v4 + git CLI adapter."""

    def __init__(
        self, token: str, project_id: str, url: str = "https://gitlab.com",
    ) -> None:
        self._token = token
        self._project_id = project_id
        self._url = (url or "https://gitlab.com").rstrip("/")
        self._project_path = quote(str(project_id), safe="")
        self._client = httpx.AsyncClient(
            base_url=f"{self._url}/api/v4",
            headers={"Private-Token": token, "Accept": "application/json"},
            timeout=TIMEOUT,
        )
        # note_id -> discussion_id, keyed by mr_iid; populated lazily by
        # get_pr_comments and used by reply_to_comment / resolve_comment.
        self._discussion_cache: dict[int, dict[int, str]] = {}

    # --- VCSInterface methods (filled in by later tasks) ----------------

    async def clone_repo(self, url: str, dest: str, depth: int = 0) -> None:
        raise NotImplementedError

    async def create_branch(self, repo_dir: str, branch_name: str) -> None:
        raise NotImplementedError

    async def push(
        self, repo_dir: str, branch_name: str,
        force: bool = False, skip_hooks: bool = False,
    ) -> None:
        raise NotImplementedError

    async def open_pr(
        self, title: str, body: str, head_branch: str, base_branch: str,
    ) -> tuple[int, str]:
        raise NotImplementedError

    async def find_pr_by_branch(self, branch: str) -> tuple[int, str] | None:
        raise NotImplementedError

    async def get_pr_comments(self, pr_number: int) -> list[PRComment]:
        raise NotImplementedError

    async def reply_to_comment(self, pr_number: int, comment_id: int, body: str) -> None:
        raise NotImplementedError

    async def resolve_comment(self, pr_number: int, comment_id: int) -> None:
        raise NotImplementedError

    async def check_pr_status(self, pr_number: int) -> PRStatus:
        raise NotImplementedError

    async def close_pr(self, pr_number: int) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 5: Run tests; expect pass**

Run: `pytest tests/unit/test_gitlab_adapter_init.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add integrations/gitlab/__init__.py integrations/gitlab/gitlab_adapter.py tests/unit/test_gitlab_adapter_init.py
git commit -m "feat(gitlab): scaffold GitLabAdapter package with constructor and stubs"
```

---

## Task 2: HTTP plumbing — `_request` with retries and body capture

**Files:**
- Modify: `integrations/gitlab/gitlab_adapter.py` (add `_request` method)
- Create: `tests/unit/test_gitlab_adapter_request.py`

- [ ] **Step 1: Write the failing request tests**

Create `tests/unit/test_gitlab_adapter_request.py`:

```python
"""Tests for GitLabAdapter._request — retries, auth fail, body capture."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from integrations.gitlab.gitlab_adapter import GitLabAdapter


def _make_adapter() -> GitLabAdapter:
    adapter = GitLabAdapter.__new__(GitLabAdapter)
    adapter._token = "t"
    adapter._project_id = "group/proj"
    adapter._url = "https://gitlab.com"
    adapter._project_path = "group%2Fproj"
    adapter._client = MagicMock()
    adapter._discussion_cache = {}
    return adapter


def _http_error(status: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request(
        "POST",
        "https://gitlab.com/api/v4/projects/group%2Fproj/merge_requests",
    )
    response = httpx.Response(
        status_code=status, content=body.encode("utf-8"), request=request,
    )
    return httpx.HTTPStatusError(
        f"Client error '{status}' for url '{request.url}'",
        request=request, response=response,
    )


@pytest.mark.asyncio
async def test_request_400_includes_response_body_in_error():
    adapter = _make_adapter()
    body = '{"message":"Branch \\"feature/x\\" already exists"}'
    err = _http_error(400, body)
    adapter._client.request = AsyncMock(side_effect=err)

    with patch("integrations.gitlab.gitlab_adapter.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(RuntimeError) as exc_info:
            await adapter._request("POST", "/projects/group%2Fproj/merge_requests", json={})

    msg = str(exc_info.value)
    assert "400" in msg
    assert "already exists" in msg


@pytest.mark.asyncio
async def test_request_401_raises_httpstatuserror_without_wrap():
    adapter = _make_adapter()
    err = _http_error(401, '{"message":"401 Unauthorized"}')
    response = err.response
    adapter._client.request = AsyncMock(return_value=response)

    with pytest.raises(httpx.HTTPStatusError):
        await adapter._request("GET", "/user")


@pytest.mark.asyncio
async def test_request_timeout_after_retries_raises_timeout():
    adapter = _make_adapter()
    timeout = httpx.TimeoutException("network too slow")
    adapter._client.request = AsyncMock(side_effect=timeout)

    with patch("integrations.gitlab.gitlab_adapter.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(httpx.TimeoutException):
            await adapter._request("GET", "/some/path")


@pytest.mark.asyncio
async def test_request_204_returns_empty_dict():
    adapter = _make_adapter()
    response = MagicMock()
    response.status_code = 204
    response.raise_for_status = MagicMock()
    adapter._client.request = AsyncMock(return_value=response)

    result = await adapter._request("DELETE", "/some/path")
    assert result == {}
```

- [ ] **Step 2: Run tests; expect failures (`_request` undefined)**

Run: `pytest tests/unit/test_gitlab_adapter_request.py -v`
Expected: AttributeError on `_request`.

- [ ] **Step 3: Add `_request` to the adapter**

Edit `integrations/gitlab/gitlab_adapter.py` — insert after `__init__`:

```python
    async def _request(self, method: str, path: str, **kwargs: Any) -> dict | list:
        """HTTP request with retries; surfaces response body on final failure."""
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await self._client.request(method, path, **kwargs)
                if response.status_code in (401, 403):
                    raise httpx.HTTPStatusError(
                        f"Authentication failed: {response.status_code}",
                        request=response.request, response=response,
                    )
                response.raise_for_status()
                if response.status_code == 204:
                    return {}
                return response.json()
            except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
                last_error = e
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
                    raise
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_BACKOFF[attempt])
                    logger.warning(
                        "GitLab request retry %d/%d for %s %s: %s",
                        attempt + 1, MAX_RETRIES, method, path, e,
                    )
        if isinstance(last_error, httpx.HTTPStatusError) and last_error.response is not None:
            body = last_error.response.text[:1500]
            logger.error(
                "GitLab %s %s failed after %d retries → %d: %s",
                method, path, MAX_RETRIES, last_error.response.status_code, body[:500],
            )
            raise RuntimeError(
                f"GitLab {method} {path} → {last_error.response.status_code}: {body[:500]}"
            ) from last_error
        raise last_error  # type: ignore[misc]
```

- [ ] **Step 4: Run tests; expect pass**

Run: `pytest tests/unit/test_gitlab_adapter_request.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add integrations/gitlab/gitlab_adapter.py tests/unit/test_gitlab_adapter_request.py
git commit -m "feat(gitlab): _request with retries, auth no-retry, body-on-fail"
```

---

## Task 3: Git subprocess plumbing — `_run_git`, `clone_repo`, `create_branch`

**Files:**
- Modify: `integrations/gitlab/gitlab_adapter.py`
- Create: `tests/unit/test_gitlab_adapter_git.py`

- [ ] **Step 1: Write failing git tests for clone + create_branch**

Create `tests/unit/test_gitlab_adapter_git.py`:

```python
"""Tests for GitLabAdapter git subprocess wrappers: clone, branch, push."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from integrations.gitlab.gitlab_adapter import GitLabAdapter


def _make_adapter() -> GitLabAdapter:
    adapter = GitLabAdapter.__new__(GitLabAdapter)
    adapter._token = "glpat_TOKEN"
    adapter._project_id = "group/proj"
    adapter._url = "https://gitlab.com"
    adapter._project_path = "group%2Fproj"
    adapter._client = MagicMock()
    adapter._discussion_cache = {}
    return adapter


def _ok_proc():
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"", b""))
    return proc


@pytest.mark.asyncio
async def test_clone_repo_runs_git_clone():
    adapter = _make_adapter()
    with patch(
        "integrations.gitlab.gitlab_adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_ok_proc()),
    ) as spawn:
        await adapter.clone_repo("https://gitlab.com/group/proj.git", "/tmp/r")
    cmd = list(spawn.call_args.args)
    assert cmd[:2] == ["git", "clone"]
    assert cmd[-2:] == ["https://gitlab.com/group/proj.git", "/tmp/r"]


@pytest.mark.asyncio
async def test_clone_repo_with_depth_adds_flag():
    adapter = _make_adapter()
    with patch(
        "integrations.gitlab.gitlab_adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_ok_proc()),
    ) as spawn:
        await adapter.clone_repo("https://gitlab.com/g/p.git", "/tmp/r", depth=1)
    cmd = list(spawn.call_args.args)
    assert "--depth" in cmd
    assert "1" in cmd


@pytest.mark.asyncio
async def test_create_branch_runs_checkout_b():
    adapter = _make_adapter()
    with patch(
        "integrations.gitlab.gitlab_adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_ok_proc()),
    ) as spawn:
        await adapter.create_branch("/tmp/r", "feature/X")
    cmd = list(spawn.call_args.args)
    assert cmd == ["git", "-C", "/tmp/r", "checkout", "-b", "feature/X"]
```

- [ ] **Step 2: Run tests; expect failures (NotImplementedError)**

Run: `pytest tests/unit/test_gitlab_adapter_git.py -v`
Expected: NotImplementedError for both methods.

- [ ] **Step 3: Implement `_run_git`, `clone_repo`, `create_branch`**

In `integrations/gitlab/gitlab_adapter.py`, replace the `clone_repo` and `create_branch` stubs and add `_run_git` as a staticmethod just before them:

```python
    @staticmethod
    async def _run_git(repo_dir: str, *args: str) -> tuple[str, str]:
        """Run a git command inside repo_dir. Returns (stdout, stderr)."""
        cmd = ["git", "-C", repo_dir] + list(args)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=SUBPROCESS_TIMEOUT,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"Git command timed out after {SUBPROCESS_TIMEOUT}s: {' '.join(cmd)}"
            )
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(f"Git command failed: {' '.join(cmd)}\n{stderr.strip()}")
        return stdout, stderr

    async def clone_repo(self, url: str, dest: str, depth: int = 0) -> None:
        cmd = ["git", "clone"]
        if depth > 0:
            cmd.extend(["--depth", str(depth)])
        cmd.extend([url, dest])

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=SUBPROCESS_TIMEOUT)
        if proc.returncode != 0:
            raise RuntimeError(f"Git clone failed: {stderr.decode().strip()}")

    async def create_branch(self, repo_dir: str, branch_name: str) -> None:
        await self._run_git(repo_dir, "checkout", "-b", branch_name)
        logger.info("Created branch: %s", branch_name)
```

- [ ] **Step 4: Run tests; expect pass**

Run: `pytest tests/unit/test_gitlab_adapter_git.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add integrations/gitlab/gitlab_adapter.py tests/unit/test_gitlab_adapter_git.py
git commit -m "feat(gitlab): clone_repo and create_branch via async git CLI"
```

---

## Task 4: `push` with origin URL rewrite, `--force`, `--no-verify`

**Files:**
- Modify: `integrations/gitlab/gitlab_adapter.py`
- Modify: `tests/unit/test_gitlab_adapter_git.py`

- [ ] **Step 1: Add failing push tests**

Append to `tests/unit/test_gitlab_adapter_git.py`:

```python
@pytest.mark.asyncio
async def test_push_default_no_force_no_no_verify():
    adapter = _make_adapter()
    with patch(
        "integrations.gitlab.gitlab_adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_ok_proc()),
    ) as spawn:
        await adapter.push("/tmp/r", "feature/X")
    calls = [list(c.args) for c in spawn.call_args_list]
    assert len(calls) == 2  # set-url then push
    _, push_cmd = calls
    assert push_cmd == ["git", "-C", "/tmp/r", "push", "-u", "origin", "feature/X"]


@pytest.mark.asyncio
async def test_push_with_skip_hooks_adds_no_verify():
    adapter = _make_adapter()
    with patch(
        "integrations.gitlab.gitlab_adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_ok_proc()),
    ) as spawn:
        await adapter.push("/tmp/r", "feature/X", skip_hooks=True)
    push_cmd = list(spawn.call_args_list[-1].args)
    assert "--no-verify" in push_cmd
    assert "--force" not in push_cmd


@pytest.mark.asyncio
async def test_push_with_force_and_skip_hooks_keeps_both():
    adapter = _make_adapter()
    with patch(
        "integrations.gitlab.gitlab_adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_ok_proc()),
    ) as spawn:
        await adapter.push("/tmp/r", "feature/X", force=True, skip_hooks=True)
    push_cmd = list(spawn.call_args_list[-1].args)
    assert "--force" in push_cmd
    assert "--no-verify" in push_cmd


@pytest.mark.asyncio
async def test_push_refreshes_origin_url_with_oauth2_token():
    """Workspaces cloned before a token rotation have a stale token in
    origin. Rewrite first so push uses the adapter's current token, in
    the GitLab oauth2 form."""
    adapter = _make_adapter()
    adapter._token = "glpat_NEW"
    adapter._url = "https://gitlab.example.com"
    adapter._project_id = "group/proj"
    with patch(
        "integrations.gitlab.gitlab_adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_ok_proc()),
    ) as spawn:
        await adapter.push("/tmp/r", "feature/X")
    set_url_cmd = list(spawn.call_args_list[0].args)
    assert set_url_cmd == [
        "git", "-C", "/tmp/r", "remote", "set-url", "origin",
        "https://oauth2:glpat_NEW@gitlab.example.com/group/proj.git",
    ]
```

- [ ] **Step 2: Run tests; expect failures**

Run: `pytest tests/unit/test_gitlab_adapter_git.py -v`
Expected: 4 failing on `push`.

- [ ] **Step 3: Implement `push`**

In `integrations/gitlab/gitlab_adapter.py`, replace the `push` stub:

```python
    async def push(
        self, repo_dir: str, branch_name: str,
        force: bool = False, skip_hooks: bool = False,
    ) -> None:
        """Push branch to origin. Rewrites origin URL with current token
        first so workspaces cloned before a rotation still authenticate.

        Uses GitLab's oauth2 username form for token-in-URL auth:
            https://oauth2:<token>@<host>/<namespace>/<project>.git
        """
        host = self._url.replace("https://", "").replace("http://", "")
        canonical_url = (
            f"https://oauth2:{self._token}@{host}/{self._project_id}.git"
        )
        await self._run_git(repo_dir, "remote", "set-url", "origin", canonical_url)

        args = ["push", "-u", "origin", branch_name]
        if force:
            args.insert(1, "--force")
        if skip_hooks:
            args.insert(1, "--no-verify")
        await self._run_git(repo_dir, *args)
        suffix = ""
        if force:
            suffix += " (force)"
        if skip_hooks:
            suffix += " (no-verify)"
        logger.info("Pushed branch: %s%s", branch_name, suffix)
```

- [ ] **Step 4: Run tests; expect pass**

Run: `pytest tests/unit/test_gitlab_adapter_git.py -v`
Expected: 7 passed (3 prior + 4 new).

- [ ] **Step 5: Commit**

```bash
git add integrations/gitlab/gitlab_adapter.py tests/unit/test_gitlab_adapter_git.py
git commit -m "feat(gitlab): push with oauth2 origin rewrite, force, no-verify"
```

---

## Task 5: `open_pr` and `find_pr_by_branch`

**Files:**
- Modify: `integrations/gitlab/gitlab_adapter.py`
- Create: `tests/unit/test_gitlab_adapter_mr.py`

- [ ] **Step 1: Write failing MR-create / find tests**

Create `tests/unit/test_gitlab_adapter_mr.py`:

```python
"""Tests for GitLabAdapter MR / discussion / pipeline methods."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.base.vcs import PRComment
from integrations.gitlab.gitlab_adapter import GitLabAdapter


def _make_adapter() -> GitLabAdapter:
    adapter = GitLabAdapter.__new__(GitLabAdapter)
    adapter._token = "t"
    adapter._project_id = "group/proj"
    adapter._url = "https://gitlab.com"
    adapter._project_path = "group%2Fproj"
    adapter._client = MagicMock()
    adapter._discussion_cache = {}
    return adapter


@pytest.mark.asyncio
async def test_open_pr_posts_correct_payload_and_returns_iid_and_url():
    adapter = _make_adapter()
    adapter._request = AsyncMock(return_value={
        "iid": 42,
        "web_url": "https://gitlab.com/group/proj/-/merge_requests/42",
    })

    iid, url = await adapter.open_pr(
        title="Add feature X",
        body="Implements ACME-123",
        head_branch="feature/x",
        base_branch="develop",
    )

    assert iid == 42
    assert url == "https://gitlab.com/group/proj/-/merge_requests/42"
    adapter._request.assert_awaited_once()
    method, path = adapter._request.await_args.args[:2]
    kwargs = adapter._request.await_args.kwargs
    assert method == "POST"
    assert path == "/projects/group%2Fproj/merge_requests"
    assert kwargs["json"] == {
        "source_branch": "feature/x",
        "target_branch": "develop",
        "title": "Add feature X",
        "description": "Implements ACME-123",
    }


@pytest.mark.asyncio
async def test_find_pr_by_branch_returns_first_open_match():
    adapter = _make_adapter()
    adapter._request = AsyncMock(return_value=[
        {"iid": 7, "web_url": "https://gitlab.com/group/proj/-/merge_requests/7"},
    ])

    result = await adapter.find_pr_by_branch("feature/x")
    assert result == (7, "https://gitlab.com/group/proj/-/merge_requests/7")
    kwargs = adapter._request.await_args.kwargs
    assert kwargs["params"] == {"source_branch": "feature/x", "state": "opened"}


@pytest.mark.asyncio
async def test_find_pr_by_branch_returns_none_when_empty():
    adapter = _make_adapter()
    adapter._request = AsyncMock(return_value=[])
    assert await adapter.find_pr_by_branch("feature/x") is None


@pytest.mark.asyncio
async def test_find_pr_by_branch_swallows_errors_and_returns_none():
    adapter = _make_adapter()
    adapter._request = AsyncMock(side_effect=RuntimeError("boom"))
    assert await adapter.find_pr_by_branch("feature/x") is None
```

- [ ] **Step 2: Run tests; expect failures**

Run: `pytest tests/unit/test_gitlab_adapter_mr.py -v`
Expected: NotImplementedError on both methods.

- [ ] **Step 3: Implement `open_pr` and `find_pr_by_branch`**

In `integrations/gitlab/gitlab_adapter.py`, replace those two stubs:

```python
    @property
    def _mr_path(self) -> str:
        return f"/projects/{self._project_path}/merge_requests"

    async def open_pr(
        self, title: str, body: str, head_branch: str, base_branch: str,
    ) -> tuple[int, str]:
        """Open a merge request. Returns (iid, web_url)."""
        data = await self._request(
            "POST",
            self._mr_path,
            json={
                "source_branch": head_branch,
                "target_branch": base_branch,
                "title": title,
                "description": body,
            },
        )
        iid = data["iid"]
        web_url = data["web_url"]
        logger.info("Opened MR !%d: %s", iid, web_url)
        return iid, web_url

    async def find_pr_by_branch(self, branch: str) -> tuple[int, str] | None:
        """Find an open MR with the given source_branch."""
        try:
            data = await self._request(
                "GET", self._mr_path,
                params={"source_branch": branch, "state": "opened"},
            )
            if data and isinstance(data, list) and len(data) > 0:
                mr = data[0]
                return mr["iid"], mr["web_url"]
        except Exception as e:
            logger.warning("Failed to find MR for branch %s: %s", branch, e)
        return None
```

- [ ] **Step 4: Run tests; expect pass**

Run: `pytest tests/unit/test_gitlab_adapter_mr.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add integrations/gitlab/gitlab_adapter.py tests/unit/test_gitlab_adapter_mr.py
git commit -m "feat(gitlab): open_pr (MR create) and find_pr_by_branch"
```

---

## Task 6: `get_pr_comments` with discussion-cache population

**Files:**
- Modify: `integrations/gitlab/gitlab_adapter.py`
- Modify: `tests/unit/test_gitlab_adapter_mr.py`

- [ ] **Step 1: Append failing get_pr_comments tests**

Append to `tests/unit/test_gitlab_adapter_mr.py`:

```python
def _diff_note(note_id: int, body: str, path: str = "a.py", line: int = 5):
    return {
        "id": note_id,
        "body": body,
        "author": {"username": "alice"},
        "position": {"new_path": path, "new_line": line},
    }


def _plain_note(note_id: int, body: str):
    return {
        "id": note_id,
        "body": body,
        "author": {"username": "alice"},
        # No "position" => general MR note, must be filtered out
    }


@pytest.mark.asyncio
async def test_get_pr_comments_returns_only_diff_position_notes():
    adapter = _make_adapter()
    discussions_page = [
        {
            "id": "abc123",
            "notes": [_diff_note(101, "fix indentation"), _diff_note(102, "rename var")],
        },
        {
            "id": "def456",
            "notes": [_plain_note(200, "LGTM overall")],
        },
    ]
    # 2 pages: first returns 2 discussions, second returns empty -> stop
    adapter._request = AsyncMock(side_effect=[discussions_page, []])

    comments = await adapter.get_pr_comments(42)

    assert len(comments) == 2
    assert all(isinstance(c, PRComment) for c in comments)
    assert {c.id for c in comments} == {101, 102}
    assert comments[0].path == "a.py"
    assert comments[0].line == 5
    assert comments[0].author == "alice"


@pytest.mark.asyncio
async def test_get_pr_comments_populates_discussion_cache():
    adapter = _make_adapter()
    discussions_page = [
        {"id": "disc-a", "notes": [_diff_note(11, "x")]},
        {"id": "disc-b", "notes": [_diff_note(22, "y"), _diff_note(23, "z")]},
    ]
    adapter._request = AsyncMock(side_effect=[discussions_page, []])

    await adapter.get_pr_comments(42)
    cache = adapter._discussion_cache[42]
    assert cache == {11: "disc-a", 22: "disc-b", 23: "disc-b"}


@pytest.mark.asyncio
async def test_get_pr_comments_paginates_until_short_page():
    """Loop until a page returns fewer than 100 items."""
    adapter = _make_adapter()
    page1 = [{"id": f"d{i}", "notes": [_diff_note(i, "x")]} for i in range(100)]
    page2 = [{"id": "last", "notes": [_diff_note(999, "z")]}]
    adapter._request = AsyncMock(side_effect=[page1, page2])

    comments = await adapter.get_pr_comments(42)
    assert len(comments) == 101
    assert adapter._request.await_count == 2
```

- [ ] **Step 2: Run tests; expect failures**

Run: `pytest tests/unit/test_gitlab_adapter_mr.py -v`
Expected: 3 new failures (NotImplementedError).

- [ ] **Step 3: Implement `get_pr_comments`**

In `integrations/gitlab/gitlab_adapter.py`, replace the `get_pr_comments` stub:

```python
    async def get_pr_comments(self, pr_number: int) -> list[PRComment]:
        """Fetch all diff-position notes across the MR's discussions.

        General MR notes (no `position` field) are skipped — only diff-anchored
        review comments are surfaced, matching the GitHub adapter's behavior
        of pulling from /pulls/:n/comments (not /issues/:n/comments).

        Populates self._discussion_cache[pr_number] so reply_to_comment and
        resolve_comment can look up a note's owning discussion_id without
        another round-trip.
        """
        cache: dict[int, str] = {}
        all_comments: list[PRComment] = []
        page = 1
        while True:
            data = await self._request(
                "GET",
                f"/projects/{self._project_path}/merge_requests/{pr_number}/discussions",
                params={"per_page": 100, "page": page},
            )
            if not data:
                break
            for disc in data:
                disc_id = disc.get("id", "")
                for note in disc.get("notes", []) or []:
                    if note.get("position") is None:
                        continue
                    note_id = int(note["id"])
                    cache[note_id] = disc_id
                    pos = note.get("position") or {}
                    all_comments.append(PRComment(
                        id=note_id,
                        body=note.get("body", ""),
                        path=pos.get("new_path") or pos.get("old_path") or "",
                        line=pos.get("new_line") or pos.get("old_line"),
                        author=(note.get("author") or {}).get("username", ""),
                    ))
            if len(data) < 100:
                break
            page += 1

        self._discussion_cache[pr_number] = cache
        return all_comments
```

- [ ] **Step 4: Run tests; expect pass**

Run: `pytest tests/unit/test_gitlab_adapter_mr.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add integrations/gitlab/gitlab_adapter.py tests/unit/test_gitlab_adapter_mr.py
git commit -m "feat(gitlab): get_pr_comments with pagination and discussion cache"
```

---

## Task 7: `reply_to_comment` and `resolve_comment` (with cache fallback)

**Files:**
- Modify: `integrations/gitlab/gitlab_adapter.py`
- Modify: `tests/unit/test_gitlab_adapter_mr.py`

- [ ] **Step 1: Append failing reply/resolve tests**

Append to `tests/unit/test_gitlab_adapter_mr.py`:

```python
@pytest.mark.asyncio
async def test_reply_to_comment_uses_cached_discussion_id():
    adapter = _make_adapter()
    adapter._discussion_cache = {42: {101: "disc-X"}}
    adapter._request = AsyncMock(return_value={})

    await adapter.reply_to_comment(42, 101, "looks good")

    method, path = adapter._request.await_args.args[:2]
    kwargs = adapter._request.await_args.kwargs
    assert method == "POST"
    assert path == "/projects/group%2Fproj/merge_requests/42/discussions/disc-X/notes"
    assert kwargs["json"] == {"body": "looks good"}


@pytest.mark.asyncio
async def test_reply_to_comment_refetches_discussions_on_cache_miss():
    adapter = _make_adapter()
    # Cache is empty — adapter must refetch and then post.
    discussions_page = [
        {"id": "disc-Y", "notes": [
            {"id": 555, "body": "x", "author": {}, "position": {"new_path": "f", "new_line": 1}},
        ]},
    ]
    # Sequence: discussions GET (page 1), discussions GET (page 2 / empty), then POST reply
    adapter._request = AsyncMock(side_effect=[discussions_page, [], {}])

    await adapter.reply_to_comment(42, 555, "thanks")

    assert adapter._request.await_count == 3
    final_call = adapter._request.await_args_list[-1]
    method, path = final_call.args[:2]
    assert method == "POST"
    assert "/discussions/disc-Y/notes" in path


@pytest.mark.asyncio
async def test_reply_to_comment_raises_on_hard_miss():
    adapter = _make_adapter()
    # Refetch returns no matching note; adapter must raise.
    adapter._request = AsyncMock(side_effect=[[], []])  # both pages empty

    with pytest.raises(RuntimeError) as exc:
        await adapter.reply_to_comment(42, 9999, "hi")
    assert "9999" in str(exc.value)


@pytest.mark.asyncio
async def test_resolve_comment_uses_cached_discussion_id():
    adapter = _make_adapter()
    adapter._discussion_cache = {42: {101: "disc-X"}}
    adapter._request = AsyncMock(return_value={})

    await adapter.resolve_comment(42, 101)

    method, path = adapter._request.await_args.args[:2]
    kwargs = adapter._request.await_args.kwargs
    assert method == "PUT"
    assert path == "/projects/group%2Fproj/merge_requests/42/discussions/disc-X"
    assert kwargs["params"] == {"resolved": "true"}
```

- [ ] **Step 2: Run tests; expect failures**

Run: `pytest tests/unit/test_gitlab_adapter_mr.py -v`
Expected: 4 new failures (NotImplementedError).

- [ ] **Step 3: Implement `reply_to_comment`, `resolve_comment`, and `_lookup_discussion`**

In `integrations/gitlab/gitlab_adapter.py`, replace those two stubs and add a private helper:

```python
    async def _lookup_discussion(self, pr_number: int, note_id: int) -> str:
        """Return discussion_id for a note. Cache → refetch once → raise."""
        cached = self._discussion_cache.get(pr_number, {}).get(note_id)
        if cached:
            return cached
        # Refetch the MR's discussions (also refreshes the cache as a side effect).
        await self.get_pr_comments(pr_number)
        cached = self._discussion_cache.get(pr_number, {}).get(note_id)
        if cached:
            return cached
        raise RuntimeError(
            f"GitLab note {note_id} not found in MR !{pr_number} discussions; "
            f"cannot reply/resolve."
        )

    async def reply_to_comment(self, pr_number: int, comment_id: int, body: str) -> None:
        """Reply to a note by posting to its owning discussion."""
        disc_id = await self._lookup_discussion(pr_number, comment_id)
        await self._request(
            "POST",
            f"/projects/{self._project_path}/merge_requests/{pr_number}/discussions/{disc_id}/notes",
            json={"body": body},
        )

    async def resolve_comment(self, pr_number: int, comment_id: int) -> None:
        """Mark a discussion thread resolved. Idempotent."""
        disc_id = await self._lookup_discussion(pr_number, comment_id)
        await self._request(
            "PUT",
            f"/projects/{self._project_path}/merge_requests/{pr_number}/discussions/{disc_id}",
            params={"resolved": "true"},
        )
```

- [ ] **Step 4: Run tests; expect pass**

Run: `pytest tests/unit/test_gitlab_adapter_mr.py -v`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add integrations/gitlab/gitlab_adapter.py tests/unit/test_gitlab_adapter_mr.py
git commit -m "feat(gitlab): reply_to_comment and resolve_comment with cache+refetch"
```

---

## Task 8: `check_pr_status` and `close_pr`

**Files:**
- Modify: `integrations/gitlab/gitlab_adapter.py`
- Modify: `tests/unit/test_gitlab_adapter_mr.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/unit/test_gitlab_adapter_mr.py`:

```python
@pytest.mark.asyncio
async def test_check_pr_status_passing_when_latest_pipeline_success():
    adapter = _make_adapter()
    pipelines = [
        {"id": 1, "status": "failed", "created_at": "2026-05-12T10:00:00Z"},
        {"id": 2, "status": "success", "created_at": "2026-05-12T11:00:00Z"},
    ]
    adapter._request = AsyncMock(return_value=pipelines)

    status = await adapter.check_pr_status(42)

    assert status.all_passing is True
    assert len(status.checks) == 2


@pytest.mark.asyncio
async def test_check_pr_status_failing_when_latest_pipeline_failed():
    adapter = _make_adapter()
    pipelines = [
        {"id": 2, "status": "failed", "created_at": "2026-05-12T11:00:00Z"},
        {"id": 1, "status": "success", "created_at": "2026-05-12T10:00:00Z"},
    ]
    adapter._request = AsyncMock(return_value=pipelines)

    status = await adapter.check_pr_status(42)
    assert status.all_passing is False


@pytest.mark.asyncio
async def test_check_pr_status_no_pipelines_returns_not_passing():
    adapter = _make_adapter()
    adapter._request = AsyncMock(return_value=[])
    status = await adapter.check_pr_status(42)
    assert status.all_passing is False
    assert status.checks == []


@pytest.mark.asyncio
async def test_close_pr_sends_state_event_close():
    adapter = _make_adapter()
    adapter._request = AsyncMock(return_value={})

    await adapter.close_pr(42)

    method, path = adapter._request.await_args.args[:2]
    kwargs = adapter._request.await_args.kwargs
    assert method == "PUT"
    assert path == "/projects/group%2Fproj/merge_requests/42"
    assert kwargs["json"] == {"state_event": "close"}
```

- [ ] **Step 2: Run tests; expect failures**

Run: `pytest tests/unit/test_gitlab_adapter_mr.py -v`
Expected: 4 new failures (NotImplementedError).

- [ ] **Step 3: Implement `check_pr_status` and `close_pr`**

In `integrations/gitlab/gitlab_adapter.py`, replace those two stubs:

```python
    async def check_pr_status(self, pr_number: int) -> PRStatus:
        """CI gate = latest pipeline's status.

        A "passing" MR has a latest pipeline with status == "success".
        No pipeline → not passing (matches GitHub's behavior when there
        are zero check_runs).
        """
        pipelines = await self._request(
            "GET",
            f"/projects/{self._project_path}/merge_requests/{pr_number}/pipelines",
        )
        if not pipelines:
            return PRStatus(all_passing=False, checks=[])
        latest = max(pipelines, key=lambda p: p.get("created_at", ""))
        return PRStatus(
            all_passing=(latest.get("status") == "success"),
            checks=[
                {
                    "name": f"pipeline {p.get('id')}",
                    "status": p.get("status"),
                    "conclusion": p.get("status"),
                }
                for p in pipelines
            ],
        )

    async def close_pr(self, pr_number: int) -> None:
        """Close an MR without merging."""
        await self._request(
            "PUT",
            f"/projects/{self._project_path}/merge_requests/{pr_number}",
            json={"state_event": "close"},
        )
        logger.info("Closed MR !%d", pr_number)
```

- [ ] **Step 4: Run tests; expect pass**

Run: `pytest tests/unit/test_gitlab_adapter_mr.py -v`
Expected: 15 passed.

- [ ] **Step 5: Run the full adapter test suite as a checkpoint**

Run: `pytest tests/unit/test_gitlab_adapter_*.py -v`
Expected: all green; `GitLabAdapter` now implements all 10 VCSInterface methods.

- [ ] **Step 6: Commit**

```bash
git add integrations/gitlab/gitlab_adapter.py tests/unit/test_gitlab_adapter_mr.py
git commit -m "feat(gitlab): check_pr_status (latest pipeline) and close_pr"
```

---

## Task 9: Wire GitLab into `main.py`

**Files:**
- Modify: `main.py` (lines 230-360 area — see spec for exact slots)

This is a refactor + extension in one commit: extract a `_build_vcs_adapter` helper, replace both call sites, rename `github_adapters` → `vcs_adapters`. No tests run against `main.py` directly; the validation is a startup smoke later in Task 12.

- [ ] **Step 1: Add the helper above the adapter-init block**

In `main.py`, add the following near the top of the function that currently contains the adapter init (just before the `# Initialize integration adapters` comment block near line 230). Also add an import line at the top of the file if not already present:

```python
# Add this to the imports at the top of main.py if missing:
from config.schemas import RepoConfig
from integrations.base.vcs import VCSInterface


def _build_vcs_adapter(repo_cfg: RepoConfig) -> VCSInterface | None:
    """Return the VCS adapter for this repo, or None if the provider is
    unsupported or required credentials are missing."""
    provider = repo_cfg.vcs.provider
    if provider == "github" and repo_cfg.vcs.github.token:
        from integrations.github.github_adapter import GitHubAdapter
        return GitHubAdapter(
            token=repo_cfg.vcs.github.token,
            owner=repo_cfg.vcs.github.owner,
            repo=repo_cfg.vcs.github.repo,
        )
    if provider == "gitlab" and repo_cfg.vcs.gitlab.token:
        from integrations.gitlab.gitlab_adapter import GitLabAdapter
        return GitLabAdapter(
            token=repo_cfg.vcs.gitlab.token,
            project_id=repo_cfg.vcs.gitlab.project_id,
            url=repo_cfg.vcs.gitlab.url or "https://gitlab.com",
        )
    return None
```

- [ ] **Step 2: Replace the initial-load loop (`main.py` ~258-272)**

Replace this current block:

```python
    # VCS adapter — per-repo, default to first GitHub repo
    github_adapters = {}
    for proj_id, proj in projects.items():
        for repo_id, repo_cfg in proj.repos.items():
            if repo_cfg.vcs.provider == "github" and repo_cfg.vcs.github.token:
                from integrations.github.github_adapter import GitHubAdapter

                gh = GitHubAdapter(
                    token=repo_cfg.vcs.github.token,
                    owner=repo_cfg.vcs.github.owner,
                    repo=repo_cfg.vcs.github.repo,
                )
                github_adapters[repo_id] = (gh, repo_cfg)
                if vcs is None:
                    vcs = gh
                print(f"  GitHub adapter for {repo_id}: {repo_cfg.vcs.github.owner}/{repo_cfg.vcs.github.repo}")
```

With:

```python
    # VCS adapter — per-repo; first non-None adapter becomes the daemon
    # default (used as fallback when a workspace has no repo-scoped adapter).
    vcs_adapters: dict[str, tuple[VCSInterface, RepoConfig]] = {}
    for proj_id, proj in projects.items():
        for repo_id, repo_cfg in proj.repos.items():
            adapter = _build_vcs_adapter(repo_cfg)
            if adapter is None:
                continue
            vcs_adapters[repo_id] = (adapter, repo_cfg)
            if vcs is None:
                vcs = adapter
            print(f"  {repo_cfg.vcs.provider} adapter for {repo_id}")
```

- [ ] **Step 3: Replace the hot-reload `_build_repo_adapters` (`main.py` ~282-297)**

Replace this current block:

```python
    def _build_repo_adapters(project, logger_):
        """Build + register VCS adapters for each repo in a single project."""
        for repo_id, repo_cfg in project.repos.items():
            provider = repo_cfg.vcs.provider
            if provider == "github" and repo_cfg.vcs.github.token:
                from integrations.github.github_adapter import GitHubAdapter
                gh = GitHubAdapter(
                    token=repo_cfg.vcs.github.token,
                    owner=repo_cfg.vcs.github.owner,
                    repo=repo_cfg.vcs.github.repo,
                )
                orchestrator.register_repo_vcs(repo_id, gh, repo_cfg)
                logger_.info(
                    "Hot-reload: registered GitHub adapter for %s: %s/%s",
                    repo_id, repo_cfg.vcs.github.owner, repo_cfg.vcs.github.repo,
                )
```

With:

```python
    def _build_repo_adapters(project, logger_):
        """Build + register VCS adapters for each repo in a single project."""
        for repo_id, repo_cfg in project.repos.items():
            adapter = _build_vcs_adapter(repo_cfg)
            if adapter is None:
                continue
            orchestrator.register_repo_vcs(repo_id, adapter, repo_cfg)
            logger_.info(
                "Hot-reload: registered %s adapter for %s",
                repo_cfg.vcs.provider, repo_id,
            )
```

- [ ] **Step 4: Update the registration loop after orchestrator creation (`main.py` ~359-360)**

Find this current line:

```python
    # Register per-repo VCS adapters
    for repo_id, (gh_adapter, repo_cfg) in github_adapters.items():
        orchestrator.register_repo_vcs(repo_id, gh_adapter, repo_cfg)
```

Replace with:

```python
    # Register per-repo VCS adapters
    for repo_id, (adapter, repo_cfg) in vcs_adapters.items():
        orchestrator.register_repo_vcs(repo_id, adapter, repo_cfg)
```

- [ ] **Step 5: Verify nothing else references `github_adapters`**

Run: `grep -n "github_adapters" main.py`
Expected: no matches.

- [ ] **Step 6: Smoke-import `main` to catch any import / syntax errors**

Run: `python -c "import main"`
Expected: no traceback. (The import doesn't run the daemon; it just verifies the module parses and resolves imports.)

- [ ] **Step 7: Commit**

```bash
git add main.py
git commit -m "feat(main): per-repo VCS dispatch via _build_vcs_adapter (GitHub + GitLab)"
```

---

## Task 10: Wizard cleanup — `tracker_label` text + regression test

**Files:**
- Modify: `dashboard/project_create_payload.py:117`
- Modify: `tests/unit/test_project_create_payload.py`

- [ ] **Step 1: Add a failing regression assertion**

In `tests/unit/test_project_create_payload.py`, find `test_redact_to_input_md_contains_var_names_not_secrets` (line 105) and add a new test below it:

```python
def test_redact_to_input_md_uses_tracker_label_not_jira_repo_label():
    """The schema field is `tracker_label` post-refactor; the redacted
    input.md the atlas agent reads must use the current name."""
    md = redact_to_input_md(VALID_PAYLOAD)
    assert "tracker_label:" in md
    assert "tracker_label in repo YAML" in md
    assert "jira_repo_label" not in md
```

- [ ] **Step 2: Run the test; expect failure**

Run: `pytest tests/unit/test_project_create_payload.py::test_redact_to_input_md_uses_tracker_label_not_jira_repo_label -v`
Expected: AssertionError — `jira_repo_label` is still in the markdown.

- [ ] **Step 3: Fix the line in `dashboard/project_create_payload.py:117`**

Replace this line:

```python
    lines.append(f"- jira_repo_label: {repo_label}  # use this as jira_repo_label in repo YAML")
```

With:

```python
    lines.append(f"- tracker_label: {repo_label}  # use this as tracker_label in repo YAML")
```

- [ ] **Step 4: Run the test; expect pass**

Run: `pytest tests/unit/test_project_create_payload.py -v`
Expected: all green (existing tests + the new one).

- [ ] **Step 5: Commit**

```bash
git add dashboard/project_create_payload.py tests/unit/test_project_create_payload.py
git commit -m "chore(wizard): emit tracker_label not jira_repo_label in redacted input.md"
```

---

## Task 11: Rewrite `docs/features/gitlab-integration.md`

**Files:**
- Modify: `docs/features/gitlab-integration.md` (full rewrite)

No tests for this — pure docs.

- [ ] **Step 1: Read the existing file**

Run: `cat docs/features/gitlab-integration.md`
Expected: ~12 lines describing the legacy helper-wrapping plan.

- [ ] **Step 2: Replace the file contents**

Overwrite `docs/features/gitlab-integration.md` with:

```markdown
# Feature: GitLab Integration

**Status:** Implemented
**Created:** 2026-04-07
**Updated:** 2026-05-12
**Author:** Oleksandr Brazhenko

## Description

VCS adapter for GitLab. Implements the same `VCSInterface` as the GitHub
adapter, so a project with `vcs.provider: gitlab` runs the full pipeline
end-to-end: clone → branch → push → MR → review-comment loop → CI gate
→ DONE.

## Architecture

- `GitLabAdapter` lives in `integrations/gitlab/gitlab_adapter.py` and
  follows the same shape as `GitHubAdapter`: direct GitLab REST API v4
  via `httpx`, with retries, body-on-failure error surfacing, and an
  async git CLI helper for clone/branch/push.
- Authentication: `Private-Token` header for REST; clone/push uses the
  `https://oauth2:<token>@<host>/<namespace>/<project>.git` URL form so
  workspaces remain authenticated after a token rotation (origin URL is
  rewritten on each push).
- MR ↔ PR mapping: GitLab's MR `iid` is the public identifier and is
  returned by `open_pr`. Discussions with diff-position notes are
  surfaced as `PRComment` objects; general MR notes are skipped. A
  private `_discussion_cache[pr_number]` map records each note's owning
  `discussion_id` so `reply_to_comment` and `resolve_comment` can post
  to the right thread without changing the `VCSInterface` contract.
- CI gate: `check_pr_status` calls `GET /merge_requests/:iid/pipelines`
  and returns `all_passing = (latest.status == "success")`. No separate
  `ci.provider: gitlab_ci` is added — the VCS adapter owns pipeline
  status.

## Configuration

`vcs.provider: gitlab` plus a `vcs.gitlab` block — see `GitLabConfig`
in `config/schemas.py`. The dashboard's `+ New Project` wizard renders
the GitLab fields, validates them against the live API via
`validate_gitlab`, and writes the YAML for you.

## References

- Spec: `docs/superpowers/specs/2026-05-12-gitlab-vcs-adapter-design.md`
- Adapter: `integrations/gitlab/gitlab_adapter.py`
- Schema: `config/schemas.py` (`GitLabConfig`, `VCSConfig`)
- Live validator: `integrations/config/config_tools.py` (`validate_gitlab`)

## Out of scope

- GitLab CI as a distinct `ci.provider` (pipeline status is read by the
  VCS adapter).
- MR approval rules, auto-merge, merge-when-pipeline-succeeds.
- Live-network integration tests in CI; manual smoke against a real
  instance is the verification path.
```

- [ ] **Step 3: Commit**

```bash
git add docs/features/gitlab-integration.md
git commit -m "docs(gitlab): rewrite gitlab-integration.md for direct-API design"
```

---

## Task 12: Final verification — full test run + import smoke + acceptance walk

**Files:** none modified

- [ ] **Step 1: Run the full unit-test suite**

Run: `pytest tests/unit -v`
Expected: all green. No regressions.

- [ ] **Step 2: Run the broader test suite (integration tests gated on env vars stay skipped)**

Run: `pytest`
Expected: all collected tests pass; integration tests requiring live credentials skip cleanly.

- [ ] **Step 3: Import-smoke `main`**

Run: `python -c "import main; print('main imports OK')"`
Expected: prints `main imports OK`; no traceback.

- [ ] **Step 4: Walk the acceptance criteria from the spec**

For each acceptance criterion in `docs/superpowers/specs/2026-05-12-gitlab-vcs-adapter-design.md`, confirm:

  - `GitLabAdapter` implements all 10 `VCSInterface` methods — verify by `grep -E "async def (clone_repo|create_branch|push|open_pr|find_pr_by_branch|get_pr_comments|reply_to_comment|resolve_comment|check_pr_status|close_pr)" integrations/gitlab/gitlab_adapter.py` returns 10 lines.
  - `main.py` initial-load and hot-reload paths both instantiate `GitLabAdapter` for `provider == "gitlab"` repos — verify by `grep "GitLabAdapter\|_build_vcs_adapter" main.py` shows the helper + import in `_build_vcs_adapter`.
  - Existing GitHub-routed projects work unchanged — verify by `pytest tests/unit/test_github_adapter_*.py -v` all pass.
  - `redact_to_input_md` emits `tracker_label:` not `jira_repo_label:` — covered by Task 10's regression test.
  - Docs updated — verify by `head -5 docs/features/gitlab-integration.md` shows `Status: Implemented`.

- [ ] **Step 5: Final commit (only if something needed touching at this stage)**

If the verification surfaced no issues, no commit is needed. If it surfaced something missed, fix it and:

```bash
git add <fixed-files>
git commit -m "fix(gitlab): <what you fixed>"
```

- [ ] **Step 6: Manual smoke (optional, off-CI)**

In a sandbox: create a project via the dashboard wizard with `vcs.provider: gitlab` pointing at a real GitLab project; drop a labeled ticket into the linked Jira project; watch the pipeline push, open an MR, and report status. This is out of CI scope but is the canonical end-to-end verification of the feature.

---

## Notes for the implementer

- The pipeline's per-repo VCS dispatch (`Orchestrator.register_repo_vcs` / `_get_vcs_for_workspace`) is already provider-agnostic — no orchestrator code changes.
- `VCSInterface` is stable for this work — do not add `thread_id` or any GitLab-specific fields to it.
- `PRComment.id` stays `int`; GitLab note IDs are integers. The string `discussion_id` lives only in the adapter's private cache.
- `_discussion_cache` is never invalidated. This is intentional: discussion IDs are stable for a note's lifetime, and a daemon restart clears the cache anyway. Don't add a TTL or eviction policy.
- If you hit a test that's hard to write with `unittest.mock`, prefer adding a helper to the test module over importing `respx` here — the GitHub adapter tests use plain mocks; we match that convention. (Integration tests do use `respx`, but those are out of scope.)
- Don't touch `HelpersConfig` GitLab slots (`fetch_mr_comments`, `resolve_mr_comments`, `post_review_comments`). They're dead but their removal is explicitly out of scope per the spec.
