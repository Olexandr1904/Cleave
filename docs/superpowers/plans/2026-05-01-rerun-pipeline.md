# Rerun Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Rerun button (DONE state only) that re-clones the repo, refreshes ticket data, and restarts the pipeline from ANALYSIS with a user-provided reason as BA context.

**Architecture:** Five focused changes: (1) `WorkspaceManager.reset_source()` handles reclone with branch fallback, (2) `Orchestrator._refetch_ticket_data()` extracted from `_create_workspace_for_ticket` with append-on-rerun logic, (3) a `/rerun` endpoint wires it together and sends a Telegram notification, (4–5) frontend adds a Rerun button with a textarea dialog.

**Tech Stack:** Python/asyncio (backend), Starlette (routing), vanilla JS (frontend), subprocess/git (clone), pytest (tests)

---

## File Map

| File | Change |
|------|--------|
| `workspace/workspace.py` | Add `Stage.ANALYSIS` to `DONE` valid transitions |
| `workspace/workspace_manager.py` | Add `reset_source()` |
| `orchestrator/orchestrator.py` | Extract `_refetch_ticket_data()`, update `_create_workspace_for_ticket`, add `_notify_rerun()` |
| `dashboard/actions.py` | Add `rerun` endpoint + route registration |
| `dashboard/static/js/actions.js` | Add `rerunWorkspace()` |
| `dashboard/static/js/detail.js` | Import `rerunWorkspace`, add Rerun button + binding |
| `tests/unit/test_workspace_manager.py` | Add `reset_source` tests |
| `tests/unit/test_orchestrator_refetch.py` | New file: `_refetch_ticket_data` tests |
| `tests/unit/test_dashboard_actions.py` | Add rerun endpoint tests |

---

## Task 1: Allow DONE → ANALYSIS transition

