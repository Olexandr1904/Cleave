# Dashboard Ticket Card Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace board ticket cards with a title-first layout, persisting `title` in `state.json` and exposing it via the dashboard API.

**Architecture:** Backend adds `title` to `WorkspaceState`, populated at workspace creation from `TicketData.summary` (or `"Workspace setup"` for setup workspaces). The dashboard's disk scan backfills missing titles from `meta/ticket.md` (idempotent, one-shot) and pipes the field through the API. Frontend rewrites `renderCard()` and the card CSS to a five-line layout (header / title+state / optional note / meta / optional footer).

**Tech Stack:** Python 3.12, Starlette, pytest, vanilla JS modules, plain CSS.

**Spec:** [docs/superpowers/specs/2026-04-27-dashboard-ticket-card-redesign.md](docs/superpowers/specs/2026-04-27-dashboard-ticket-card-redesign.md)

---

## File Map

**Modify:**
- [workspace/workspace.py](workspace/workspace.py) — add `title` field
- [workspace/workspace_manager.py](workspace/workspace_manager.py) — `create()` accepts and persists `title`
- [orchestrator/orchestrator.py](orchestrator/orchestrator.py) — pass `pt.ticket.summary` into `create()`
- [dashboard/setup_workspace.py](dashboard/setup_workspace.py) — set static `title = "Workspace setup"`
- [dashboard/web.py](dashboard/web.py) — backfill from `meta/ticket.md`, include `title` in API response
- [dashboard/static/js/board.js](dashboard/static/js/board.js) — rewrite `renderCard()`
- [dashboard/static/style.css](dashboard/static/style.css) — replace card rules

**Modify (tests):**
- [tests/unit/test_workspace.py](tests/unit/test_workspace.py)
- [tests/unit/test_workspace_manager.py](tests/unit/test_workspace_manager.py)
- [tests/unit/test_setup_workspace.py](tests/unit/test_setup_workspace.py)
- [tests/unit/test_dashboard_web.py](tests/unit/test_dashboard_web.py)

No new files.

---

## Conventions for this plan