**Files:**
- Modify: `workspace/workspace.py`
- Test: `tests/unit/test_session_fixes.py` (existing file that tests transitions)

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_session_fixes.py`:

```python
def test_done_can_transition_to_analysis(tmp_path):
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    (ws_root / "meta").mkdir()
    state = WorkspaceState(
        ticket_id="T-1",
        company_id="co",
        repo_id="repo",
        workspace_root=str(ws_root),
        current_state="DONE",
    )
    ws = Workspace(str(ws_root), state)
    ws.transition(Stage.ANALYSIS)
    assert ws.state.current_state == Stage.ANALYSIS
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/test_session_fixes.py::test_done_can_transition_to_analysis -v
```

Expected: FAIL — `InvalidTransitionError: Cannot transition from DONE to ANALYSIS`

- [ ] **Step 3: Add ANALYSIS to DONE's valid transitions**

In `workspace/workspace.py`, find line:
```python
Stage.DONE:               {Stage.ARCHIVED},
```
Replace with:
```python
Stage.DONE:               {Stage.ARCHIVED, Stage.ANALYSIS},
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/unit/test_session_fixes.py::test_done_can_transition_to_analysis -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add workspace/workspace.py tests/unit/test_session_fixes.py
git commit -m "feat(rerun): allow DONE → ANALYSIS transition"
```

---

## Task 2: WorkspaceManager.reset_source()

**Files:**
- Modify: `workspace/workspace_manager.py`
- Test: `tests/unit/test_workspace_manager.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_workspace_manager.py`:

```python
class TestResetSource:
    def _make_ws(self, tmp_path, branch="feature/T-1-t-1"):
        ws_root = tmp_path / "acme" / "repo" / "tickets" / "T-1"
        ws_root.mkdir(parents=True)
        (ws_root / "meta").mkdir()
        state = WorkspaceState(
            ticket_id="T-1",
            company_id="acme",
            repo_id="repo",
            workspace_root=str(ws_root),
            branch=branch,
        )
        ws = Workspace(str(ws_root), state)
        ws.save_state()
        return ws

    @patch("subprocess.run")
    def test_clones_and_checks_out_feature_branch(self, mock_run, tmp_path):
        ws = self._make_ws(tmp_path)
        # clone succeeds; checkout feature branch succeeds
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr=""),   # git clone
            MagicMock(returncode=0, stderr=""),   # git checkout feature/T-1-t-1
        ]
        manager = WorkspaceManager(str(tmp_path))
        # create the source dir so rmtree runs
        ws.source_dir.mkdir(parents=True)

        branch = manager.reset_source(ws, "https://git.example.com/repo.git", "develop")

        assert branch == "feature/T-1-t-1"
        clone_call = mock_run.call_args_list[0]
        assert clone_call[0][0][0] == "git"
        assert clone_call[0][0][1] == "clone"
        checkout_call = mock_run.call_args_list[1]
        assert "feature/T-1-t-1" in checkout_call[0][0]

    @patch("subprocess.run")
    def test_falls_back_to_default_branch_when_feature_missing(self, mock_run, tmp_path):
        ws = self._make_ws(tmp_path)
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr=""),   # git clone
            MagicMock(returncode=1, stderr="pathspec did not match"),  # checkout feature fails
            MagicMock(returncode=0, stderr=""),   # git checkout develop
        ]
        manager = WorkspaceManager(str(tmp_path))
        ws.source_dir.mkdir(parents=True)

        branch = manager.reset_source(ws, "https://git.example.com/repo.git", "develop")

        assert branch == "develop"

    @patch("subprocess.run")
    def test_works_when_source_already_absent(self, mock_run, tmp_path):
        ws = self._make_ws(tmp_path)
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr=""),  # git clone
            MagicMock(returncode=0, stderr=""),  # git checkout feature
        ]
        manager = WorkspaceManager(str(tmp_path))
        # source_dir does NOT exist — should not raise

        branch = manager.reset_source(ws, "https://git.example.com/repo.git", "develop")
        assert branch == "feature/T-1-t-1"

    @patch("subprocess.run")
    def test_raises_workspace_error_on_clone_failure(self, mock_run, tmp_path):
        ws = self._make_ws(tmp_path)
        mock_run.return_value = MagicMock(returncode=1, stderr="Repository not found")
        manager = WorkspaceManager(str(tmp_path))

        with pytest.raises(WorkspaceError, match="Git clone failed"):
            manager.reset_source(ws, "https://git.example.com/repo.git", "develop")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_workspace_manager.py::TestResetSource -v