- Run pytest via the venv: `.venv/bin/pytest`. The repo is at `/home/admin0/tot` — run commands from there.
- Commit at the end of every task. One task = one commit. Commit messages use `feat:`, `refactor:`, `test:` prefixes (matches the repo's style — see `git log --oneline | head`).
- Frontend has no JS test runner. Verification is visual via `python -m main` on the dev port and browser inspection.

---

## Task 1: Add `title` field to `WorkspaceState`

**Files:**
- Modify: `workspace/workspace.py:56-82`
- Test: `tests/unit/test_workspace.py:45-67` (`TestWorkspaceState`)

- [ ] **Step 1: Add a failing test for the new field**

In `tests/unit/test_workspace.py`, inside `class TestWorkspaceState`, add:

```python
    def test_title_defaults_to_none(self):
        state = WorkspaceState(
            ticket_id="T-1", company_id="c", repo_id="r", workspace_root="/tmp"
        )
        assert state.title is None
```

- [ ] **Step 2: Run test, verify it fails**

```bash
cd /home/admin0/tot
.venv/bin/pytest tests/unit/test_workspace.py::TestWorkspaceState::test_title_defaults_to_none -v
```

Expected: `AttributeError: 'WorkspaceState' object has no attribute 'title'`.

- [ ] **Step 3: Add the field**

In `workspace/workspace.py`, inside `@dataclass class WorkspaceState`, add the line at the end of the field list (right before `def __post_init__`):

```python
    pending_review_comments: list[dict] | None = None
    review_cycle: int = 0
    title: str | None = None
```

- [ ] **Step 4: Run test, verify it passes**

```bash
.venv/bin/pytest tests/unit/test_workspace.py -v
```

Expected: all `TestWorkspaceState` tests pass, including the new one.

- [ ] **Step 5: Commit**

```bash
git add workspace/workspace.py tests/unit/test_workspace.py
git commit -m "feat(workspace): add title field to WorkspaceState"
```

---

## Task 2: `WorkspaceManager.create()` accepts and persists `title`

**Files:**
- Modify: `workspace/workspace_manager.py:50-145`
- Test: `tests/unit/test_workspace_manager.py` (add to `TestWorkspaceCreation`)

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_workspace_manager.py`, inside `class TestWorkspaceCreation`, add a new test. The class already mocks `subprocess.run` to fake the git clone, so reuse the same fixture pattern. Add this method:

```python
    def test_persists_title(self, mock_run, manager):
        # mock_run is set up by the class fixture to return success
        ws = manager.create(
            company_id="acme",
            repo_id="acme-app",
            ticket_id="ACME-1",
            clone_url="git@example.com:acme/acme-app.git",
            title="Login screen flickers on cold start",
        )
        # title is on the in-memory state
        assert ws.state.title == "Login screen flickers on cold start"

        # title is on disk in state.json
        import json
        state = json.loads((ws.root / "state.json").read_text())
        assert state["title"] == "Login screen flickers on cold start"
```

- [ ] **Step 2: Run, verify it fails**

```bash
.venv/bin/pytest tests/unit/test_workspace_manager.py::TestWorkspaceCreation::test_persists_title -v
```

Expected: `TypeError: create() got an unexpected keyword argument 'title'`.

- [ ] **Step 3: Update `create()` signature and the `WorkspaceState(...)` call**

In `workspace/workspace_manager.py`, change `def create` to accept `title`:

```python
    def create(
        self,
        company_id: str,
        repo_id: str,
        ticket_id: str,
        clone_url: str,
        clone_depth: int = 0,
        default_branch: str = "develop",
        branch_prefix: str = "feature",
        title: str | None = None,
    ) -> Workspace:
```

Then update the `WorkspaceState(...)` instantiation (around line 130) to pass `title`:

```python
            state = WorkspaceState(
                ticket_id=ticket_id,
                company_id=company_id,
                repo_id=repo_id,
                workspace_root=str(workspace_root),
                branch=branch_name,
                title=title,
            )
```

Update the docstring to mention the new parameter:

```python
            title: Ticket summary/title (e.g. "Login screen flickers...").
        """
```

(Add this line in the `Args:` block, between `branch_prefix` and `Returns:`.)

- [ ] **Step 4: Run, verify it passes**

```bash
.venv/bin/pytest tests/unit/test_workspace_manager.py -v
```

All tests pass — including the new `test_persists_title` and the existing `TestWorkspaceCreation` tests (which call `create()` without `title` — must still work because `title` defaults to `None`).

- [ ] **Step 5: Commit**

```bash
git add workspace/workspace_manager.py tests/unit/test_workspace_manager.py
git commit -m "feat(workspace): create() persists title to state.json"
```

---

## Task 3: Orchestrator passes ticket summary as title

**Files:**
- Modify: `orchestrator/orchestrator.py:538-550` (the `_create_workspace_for_ticket` call)

This task has no unit test — `_create_workspace_for_ticket` is an internal helper coupled to mock-heavy orchestrator fixtures, and a focused test would duplicate the WorkspaceManager test. The integration is verified end-to-end in Task 9 by checking that newly-polled tickets land with a `title` in `state.json`.

- [ ] **Step 1: Pass `title=pt.ticket.summary` into the `create()` call**

In `orchestrator/orchestrator.py`, modify the `_workspace_manager.create(...)` call inside `_create_workspace_for_ticket` (around line 538). Add the `title` argument at the end:

```python
        ws = self._workspace_manager.create(
            company_id=project_id,
            repo_id=pt.repo_id,
            ticket_id=pt.ticket.id,
            clone_url=repo_config.git.clone_url,
            clone_depth=repo_config.git.depth,
            default_branch=repo_config.vcs.github.default_branch
            if repo_config.vcs.provider == "github"
            else repo_config.vcs.gitlab.default_branch,
            branch_prefix=repo_config.vcs.github.branch_prefix
            if repo_config.vcs.provider == "github"
            else repo_config.vcs.gitlab.branch_prefix,
            title=pt.ticket.summary,
        )
```

- [ ] **Step 2: Run the orchestrator test suite to confirm nothing regressed**

```bash
.venv/bin/pytest tests/unit -k "orchestrator or workspace" -v
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/orchestrator.py
git commit -m "feat(orchestrator): pass ticket summary as workspace title"
```

---

## Task 4: Setup workspace state stores `title = "Workspace setup"`

**Files:**
- Modify: `dashboard/setup_workspace.py:25-66`
- Test: `tests/unit/test_setup_workspace.py:16-33`

- [ ] **Step 1: Add a failing assertion**

In `tests/unit/test_setup_workspace.py`, extend `test_create_setup_workspace_creates_tree` by adding one line at the end of the existing assertions:

```python
    assert state["repo_id"] == "acme-app"
    assert state["title"] == "Workspace setup"
```

- [ ] **Step 2: Run, verify it fails**

```bash
.venv/bin/pytest tests/unit/test_setup_workspace.py::test_create_setup_workspace_creates_tree -v
```

Expected: `KeyError: 'title'`.

- [ ] **Step 3: Add `"title": "Workspace setup"` to the state dict**

In `dashboard/setup_workspace.py`, modify the `state = {...}` block inside `create_setup_workspace` (around line 45):

```python
    state = {
        "ticket_id": "setup",
        "company_id": project_id,
        "repo_id": repo_id,
        "current_state": "SETUP_PENDING",
        "previous_state": None,
        "started_at": now,
        "last_updated_at": now,
        "kind": "setup",
        "title": "Workspace setup",
    }
```

- [ ] **Step 4: Run, verify it passes**

```bash
.venv/bin/pytest tests/unit/test_setup_workspace.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add dashboard/setup_workspace.py tests/unit/test_setup_workspace.py
git commit -m "feat(setup): tag setup workspace with static title"
```

---

## Task 5: Dashboard scan backfills missing title from `meta/ticket.md`

**Files:**
- Modify: `dashboard/web.py:77-119` (the `_scan_all_workspaces` function)
- Test: `tests/unit/test_dashboard_web.py` (new test class — see Step 1)

The backfill rule from the spec:

- If `state["title"]` is missing or empty AND `meta/ticket.md` exists → parse first line, strip leading `# `, drop the `TICKET-ID:` prefix if present, write the result back to `state.json`.
- If `state["title"]` is missing or empty AND the workspace dir is named `setup` → write `title = "Workspace setup"` (covers pre-Task-4 setup workspaces).
- Otherwise → leave empty.

- [ ] **Step 1: Write three failing tests**

Add a new test class to `tests/unit/test_dashboard_web.py` at the end of the file:

```python
import json
from pathlib import Path
from dashboard.web import _scan_all_workspaces


class TestTitleBackfill:
    def _write_state(self, root: Path, **fields):
        root.mkdir(parents=True, exist_ok=True)
        defaults = {
            "ticket_id": "T-1",
            "company_id": "acme",
            "repo_id": "acme-app",
            "current_state": "ANALYSIS",
            "started_at": "2026-04-27T00:00:00+00:00",
            "last_updated_at": "2026-04-27T00:00:00+00:00",
        }
        defaults.update(fields)
        (root / "state.json").write_text(json.dumps(defaults), encoding="utf-8")

    def test_backfills_from_ticket_md(self, tmp_path):
        ws = tmp_path / "acme" / "acme-app" / "tickets" / "T-1"
        self._write_state(ws)
        meta = ws / "meta"
        meta.mkdir()
        (meta / "ticket.md").write_text(
            "# T-1: Login screen flickers on cold start\n\n## Description\n",
            encoding="utf-8",
        )

        results = _scan_all_workspaces(str(tmp_path))

        assert results[0]["title"] == "Login screen flickers on cold start"
        # Disk was updated
        on_disk = json.loads((ws / "state.json").read_text())
        assert on_disk["title"] == "Login screen flickers on cold start"

    def test_backfill_handles_missing_id_prefix(self, tmp_path):
        ws = tmp_path / "acme" / "acme-app" / "tickets" / "T-2"
        self._write_state(ws, ticket_id="T-2")
        meta = ws / "meta"
        meta.mkdir()
        (meta / "ticket.md").write_text("# Just a plain title\n", encoding="utf-8")

        results = _scan_all_workspaces(str(tmp_path))
        assert results[0]["title"] == "Just a plain title"

    def test_backfill_setup_workspace(self, tmp_path):
        ws = tmp_path / "acme" / "acme-app" / "setup"
        self._write_state(ws, ticket_id="setup")
        # No meta/ticket.md

        results = _scan_all_workspaces(str(tmp_path))
        assert results[0]["title"] == "Workspace setup"
        on_disk = json.loads((ws / "state.json").read_text())
        assert on_disk["title"] == "Workspace setup"

    def test_backfill_skipped_when_title_present(self, tmp_path):
        ws = tmp_path / "acme" / "acme-app" / "tickets" / "T-3"
        self._write_state(ws, ticket_id="T-3", title="Already set")
        meta = ws / "meta"
        meta.mkdir()
        (meta / "ticket.md").write_text("# T-3: Different title\n", encoding="utf-8")

        results = _scan_all_workspaces(str(tmp_path))
        assert results[0]["title"] == "Already set"

    def test_backfill_no_meta_no_setup(self, tmp_path):
        ws = tmp_path / "acme" / "acme-app" / "tickets" / "T-4"
        self._write_state(ws, ticket_id="T-4")
        # No meta/ticket.md and not a setup dir

        results = _scan_all_workspaces(str(tmp_path))
        assert results[0]["title"] == ""
```

- [ ] **Step 2: Run, verify the tests fail**

```bash
.venv/bin/pytest tests/unit/test_dashboard_web.py::TestTitleBackfill -v
```

Expected: 5 failures — `KeyError: 'title'` or assertion mismatches.

- [ ] **Step 3: Add a backfill helper and call it from the scan**

In `dashboard/web.py`, add this helper function above `_scan_all_workspaces`:

```python
def _maybe_backfill_title(ws_root: Path, data: dict) -> str:
    """Return the title for this workspace, backfilling state.json if needed.

    Reads ``meta/ticket.md`` first line for ticket workspaces, or assigns the
    static "Workspace setup" title for the setup directory. The result is
    written back to ``state.json`` so the parse only happens once per workspace.
    """
    title = data.get("title") or ""
    if title:
        return title

    if ws_root.name == "setup":
        title = "Workspace setup"
    else:
        ticket_md = ws_root / "meta" / "ticket.md"
        if ticket_md.exists():
            try:
                first_line = ticket_md.read_text(encoding="utf-8").splitlines()[0]
            except (OSError, IndexError):
                first_line = ""
            # First line shape: "# TICKET-ID: Title" or "# Title"
            stripped = first_line.lstrip("# ").strip()
            ticket_id = data.get("ticket_id", "")
            if ticket_id and stripped.startswith(f"{ticket_id}:"):
                title = stripped[len(ticket_id) + 1:].strip()
            else:
                title = stripped

    if title:
        data["title"] = title
        try:
            (ws_root / "state.json").write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.warning("Failed to backfill title for %s: %s", ws_root, e)
    return title
```

Then in `_scan_all_workspaces`, call it after loading `data`. The current loop body is around line 86. Modify it so the title is computed and added to the result dict:

```python
    for state_file in base.rglob("state.json"):
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            ws_root = state_file.parent
            title = _maybe_backfill_title(ws_root, data)
            # List available reports
            reports_dir = ws_root / "reports"
            reports = sorted(f.name for f in reports_dir.iterdir() if f.is_file()) if reports_dir.exists() else []
            ...
            results.append({
                "ticket_id": data.get("ticket_id", ""),
                "company_id": data.get("company_id", ""),
                "repo_id": data.get("repo_id", ""),
                "current_state": data.get("current_state", "UNKNOWN"),
                "previous_state": data.get("previous_state"),
                "title": title,
                "branch": data.get("branch"),
                ...
```

(Insert `"title": title,` into the dict — exact placement: right before `"branch"`.)

- [ ] **Step 4: Run the new tests**

```bash
.venv/bin/pytest tests/unit/test_dashboard_web.py::TestTitleBackfill -v
```

Expected: all 5 pass.

- [ ] **Step 5: Run the full dashboard test file to confirm no regression**

```bash
.venv/bin/pytest tests/unit/test_dashboard_web.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add dashboard/web.py tests/unit/test_dashboard_web.py
git commit -m "feat(dashboard): backfill title from meta/ticket.md on scan"
```

---

## Task 6: Frontend — rewrite `renderCard()` for the new layout

**Files:**
- Modify: `dashboard/static/js/board.js:230-298` (the `renderCard` function)

This task changes only the HTML the card emits. The existing event-binding code at the top of `renderBoard` matches by `data-action` attributes — keep those attributes intact on the new icon buttons.

- [ ] **Step 1: Replace `renderCard()` with the new structure**

In `dashboard/static/js/board.js`, replace the entire `renderCard` function (and only that function — the rest of the file is untouched) with:

```javascript
function renderCard(ws) {
  const stateVal = ws.current_state || 'NEW';
  const dimmed = ['DONE', 'ARCHIVED', 'SETUP_DONE'].includes(stateVal);

  let cardClass = 'card';
  if (stateVal === 'BLOCKED' || stateVal === 'FAILED') cardClass += ' card-blocked';
  if (stateVal === 'AWAITING_APPROVAL') cardClass += ' card-awaiting';
  if (stateVal === 'MANUAL_CONTROL') cardClass += ' card-manual';
  if (stateVal === 'PAUSED') cardClass += ' card-paused';
  if (dimmed) cardClass += ' card-dimmed';

  // Line 1: ID + pause/resume + delete
  const PAUSEABLE_STATES = ['ANALYSIS', 'DEV', 'SCOPE_CHECK', 'QA', 'PUSHED', 'PR_REVIEW'];
  let pauseIcon = '';
  if (PAUSEABLE_STATES.includes(stateVal)) {
    pauseIcon = `<button class="card-icon-btn" data-action="pause" data-ticket="${esc(ws.ticket_id)}" title="Pause" onclick="event.stopPropagation()">⏸</button>`;
  } else if (stateVal === 'PAUSED') {
    pauseIcon = `<button class="card-icon-btn" data-action="unpause" data-ticket="${esc(ws.ticket_id)}" title="Resume" onclick="event.stopPropagation()">▶</button>`;
  }
  const deleteIcon = `<button class="card-icon-btn card-icon-delete" data-action="delete" data-ticket="${esc(ws.ticket_id)}" title="Delete" onclick="event.stopPropagation()">✕</button>`;

  // Line 3: error > manual-control note > omitted
  let noteHtml = '';
  if (ws.error) {
    noteHtml = `<div class="card-error" title="${esc(ws.error)}">${esc(ws.error)}</div>`;
  } else if (stateVal === 'MANUAL_CONTROL') {
    noteHtml = `<div class="card-manual-note">You have control</div>`;
  }

  // Line 4: meta — repo · time · iter
  const iters = ws.stage_iterations || {};
  const totalIters = Object.values(iters).reduce((a, b) => a + b, 0);
  const metaParts = [];
  if (ws.repo_id) metaParts.push(esc(ws.repo_id));
  metaParts.push(esc(timeAgo(ws.started_at)));
  if (totalIters > 0) metaParts.push(`iter ${totalIters}`);
  const metaHtml = `<div class="card-meta">${metaParts.join(' <span class="card-meta-sep">·</span> ')}</div>`;

  // Line 5: PR + contextual button (Approve / Clean) — omitted if both absent
  const prLink = ws.pr_url
    ? `<a class="card-pr-link" href="${esc(ws.pr_url)}" target="_blank" onclick="event.stopPropagation()">PR #${esc(String(ws.pr_number || ''))}</a>`
    : '';
  let contextualBtn = '';
  if (stateVal === 'AWAITING_APPROVAL') {
    contextualBtn = `<button class="action-btn btn-approve" data-action="approve" data-ticket="${esc(ws.ticket_id)}">Approve</button>`;
  } else if (dimmed && ws.workspace_root) {
    contextualBtn = `<button class="action-btn btn-clean" data-action="clean" data-ticket="${esc(ws.ticket_id)}" onclick="event.stopPropagation()" title="Remove source code to free disk space">Clean</button>`;
  }
  const footerHtml = (prLink || contextualBtn)
    ? `<div class="card-footer">
         ${prLink}
         <span class="card-footer-spacer"></span>
         ${contextualBtn}
       </div>`
    : '';

  return `<div class="${cardClass}" data-ticket="${esc(ws.ticket_id)}">
    <div class="card-line1">
      <span class="card-id">${esc(ws.ticket_id)}</span>
      <span class="card-line1-spacer"></span>
      ${pauseIcon}
      ${deleteIcon}
    </div>
    <div class="card-line2">
      <div class="card-title" title="${esc(ws.title || '')}">${esc(ws.title || '')}</div>
      ${stateBadgeHtml(stateVal)}
    </div>
    ${noteHtml}
    ${metaHtml}
    ${footerHtml}
  </div>`;
}
```

- [ ] **Step 2: Verify no other code in `board.js` references the removed class names**

```bash
cd /home/admin0/tot
grep -n "card-header\|card-repo\|card-ticket\|card-iter\|card-manual-label" dashboard/static/js/board.js
```

Expected: no results. (The selectors in the event-binding block use `data-action` attributes — those still exist on the new buttons.)

- [ ] **Step 3: Commit**

```bash
git add dashboard/static/js/board.js
git commit -m "refactor(board): rewrite renderCard for title-first layout"
```

---

## Task 7: Frontend — replace card CSS

**Files:**
- Modify: `dashboard/static/style.css:139-242` (and `:270-272` for the stripe modifiers, which stay)

This swaps the old card rules (line 146 onwards) for new ones aligned to the new HTML. The state-stripe modifiers (`.card-blocked`, `.card-awaiting`, `.card-paused`, `.card-manual`), state badges, `.card-dimmed`, and `.cards-grid` rule are unchanged. So is `.card:hover`.

- [ ] **Step 1: Replace the card CSS block**

In `dashboard/static/style.css`, find the block starting at `.cards-grid {` (around line 139) and ending right before `/* ── State badges ── */` (around line 244). Replace everything in that range with:

```css
  .cards-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 8px;
  }

  .card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 12px 14px;
    width: 300px;
    cursor: pointer;
    transition: border-color .15s, box-shadow .15s;
    position: relative;
  }
  .card:hover { border-color: #58a6ff; box-shadow: 0 0 0 1px #1f6feb33; }

  /* Line 1 — ID + icon controls */
  .card-line1 {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 10px;
  }
  .card-id {
    font-weight: 700;
    font-size: 13px;
    color: #e6edf3;
  }
  .card-line1-spacer { flex: 1; }

  /* Line 2 — title + state badge */
  .card-line2 {
    display: flex;
    gap: 8px;
    align-items: flex-start;
    margin-bottom: 10px;
  }
  .card-title {
    flex: 1;
    font-weight: 600;
    font-size: 14px;
    color: #e6edf3;
    line-height: 1.35;
    max-height: 2.7em;
    overflow: hidden;
    text-overflow: ellipsis;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    word-break: break-word;
  }
  .card-line2 .state-badge {
    flex-shrink: 0;
    align-self: flex-start;
    margin-top: 2px;
  }

  /* Line 3 — error / manual-control note (conditional) */
  .card-error {
    font-size: 11px;
    color: #f85149;
    line-height: 1.35;
    word-break: break-word;
    margin-bottom: 8px;
  }
  .card-manual-note {
    font-size: 11px;
    color: #d2a8ff;
    line-height: 1.35;
    margin-bottom: 8px;
  }

  /* Line 4 — meta */
  .card-meta {
    font-size: 11px;
    color: #8b949e;
    margin-bottom: 8px;
  }
  .card-meta-sep { color: #30363d; }

  /* Line 5 — footer (conditional) */
  .card-footer {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .card-footer-spacer { flex: 1; }
  .card-pr-link {
    font-size: 11px;
    color: #58a6ff;
    text-decoration: none;
  }
  .card-pr-link:hover { text-decoration: underline; }

  /* Icon buttons (line 1) */
  .card-icon-btn {
    width: 22px;
    height: 22px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: 1px solid #30363d;
    color: #8b949e;
    border-radius: 4px;
    font-size: 11px;
    cursor: pointer;
    padding: 0;
  }
  .card-icon-btn:hover { color: #e6edf3; border-color: #58a6ff; }
  .card-icon-delete:hover { border-color: #da3633; }

  /* Dimmed card (DONE / ARCHIVED / SETUP_DONE) */
  .card-dimmed { opacity: 0.45; }
  .card-dimmed:hover { opacity: 0.7; }
```

The `/* ── State badges ── */` block and everything below it stays as-is. Verify the next line after your replacement is still `/* ── State badges ── */`.

- [ ] **Step 2: Confirm `.btn-delete` removal didn't orphan anything**

The old CSS had a `.card .btn-delete` hover-fade rule. The new design always shows the delete icon, so that rule is intentionally gone. Confirm no other selector references it:

```bash
grep -n "btn-delete\|card-iter\|card-manual-label\|card-repo\|card-ticket\|card-header" dashboard/static/style.css
```

Expected: no results in the card region. (`.btn-delete` may still appear in detail-view CSS — that's fine, leave it.)

- [ ] **Step 3: Commit**

```bash
git add dashboard/static/style.css
git commit -m "refactor(board): card CSS for title-first layout"
```

---

## Task 8: Manual verification on dev server

This task has no automated test — it's the human-eyes pass that confirms the layout matches the spec across states. The brainstorming session signed off on the visual design; this verifies it landed correctly in real CSS/JS.

- [ ] **Step 1: Start the dev server**

If the dashboard daemon is already running locally, just hard-refresh the browser. Otherwise:

```bash
cd /home/admin0/tot
.venv/bin/python -m main --config config-live
```

The dashboard URL is logged to stdout on startup (`Dashboard: http://<host>:<port>`).

- [ ] **Step 2: Verify each state's card renders correctly**

Walk the board and confirm:

- [ ] An **ANALYSIS / DEV / SCOPE_CHECK / QA / PUSHED / PR_REVIEW** card shows: ID + ⏸ + ✕ on line 1, title + state badge on line 2, repo · time · iter on line 4, optional PR link on line 5.
- [ ] A **FAILED** or **BLOCKED** card shows red error text on line 3, no pause icon, red left stripe.
- [ ] An **AWAITING_APPROVAL** card shows the Approve button on line 5, yellow stripe, no pause icon.
- [ ] A **PAUSED** card shows ▶ resume icon on line 1, blue stripe, opacity ~0.85.
- [ ] A **MANUAL_CONTROL** card shows "You have control" on line 3 in purple, purple stripe, no pause icon.
- [ ] A **DONE / ARCHIVED / SETUP_DONE** card is dimmed; if `workspace_root` exists, the Clean button is on line 5.
- [ ] The **setup** workspace card shows "Workspace setup" on line 2 (verify after one dashboard load to trigger Task 5's backfill if it's an old setup workspace).
- [ ] An **old workspace that predates Task 1** initially has no title in `state.json`; after the first dashboard load, its title appears, and `state.json` on disk now has the `title` field (use `cat` to verify).
- [ ] **Hovering a long title** shows the full string in a tooltip; the visible text is clamped to 2 lines with ellipsis.
- [ ] **Hovering the delete icon** turns its border red; **hovering pause** turns the border blue.

- [ ] **Step 3: Verify Pause/Resume/Approve/Clean/Delete actions still work**

- [ ] Click ⏸ on a pauseable card → state changes to PAUSED, icon swaps to ▶.
- [ ] Click ▶ → state restores to previous, icon goes back to ⏸ (or hides if not pauseable).
- [ ] Click Approve on an AWAITING_APPROVAL card → state advances normally.
- [ ] Click Clean on a DONE card → workspace source/ removed, card stays DONE but loses the Clean button.
- [ ] Click ✕ → confirm dialog → workspace deleted, card disappears.

- [ ] **Step 4: If anything's wrong, fix it, then commit the fix as a follow-up**

```bash
git add <files>
git commit -m "fix(board): <what>"
```

If everything passes, no commit needed for this task — it's verification only.

---

## Self-Review

Spec coverage check:

| Spec section | Covered by |
|---|---|
| §Goal — title-first cards | Tasks 1, 6, 7 |
| §Decisions #1 (Layout direction) | Tasks 6, 7 |
| §Decisions #2 (Title in WorkspaceState) | Task 1 |
| §Decisions #3 (Backfill from meta/ticket.md) | Task 5 |
| §Decisions #4 (Setup title) | Task 4 + Task 5 fallback for old setup dirs |
| §Decisions #5 (Error on line 3) | Task 6 (`noteHtml`) + Task 7 (`.card-error`) |
| §Decisions #6 (Iter as plain text) | Task 6 (`metaParts`) — no chip class |
| §Decisions #7 (Pause/delete on line 1) | Task 6 + Task 7 (`.card-icon-btn`) |
| §Card Anatomy lines 1–5 | Task 6 HTML + Task 7 CSS |
| §State-Specific Behavior table | Task 6 (state branches) |
| §Data Model Changes — `title` field | Task 1 |
| §Data Model Changes — population | Tasks 2, 3, 4 |
| §Data Model Changes — backfill | Task 5 |
| §API surface — `title` in response | Task 5 (Step 3 dict insert) |
| §Frontend Changes — board.js | Task 6 |
| §Frontend Changes — style.css | Task 7 |
| §Verification | Task 8 |

No gaps. Type/method consistency: `_maybe_backfill_title(ws_root, data)` is referenced once, defined once. `title` parameter on `WorkspaceManager.create` matches `WorkspaceState.title` (both `str | None`). Frontend class names match between `board.js` and `style.css` (`card-line1`, `card-line2`, `card-title`, `card-meta`, `card-meta-sep`, `card-error`, `card-manual-note`, `card-footer`, `card-icon-btn`, `card-icon-delete`, `card-pr-link`).