```

Expected: FAIL — `AttributeError: 'WorkspaceManager' object has no attribute 'reset_source'`

- [ ] **Step 3: Implement reset_source()**

Add after `cleanup_source()` in `workspace/workspace_manager.py`:

```python
def reset_source(
    self,
    workspace: Workspace,
    clone_url: str,
    default_branch: str,
) -> str:
    """Re-clone source for a ticket rerun.

    Wipes existing source/ if present, clones fresh, then checks out
    workspace.state.branch. Falls back to default_branch if the remote
    branch no longer exists. Returns the branch actually checked out.
    """
    source_dir = workspace.source_dir
    branch_name = workspace.state.branch or default_branch

    if source_dir.exists():
        shutil.rmtree(source_dir)

    result = subprocess.run(
        ["git", "clone", clone_url, str(source_dir)],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    if result.returncode != 0:
        raise WorkspaceError(f"Git clone failed: {result.stderr.strip()}")

    checkout = subprocess.run(
        ["git", "checkout", branch_name],
        cwd=str(source_dir),
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    checked_out = branch_name
    if checkout.returncode != 0:
        subprocess.run(
            ["git", "checkout", default_branch],
            cwd=str(source_dir),
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        checked_out = default_branch

    (source_dir / "reports").mkdir(parents=True, exist_ok=True)
    logger.info(
        "Reset source for %s — checked out %s",
        workspace.state.ticket_id,
        checked_out,
    )
    return checked_out
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/test_workspace_manager.py::TestResetSource -v
```

Expected: all 4 PASS

- [ ] **Step 5: Commit**

```bash
git add workspace/workspace_manager.py tests/unit/test_workspace_manager.py
git commit -m "feat(rerun): add WorkspaceManager.reset_source()"
```

---

## Task 3: Orchestrator._refetch_ticket_data() + _notify_rerun()

**Files:**
- Modify: `orchestrator/orchestrator.py`
- Create: `tests/unit/test_orchestrator_refetch.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_orchestrator_refetch.py`:

```python
"""Tests for Orchestrator._refetch_ticket_data()."""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.orchestrator import Orchestrator
from workspace.workspace import Workspace, WorkspaceState


def _make_orch(tracker=None):
    orch = Orchestrator.__new__(Orchestrator)
    orch._tracker = tracker
    return orch


def _make_ws(tmp_path: Path, ticket_id: str = "T-1") -> Workspace:
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    meta = ws_root / "meta"
    meta.mkdir()
    state = WorkspaceState(
        ticket_id=ticket_id,
        company_id="co",
        repo_id="repo",
        workspace_root=str(ws_root),
    )
    ws = Workspace(str(ws_root), state)
    ws.save_state()
    return ws


def _make_tracker(ticket_summary="Summary", comments=None, changelog=None):
    tracker = MagicMock()
    ticket = SimpleNamespace(
        id="T-1",
        summary=ticket_summary,
        description="Description",
        status="In Progress",
        labels=[],
        attachments=[],
        linked_issues=[],
        assignee=None,
        reporter=None,
        priority=None,
        created=None,
        updated=None,
    )
    tracker.get_ticket = AsyncMock(return_value=ticket)
    tracker._request = AsyncMock(return_value={
        "fields": {
            "comment": {"comments": comments or []},
        },
        "changelog": {"histories": changelog or []},
    })
    return tracker


@pytest.mark.asyncio
async def test_writes_fresh_ticket_md_on_first_run(tmp_path):
    tracker = _make_tracker(ticket_summary="Login screen flickers")
    orch = _make_orch(tracker)
    ws = _make_ws(tmp_path)

    await orch._refetch_ticket_data(ws)

    content = (ws.meta_dir / "ticket.md").read_text()
    assert "Login screen flickers" in content


@pytest.mark.asyncio
async def test_appends_refresh_block_on_rerun(tmp_path):
    tracker = _make_tracker(ticket_summary="Updated description")
    orch = _make_orch(tracker)
    ws = _make_ws(tmp_path)
    (ws.meta_dir / "ticket.md").write_text("# T-1\n\nOriginal description\n")

    await orch._refetch_ticket_data(ws)

    content = (ws.meta_dir / "ticket.md").read_text()
    assert "Original description" in content
    assert "## Refresh" in content
    assert "Updated description" in content


@pytest.mark.asyncio
async def test_appends_only_new_comments(tmp_path):
    existing_comment = {
        "id": "100",
        "author": {"displayName": "Alice"},
        "created": "2026-04-01T10:00:00Z",
        "body": "First comment",
    }
    new_comment = {
        "id": "101",
        "author": {"displayName": "Bob"},
        "created": "2026-05-01T10:00:00Z",
        "body": "New comment after rerun",
    }
    tracker = _make_tracker(comments=[existing_comment, new_comment])
    orch = _make_orch(tracker)
    ws = _make_ws(tmp_path)
    # Simulate existing comments.md with comment 100 already written
    (ws.meta_dir / "comments.md").write_text(
        "# Jira Comments\n\n<!-- comment:100 -->\n## Alice (2026-04-01)\n\nFirst comment\n"
    )

    await orch._refetch_ticket_data(ws)

    content = (ws.meta_dir / "comments.md").read_text()
    assert "First comment" in content
    assert content.count("First comment") == 1  # not duplicated
    assert "New comment after rerun" in content
    assert "<!-- comment:101 -->" in content


@pytest.mark.asyncio
async def test_appends_only_new_history_entries(tmp_path):
    histories = [
        {
            "created": "2026-04-01T09:00:00Z",
            "author": {"displayName": "PM"},
            "items": [{"field": "status", "fromString": "To Do", "toString": "In Progress"}],
        },
        {
            "created": "2026-05-01T09:00:00Z",
            "author": {"displayName": "QA"},
            "items": [{"field": "status", "fromString": "In Progress", "toString": "Done"}],
        },
    ]
    tracker = _make_tracker(changelog=histories)
    orch = _make_orch(tracker)
    ws = _make_ws(tmp_path)
    # Existing history has first entry only
    (ws.meta_dir / "history.md").write_text(
        "# Status History\n\n- 2026-04-01: To Do → In Progress by PM\n"
    )

    await orch._refetch_ticket_data(ws)

    content = (ws.meta_dir / "history.md").read_text()
    assert "To Do → In Progress" in content
    assert content.count("To Do → In Progress") == 1  # not duplicated
    assert "In Progress → Done" in content


@pytest.mark.asyncio
async def test_no_op_when_no_tracker(tmp_path):
    orch = _make_orch(tracker=None)
    ws = _make_ws(tmp_path)

    # Should not raise
    await orch._refetch_ticket_data(ws)

    assert not (ws.meta_dir / "ticket.md").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_orchestrator_refetch.py -v
```

Expected: FAIL — `AttributeError: '_refetch_ticket_data' not found`

- [ ] **Step 3: Extract _refetch_ticket_data() into the Orchestrator**

In `orchestrator/orchestrator.py`, add this method. Place it just before `_create_workspace_for_ticket` (around line 554):

```python
async def _refetch_ticket_data(self, workspace: Workspace) -> None:
    """Write or append ticket meta files (ticket.md, comments.md, history.md).

    First run (file absent): writes fresh content.
    Rerun (file exists): appends a timestamped refresh block so agents can
    see what changed between runs.
    """
    import re
    from datetime import date

    ticket_id = workspace.state.ticket_id

    if self._tracker:
        try:
            ticket = await self._tracker.get_ticket(ticket_id)
            ticket_md = _ticket_to_markdown(ticket)
            ticket_file = workspace.meta_dir / "ticket.md"
            if ticket_file.exists():
                refresh_date = date.today().isoformat()
                ticket_file.write_text(
                    ticket_file.read_text(encoding="utf-8")
                    + f"\n\n## Refresh {refresh_date}\n\n{ticket_md}",
                    encoding="utf-8",
                )
            else:
                ticket_file.write_text(ticket_md, encoding="utf-8")
        except Exception as e:
            logger.warning(
                "Failed to refetch ticket description for %s: %s", ticket_id, e
            )

    if self._tracker and hasattr(self._tracker, "_request"):
        try:
            data = await self._tracker._request(
                "GET", f"/issue/{ticket_id}?expand=changelog&fields=comment",
            )

            # Comments — write fresh on first run, append only new ones on rerun
            comments = (
                data.get("fields", {}).get("comment", {}).get("comments", [])
            )
            if comments:
                comments_file = workspace.meta_dir / "comments.md"
                existing_ids: set[str] = set()
                if comments_file.exists():
                    existing_ids = set(
                        re.findall(
                            r"<!-- comment:(\S+) -->",
                            comments_file.read_text(encoding="utf-8"),
                        )
                    )
                new_lines: list[str] = []
                for c in comments:
                    cid = str(c.get("id", ""))
                    if cid and cid in existing_ids:
                        continue
                    author = c.get("author", {}).get("displayName", "?")
                    created = c.get("created", "")[:10]
                    body = c.get("body", "")
                    if isinstance(body, dict):
                        from integrations.jira.jira_adapter import _extract_adf_text
                        body = _extract_adf_text(body)
                    marker = f"<!-- comment:{cid} -->\n" if cid else ""
                    new_lines.append(
                        f"{marker}## {author} ({created})\n\n{body}\n"
                    )
                if new_lines:
                    block = "\n".join(new_lines)
                    if comments_file.exists():
                        comments_file.write_text(
                            comments_file.read_text(encoding="utf-8")
                            + "\n"
                            + block,
                            encoding="utf-8",
                        )
                    else:
                        comments_file.write_text(
                            "# Jira Comments\n\n" + block, encoding="utf-8"
                        )

            # History — write fresh on first run, append only new lines on rerun
            changelog = data.get("changelog", {}).get("histories", [])
            history_file = workspace.meta_dir / "history.md"
            existing_history = (
                history_file.read_text(encoding="utf-8")
                if history_file.exists()
                else ""
            )
            new_changes: list[str] = []
            for h in changelog:
                for item in h.get("items", []):
                    if item.get("field") == "status":
                        line = (
                            f"- {h.get('created', '')[:10]}: "
                            f"{item.get('fromString', '?')} → "
                            f"{item.get('toString', '?')} "
                            f"by {h.get('author', {}).get('displayName', '?')}"
                        )
                        if line not in existing_history:
                            new_changes.append(line)
            if new_changes:
                new_block = "\n".join(new_changes) + "\n"
                if history_file.exists():
                    history_file.write_text(
                        existing_history.rstrip() + "\n" + new_block,
                        encoding="utf-8",
                    )
                else:
                    history_file.write_text(
                        "# Status History\n\n" + new_block, encoding="utf-8"
                    )

        except Exception as e:
            logger.warning(
                "Failed to refetch comments/history for %s: %s", ticket_id, e
            )
```

- [ ] **Step 4: Update _create_workspace_for_ticket to call _refetch_ticket_data**

In `orchestrator/orchestrator.py`, replace the inline block in `_create_workspace_for_ticket` that writes ticket.md and fetches comments/history/attachments. Find these lines (approximately 597–665):

```python
        # Write ticket data as markdown
        ticket_md = _ticket_to_markdown(pt.ticket)
        (ws.meta_dir / "ticket.md").write_text(ticket_md, encoding="utf-8")

        # Fetch Jira comments and status history for agent context
        if self._tracker and hasattr(self._tracker, '_request'):
            try:
                data = await self._tracker._request(
                    ...long block...
                )
            except Exception as e:
                logger.warning("Failed to fetch comments/history for %s: %s", pt.ticket.id, e)

        # Download ticket attachments (screenshots, images)
        if pt.ticket.attachments:
            ...long block...
```

Replace the entire block (from `# Write ticket data` through the end of the attachments block) with:

```python
        # Write ticket metadata — calls tracker for comments/history/attachments
        await self._refetch_ticket_data(ws)
```

**Note:** The `# Fetch and write parent ticket if linked` block and the `# Transition Jira to In Progress` block that follow must remain unchanged.

- [ ] **Step 5: Add _notify_rerun()**

Add after `_notify_failed()` in `orchestrator/orchestrator.py`:

```python
    async def _notify_rerun(
        self, workspace: Workspace, branch: str, reason: str
    ) -> None:
        """Send Telegram notification when a rerun is triggered from the dashboard."""
        if self._notifier is None:
            return
        chat_id = self._get_chat_id(workspace)
        state = workspace.state
        title = self._get_ticket_title(workspace)
        hdr = self._tg_header("🔄", state, title)
        first_line = reason.splitlines()[0][:80] if reason else ""
        msg = (
            f"{hdr}\n"
            f"Rerun started from dashboard.\n"
            f"Branch: {branch}\n"
            f"Reason: {first_line}"
        )
        try:
            await self._notifier.send_message(chat_id, msg)
        except Exception as e:
            logger.warning("Failed to send rerun notification: %s", e)
```

- [ ] **Step 6: Run refetch tests to verify they pass**

```bash
pytest tests/unit/test_orchestrator_refetch.py -v
```

Expected: all 5 PASS

- [ ] **Step 7: Run full test suite to verify no regressions**

```bash
pytest tests/unit/ -x -q
```

Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_orchestrator_refetch.py
git commit -m "feat(rerun): extract _refetch_ticket_data(), add _notify_rerun()"
```

---

## Task 4: /rerun endpoint

**Files:**
- Modify: `dashboard/actions.py`
- Test: `tests/unit/test_dashboard_actions.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_dashboard_actions.py`:

```python
class TestRerunEndpoint:
    def _make_done_workspace(self, tmp_path, ticket_id="T-1"):
        ws_root = tmp_path / "ws"
        ws_root.mkdir(parents=True)
        meta = ws_root / "meta"
        meta.mkdir()
        ws = MagicMock(spec=Workspace)
        ws.state = WorkspaceState(
            ticket_id=ticket_id,
            company_id="test-co",
            repo_id="test-repo",
            workspace_root=str(ws_root),
            current_state="DONE",
            pr_number=42,
            pr_url="https://github.com/pr/42",
            review_cycle=2,
        )
        ws.source_dir = MagicMock()
        ws.source_dir.__str__ = lambda self: str(ws_root / "source")
        ws.meta_dir = meta
        ws.reports_dir = MagicMock()
        return ws

    def _make_client(self, bus, store, orchestrator, mode_handler, tmp_path):
        # global_config with project/repo
        repo_config = MagicMock()
        repo_config.git.clone_url = "https://git.example.com/repo.git"
        repo_config.vcs.provider = "github"
        repo_config.vcs.github.default_branch = "develop"
        project = MagicMock()
        project.repos.get = MagicMock(return_value=repo_config)
        global_config = MagicMock()
        global_config.workspaces.base_dir = str(tmp_path)
        orchestrator._projects = {"test-co": project}
        app = create_app(
            bus, store,
            orchestrator=orchestrator,
            mode_handler=mode_handler,
            global_config=global_config,
        )
        return TestClient(app)

    def test_rerun_transitions_to_analysis(self, bus, store, orchestrator, mode_handler, tmp_path):
        ws = self._make_done_workspace(tmp_path)
        orchestrator.get_active_workspaces.return_value = [ws]
        orchestrator._refetch_ticket_data = AsyncMock()
        orchestrator._workspace_manager = MagicMock()
        orchestrator._workspace_manager.reset_source = MagicMock(return_value="feature/T-1-t-1")
        orchestrator._notify_rerun = AsyncMock()
        client = self._make_client(bus, store, orchestrator, mode_handler, tmp_path)

        resp = client.post("/api/workspaces/T-1/rerun", json={"reason": "QA found login bug"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["new_state"] == "ANALYSIS"
        assert data["branch"] == "feature/T-1-t-1"

    def test_rerun_rejects_non_done_state(self, bus, store, orchestrator, mode_handler, tmp_path):
        ws = MagicMock(spec=Workspace)
        ws.state = WorkspaceState(
            ticket_id="T-1", company_id="test-co", repo_id="test-repo",
            workspace_root="/tmp/x", current_state="FAILED",
        )
        orchestrator.get_active_workspaces.return_value = [ws]
        client = self._make_client(bus, store, orchestrator, mode_handler, tmp_path)

        resp = client.post("/api/workspaces/T-1/rerun", json={"reason": "reason"})

        assert resp.status_code == 400
        assert "Cannot rerun" in resp.json()["message"]

    def test_rerun_requires_reason(self, bus, store, orchestrator, mode_handler, tmp_path):
        ws = self._make_done_workspace(tmp_path)
        orchestrator.get_active_workspaces.return_value = [ws]
        client = self._make_client(bus, store, orchestrator, mode_handler, tmp_path)

        resp = client.post("/api/workspaces/T-1/rerun", json={"reason": "  "})

        assert resp.status_code == 400
        assert "reason" in resp.json()["message"]

    def test_rerun_writes_rerun_history(self, bus, store, orchestrator, mode_handler, tmp_path):
        ws = self._make_done_workspace(tmp_path)
        orchestrator.get_active_workspaces.return_value = [ws]
        orchestrator._refetch_ticket_data = AsyncMock()
        orchestrator._workspace_manager = MagicMock()
        orchestrator._workspace_manager.reset_source = MagicMock(return_value="develop")
        orchestrator._notify_rerun = AsyncMock()
        client = self._make_client(bus, store, orchestrator, mode_handler, tmp_path)

        client.post("/api/workspaces/T-1/rerun", json={"reason": "Post-QA regression"})

        rerun_file = ws.meta_dir / "rerun_history.md"
        assert rerun_file.exists()
        content = rerun_file.read_text()
        assert "Post-QA regression" in content
        assert "## Rerun" in content

    def test_rerun_clears_stale_pr_fields(self, bus, store, orchestrator, mode_handler, tmp_path):
        ws = self._make_done_workspace(tmp_path)
        orchestrator.get_active_workspaces.return_value = [ws]
        orchestrator._refetch_ticket_data = AsyncMock()
        orchestrator._workspace_manager = MagicMock()
        orchestrator._workspace_manager.reset_source = MagicMock(return_value="feature/T-1-t-1")
        orchestrator._notify_rerun = AsyncMock()
        client = self._make_client(bus, store, orchestrator, mode_handler, tmp_path)

        client.post("/api/workspaces/T-1/rerun", json={"reason": "retry"})

        assert ws.state.pr_number is None
        assert ws.state.pr_url is None
        assert ws.state.review_cycle == 0
        assert ws.state.error is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_dashboard_actions.py::TestRerunEndpoint -v
```

Expected: FAIL — 404 on `/api/workspaces/T-1/rerun` (route not registered yet)

- [ ] **Step 3: Implement the rerun endpoint**

In `dashboard/actions.py`, add the following function inside `build_action_routes()`, after `clean_source` and before the `return [...]` block:

```python
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
```

- [ ] **Step 4: Register the route**

In the `return [...]` block at the bottom of `build_action_routes()`, add before the closing bracket:

```python
        Route("/api/workspaces/{ticket_id:path}/rerun", rerun, methods=["POST"]),
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/unit/test_dashboard_actions.py::TestRerunEndpoint -v
```

Expected: all 5 PASS

- [ ] **Step 6: Run full test suite**

```bash
pytest tests/unit/ -x -q
```

Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add dashboard/actions.py tests/unit/test_dashboard_actions.py
git commit -m "feat(rerun): add /rerun endpoint"
```

---

## Task 5: Frontend — rerunWorkspace() in actions.js

**Files:**
- Modify: `dashboard/static/js/actions.js`

No automated tests for JS — verify manually in the browser after Task 6.

- [ ] **Step 1: Add rerunWorkspace() to actions.js**

In `dashboard/static/js/actions.js`, add after `archiveWorkspace()`:

```js
export function rerunWorkspace(ticketId) {
  return new Promise((resolve, reject) => {
    const overlay = document.createElement('div');
    overlay.className = 'dialog-overlay';
    overlay.innerHTML = `<div class="dialog">
      <div class="dialog-title">Rerun pipeline for ${esc(ticketId)}?</div>
      <div>
        <p style="margin:0 0 8px;font-size:12px;color:#8b949e;">Describe why this ticket needs to be rerun. This will be passed to the BA agent as context.</p>
        <textarea id="rerun-reason" class="manual-comment" placeholder="e.g. Post-QA regression: login crashes on Android 14" style="width:100%;min-height:80px;"></textarea>
      </div>
      <div class="dialog-actions">
        <button class="action-btn btn-retry" id="dlg-cancel">Cancel</button>
        <button class="action-btn btn-take-control" id="dlg-confirm" disabled>Rerun</button>
      </div>
    </div>`;
    document.body.appendChild(overlay);

    const textarea = overlay.querySelector('#rerun-reason');
    const confirmBtn = overlay.querySelector('#dlg-confirm');

    textarea.addEventListener('input', () => {
      confirmBtn.disabled = textarea.value.trim().length === 0;
    });

    overlay.querySelector('#dlg-cancel').onclick = () => {
      overlay.remove();
      reject(new Error('cancelled'));
    };
    confirmBtn.onclick = async () => {
      const reason = textarea.value.trim();
      overlay.remove();
      try {
        const result = await postJSON(
          `/api/workspaces/${encodeURIComponent(ticketId)}/rerun`,
          { reason },
        );
        resolve(result);
      } catch (e) {
        reject(e);
      }
    };
    overlay.onclick = (e) => { if (e.target === overlay) { overlay.remove(); reject(new Error('cancelled')); } };
  });
}
```

- [ ] **Step 2: Commit**

```bash
git add dashboard/static/js/actions.js
git commit -m "feat(rerun): add rerunWorkspace() dialog to actions.js"
```

---

## Task 6: Frontend — Rerun button in detail.js

**Files:**
- Modify: `dashboard/static/js/detail.js`

- [ ] **Step 1: Add rerunWorkspace to the import line**

In `dashboard/static/js/detail.js`, find line 7:
```js
import { approveWorkspace, rejectWorkspace, retryWorkspace, takeControl, releaseControl, resumeWorkspace, archiveWorkspace, pauseWorkspace, unpauseWorkspace, showConfirmDialog } from './actions.js';
```

Replace with:
```js
import { approveWorkspace, rejectWorkspace, retryWorkspace, takeControl, releaseControl, resumeWorkspace, archiveWorkspace, pauseWorkspace, unpauseWorkspace, showConfirmDialog, rerunWorkspace } from './actions.js';
```

- [ ] **Step 2: Add Rerun button to buildActionBar()**

In `dashboard/static/js/detail.js`, inside `buildActionBar()`, find:
```js
  if (canArchive) {
    buttons += `<button class="action-btn btn-reject" id="act-archive">Archive</button>`;
  }
```

Add after it:
```js
  if (stateVal === 'DONE') {
    buttons += `<button class="action-btn btn-retry" id="act-rerun">Rerun</button>`;
  }
```

- [ ] **Step 3: Bind the Rerun button in bindActionButtons()**

In `dashboard/static/js/detail.js`, inside `bindActionButtons()`, add after the Archive binding block (around line 350):

```js
  // Rerun (from DONE)
  const rerunBtn = document.getElementById('act-rerun');
  if (rerunBtn) {
    rerunBtn.addEventListener('click', async () => {
      try {
        const result = await rerunWorkspace(ticketId);
        if (result && result.branch) {
          const msg = result.branch.startsWith('feature/')
            ? `Rerun started on ${result.branch}`
            : `Feature branch not found — checked out ${result.branch}`;
          alert(msg);
        }
        await renderDetail(ticketId, onBack);
      } catch (e) {
        if (e.message !== 'cancelled') alert('Rerun failed: ' + e.message);
      }
    });
  }
```

- [ ] **Step 4: Manual smoke test**

Start the dashboard:
```bash
python -m dashboard.web
```

1. Open a ticket in DONE state.
2. Confirm a "Rerun" button appears in the action bar.
3. Click Rerun — a dialog appears with a textarea and a disabled "Rerun" button.
4. Type a reason — "Rerun" button enables.
5. Submit — the detail view reloads showing ANALYSIS state.
6. Clear the textarea — confirm "Rerun" stays disabled.
7. Cancel — confirm no state change.

- [ ] **Step 5: Commit**

```bash
git add dashboard/static/js/detail.js
git commit -m "feat(rerun): add Rerun button to DONE state action bar"
```
