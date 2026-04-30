# Per-Ticket Model Selection via Jira Label — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a Jira ticket label (`model-haiku` / `model-opus` / `model-sonnet`) override the Claude model used for every agent dispatched against that ticket. Snapshot the resolution at workspace creation so it survives restart and mid-pipeline retry.

**Architecture:** A pure resolver maps a ticket's labels → a Claude model id. The orchestrator calls it once when creating a workspace, stores the result on `WorkspaceState.model` (persisted in `state.json`), and posts a Jira comment if the labels are ambiguous or unknown. `agent_runtime.execute()` prefers `workspace.state.model` over the existing per-agent / global-default fallback chain.

**Tech Stack:** Python 3.12, pytest, existing dataclass-based state (`workspace/workspace.py`), existing Jira adapter (`integrations/jira/jira_adapter.py`).

**Spec:** [`docs/superpowers/specs/2026-04-30-per-ticket-model-label-design.md`](../specs/2026-04-30-per-ticket-model-label-design.md)

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `orchestrator/model_resolver.py` | CREATE | Pure resolver: labels → `(model_id_or_none, warning_or_none)` |
| `tests/unit/test_model_resolver.py` | CREATE | Resolver unit tests |
| `workspace/workspace.py` | MODIFY (line 57+) | Add `model: str = ""` to `WorkspaceState` |
| `orchestrator/agent_runtime.py` | MODIFY (lines 213-217) | Prefer `workspace.state.model` over agent frontmatter |
| `tests/unit/test_agent_runtime.py` | MODIFY | Add cases for snapshot-wins-over-frontmatter |
| `orchestrator/orchestrator.py` | MODIFY (function `_create_workspace_for_ticket`, line 537+) | Call resolver, persist model, post warning comment |
| `tests/unit/test_orchestrator_model_label.py` | CREATE | Integration test for the wiring in `_create_workspace_for_ticket` |

---

## Task 1: Resolver — pure function

**Files:**
- Create: `orchestrator/model_resolver.py`
- Test:   `tests/unit/test_model_resolver.py`

- [ ] **Step 1.1: Write the failing tests**

Create `tests/unit/test_model_resolver.py`:

```python
"""Tests for orchestrator/model_resolver.py."""

from __future__ import annotations

import pytest

from orchestrator.model_resolver import (
    LABEL_PREFIX,
    SHORT_NAME_TO_MODEL,
    ResolutionResult,
    resolve_ticket_model,
)


def test_empty_labels_returns_none():
    result = resolve_ticket_model([])
    assert result == ResolutionResult(model=None, warning=None)


def test_no_model_label_returns_none():
    result = resolve_ticket_model(["ai-pipeline", "frontend", "bug"])
    assert result == ResolutionResult(model=None, warning=None)


def test_single_valid_label_haiku():
    result = resolve_ticket_model(["model-haiku"])
    assert result.model == SHORT_NAME_TO_MODEL["haiku"]
    assert result.warning is None


def test_single_valid_label_opus():
    result = resolve_ticket_model(["model-opus"])
    assert result.model == SHORT_NAME_TO_MODEL["opus"]
    assert result.warning is None


def test_single_valid_label_sonnet():
    result = resolve_ticket_model(["model-sonnet"])
    assert result.model == SHORT_NAME_TO_MODEL["sonnet"]
    assert result.warning is None


def test_valid_label_with_unrelated_labels():
    result = resolve_ticket_model(["ai-pipeline", "model-opus", "frontend"])
    assert result.model == SHORT_NAME_TO_MODEL["opus"]
    assert result.warning is None


def test_unknown_short_name_returns_warning():
    result = resolve_ticket_model(["model-llama"])
    assert result.model is None
    assert result.warning is not None
    assert "model-llama" in result.warning
    assert "model-haiku" in result.warning
    assert "model-opus" in result.warning
    assert "model-sonnet" in result.warning


def test_two_valid_labels_returns_warning():
    result = resolve_ticket_model(["model-opus", "model-haiku"])
    assert result.model is None
    assert result.warning is not None
    assert "model-opus" in result.warning
    assert "model-haiku" in result.warning


def test_valid_plus_unknown_treated_as_ambiguous():
    result = resolve_ticket_model(["model-opus", "model-llama"])
    assert result.model is None
    assert result.warning is not None
    assert "model-opus" in result.warning
    assert "model-llama" in result.warning


def test_short_name_case_insensitive():
    """`model-OPUS` matches `model-opus` (case-insensitive on the short name)."""
    result = resolve_ticket_model(["model-OPUS"])
    assert result.model == SHORT_NAME_TO_MODEL["opus"]
    assert result.warning is None


def test_prefix_must_be_lowercase():
    """`MODEL-opus` does NOT match — prefix must be lowercase."""
    result = resolve_ticket_model(["MODEL-opus"])
    assert result.model is None
    assert result.warning is None


def test_label_prefix_constant():
    """The prefix is `model-`, exposed as a module constant."""
    assert LABEL_PREFIX == "model-"


def test_short_name_map_covers_three_models():
    """The mapping covers exactly haiku, opus, sonnet."""
    assert set(SHORT_NAME_TO_MODEL.keys()) == {"haiku", "opus", "sonnet"}
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `pytest tests/unit/test_model_resolver.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.model_resolver'`

- [ ] **Step 1.3: Implement the resolver**

Create `orchestrator/model_resolver.py`:

```python
"""Resolve a Jira ticket's labels to a Claude model id.

Pure function module — no I/O, no side effects. Called once per workspace
at creation time; the result is snapshotted into WorkspaceState.model.

Keep SHORT_NAME_TO_MODEL in sync with dashboard/settings_store.ALLOWED_MODELS.
When a model version is bumped, update both lists together.
"""

from __future__ import annotations

from dataclasses import dataclass

LABEL_PREFIX = "model-"

SHORT_NAME_TO_MODEL: dict[str, str] = {
    "haiku":  "claude-haiku-4-5-20251001",
    "opus":   "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
}


@dataclass(frozen=True)
class ResolutionResult:
    """Outcome of resolving a ticket's labels.

    model    — full Claude model id, or None to use the existing fallback chain
    warning  — human-readable text to post as a Jira comment, or None
    """

    model: str | None
    warning: str | None


def resolve_ticket_model(labels: list[str]) -> ResolutionResult:
    """Inspect a ticket's labels and resolve to a Claude model id.

    Returns ResolutionResult(model=None, warning=None) when no model-* label
    is present. Returns (model_id, None) when exactly one valid label is
    present. Returns (None, warning_text) when labels are ambiguous or the
    short name is unknown.
    """
    model_labels = [lbl for lbl in labels if lbl.startswith(LABEL_PREFIX)]

    if not model_labels:
        return ResolutionResult(model=None, warning=None)

    if len(model_labels) > 1:
        return ResolutionResult(
            model=None,
            warning=(
                f"Multiple model labels found ({', '.join(f'`{l}`' for l in model_labels)}). "
                f"Falling back to global default. Please remove all but one."
            ),
        )

    # Exactly one model-* label
    label = model_labels[0]
    short_name = label[len(LABEL_PREFIX):].lower()

    if short_name not in SHORT_NAME_TO_MODEL:
        supported = ", ".join(f"`{LABEL_PREFIX}{n}`" for n in SHORT_NAME_TO_MODEL)
        return ResolutionResult(
            model=None,
            warning=(
                f"Unknown model label `{label}`. "
                f"Supported: {supported}. Falling back to global default."
            ),
        )

    return ResolutionResult(model=SHORT_NAME_TO_MODEL[short_name], warning=None)
```

- [ ] **Step 1.4: Run tests to verify they pass**

Run: `pytest tests/unit/test_model_resolver.py -v`
Expected: PASS — all 13 tests green.

- [ ] **Step 1.5: Commit**

```bash
git add orchestrator/model_resolver.py tests/unit/test_model_resolver.py
git commit -m "feat(orchestrator): add per-ticket model label resolver"
```

---

## Task 2: Add `model` field to `WorkspaceState`

**Files:**
- Modify: `workspace/workspace.py:82` (after `title: str | None = None`)

`WorkspaceState` is a dataclass at [workspace/workspace.py:57](../../workspace/workspace.py#L57). Existing `_load_state` already filters unknown fields, so adding a new field is backwards compatible — old `state.json` files load with the default `""`.

- [ ] **Step 2.1: Write the failing test**

Add to `tests/unit/test_agent_runtime.py` near the existing `WorkspaceState` import (top of file already imports it):

```python
def test_workspace_state_has_model_field_default_empty():
    """WorkspaceState exposes a `model` field defaulting to empty string.

    Empty string means: no per-ticket override; agent_runtime falls back to
    agent frontmatter / global default.
    """
    state = WorkspaceState(
        ticket_id="TEST-1",
        company_id="p",
        repo_id="r",
        workspace_root="/tmp/x",
    )
    assert state.model == ""


def test_workspace_state_model_field_settable():
    """The model field can be set to a Claude model id."""
    state = WorkspaceState(
        ticket_id="TEST-1",
        company_id="p",
        repo_id="r",
        workspace_root="/tmp/x",
        model="claude-opus-4-7",
    )
    assert state.model == "claude-opus-4-7"
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `pytest tests/unit/test_agent_runtime.py::test_workspace_state_has_model_field_default_empty tests/unit/test_agent_runtime.py::test_workspace_state_model_field_settable -v`
Expected: FAIL — `AttributeError: 'WorkspaceState' object has no attribute 'model'` or `TypeError: unexpected keyword argument 'model'`.

- [ ] **Step 2.3: Add the field**

In [workspace/workspace.py:82](../../workspace/workspace.py#L82), the current line reads:

```python
    title: str | None = None
```

Add a new line immediately after it:

```python
    title: str | None = None
    model: str = ""
```

So the dataclass fields end with:

```python
    review_cycle: int = 0
    title: str | None = None
    model: str = ""

    def __post_init__(self) -> None:
```

- [ ] **Step 2.4: Run the new tests + the full agent_runtime + workspace test files**

Run: `pytest tests/unit/test_agent_runtime.py tests/unit/test_admin_workspace.py -v`
Expected: PASS — both new tests green; no regressions in existing tests. Existing `state.json` files on disk continue to load (the `_load_state` filter on line 178-180 of `workspace.py` already handles unknown / missing fields).

- [ ] **Step 2.5: Commit**

```bash
git add workspace/workspace.py tests/unit/test_agent_runtime.py
git commit -m "feat(workspace): add model field to WorkspaceState"
```

---

## Task 3: Wire `agent_runtime.execute()` to prefer `state.model`

**Files:**
- Modify: `orchestrator/agent_runtime.py:213-217`
- Modify: `tests/unit/test_agent_runtime.py`

The current code in [agent_runtime.py:213-217](../../orchestrator/agent_runtime.py#L213-L217):

```python
        # Determine model
        model = ""
        agent_meta = agent.metadata.get("agent", {})
        if isinstance(agent_meta, dict):
            model = agent_meta.get("model", "")
```

This must be replaced so that `workspace.state.model` (the per-ticket snapshot) wins over the agent frontmatter. Empty string in `state.model` means "no per-ticket override".

- [ ] **Step 3.1: Write the failing tests**

Add to `tests/unit/test_agent_runtime.py` (use existing `registry`, `mock_llm`, `workspace` fixtures defined at the top of the file):

```python
class TestModelSelection:
    """Per-ticket snapshot wins over agent frontmatter wins over default."""

    @pytest.mark.asyncio
    async def test_state_model_overrides_agent_frontmatter(
        self, registry, mock_llm, tmp_path
    ):
        """When workspace.state.model is set, it overrides any agent pin."""
        ws_root = tmp_path / "snap-ws"
        ws_root.mkdir()
        (ws_root / "meta").mkdir()
        (ws_root / "logs").mkdir()
        (ws_root / "source" / "reports").mkdir(parents=True)
        state = WorkspaceState(
            ticket_id="TEST-99",
            company_id="p",
            repo_id="r",
            workspace_root=str(ws_root),
            model="claude-opus-4-7",
        )
        ws = Workspace(str(ws_root), state)
        ws.save_state()

        runtime = AgentRuntime(registry, mock_llm)
        # dev-agent's frontmatter currently has model: "" — we still want
        # to assert the snapshot is used regardless. Picking dev-agent
        # exercises the no-tools path most simply.
        await runtime.execute("dev-agent", ws)

        # The mock LLM's send_message was called with the snapshot model.
        assert mock_llm.send_message.called
        call_kwargs = mock_llm.send_message.call_args.kwargs
        assert call_kwargs.get("model") == "claude-opus-4-7"

    @pytest.mark.asyncio
    async def test_empty_state_model_falls_back_to_agent_frontmatter(
        self, registry, mock_llm, workspace
    ):
        """When state.model is empty, the existing fallback chain applies."""
        # workspace fixture has state.model defaulting to ""
        assert workspace.state.model == ""

        runtime = AgentRuntime(registry, mock_llm)
        await runtime.execute("dev-agent", workspace)

        # send_message was called; model param is "" (agent has no pin),
        # which means the adapter will use its own provider default.
        assert mock_llm.send_message.called
        call_kwargs = mock_llm.send_message.call_args.kwargs
        assert call_kwargs.get("model") == ""
```

Note: `agent_runtime.execute()` for an agent with no tools goes through `_execute_simple` which calls `self._llm.send_message(prompt, model=model, ...)`. The mock LLM's `send_message` is an `AsyncMock` (per the existing `mock_llm` fixture), so we can inspect `call_args`.

- [ ] **Step 3.2: Run tests to verify they fail**

Run: `pytest tests/unit/test_agent_runtime.py::TestModelSelection -v`
Expected: FAIL — first test asserts `model == "claude-opus-4-7"` but current code ignores `state.model`, so the call goes through with `model == ""` from agent frontmatter.

- [ ] **Step 3.3: Update model determination in `agent_runtime.execute()`**

In [orchestrator/agent_runtime.py](../../orchestrator/agent_runtime.py), replace lines 213-217. The current block:

```python
        # Determine model
        model = ""
        agent_meta = agent.metadata.get("agent", {})
        if isinstance(agent_meta, dict):
            model = agent_meta.get("model", "")
```

becomes:

```python
        # Determine model — per-ticket snapshot wins over agent frontmatter.
        # See docs/superpowers/specs/2026-04-30-per-ticket-model-label-design.md
        model = workspace.state.model or ""
        if not model:
            agent_meta = agent.metadata.get("agent", {})
            if isinstance(agent_meta, dict):
                model = agent_meta.get("model", "")
```

- [ ] **Step 3.4: Run tests to verify they pass**

Run: `pytest tests/unit/test_agent_runtime.py -v`
Expected: PASS — both new tests green; all existing tests still pass.

- [ ] **Step 3.5: Commit**

```bash
git add orchestrator/agent_runtime.py tests/unit/test_agent_runtime.py
git commit -m "feat(agent_runtime): prefer per-ticket model snapshot over agent frontmatter"
```

---

## Task 4: Wire `_create_workspace_for_ticket` to call resolver, persist model, post warning comment

**Files:**
- Modify: `orchestrator/orchestrator.py:537+` (`_create_workspace_for_ticket`)
- Create: `tests/unit/test_orchestrator_model_label.py`

The orchestrator method [`_create_workspace_for_ticket`](../../orchestrator/orchestrator.py#L537) creates a workspace and writes ticket metadata. After workspace creation, we call the resolver, persist the model into `state.json`, and post a Jira comment if there's a warning. Comment-post failures must NOT abort workspace creation — log and continue.

- [ ] **Step 4.1: Write the failing tests**

Create `tests/unit/test_orchestrator_model_label.py`:

```python
"""Tests for per-ticket model label wiring in _create_workspace_for_ticket.

These tests focus on the resolver call + state persistence + comment-post
behavior. They do not exercise the full polling pipeline — that's covered
by other orchestrator tests.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.model_resolver import SHORT_NAME_TO_MODEL
from workspace.workspace import Workspace, WorkspaceState


@pytest.fixture
def fake_ws(tmp_path):
    """Build a minimal Workspace on disk with the directories the orchestrator writes to."""
    root = tmp_path / "fake-ws"
    root.mkdir()
    (root / "meta").mkdir()  # _create_workspace_for_ticket writes ticket.md here
    state = WorkspaceState(
        ticket_id="TEST-1",
        company_id="p",
        repo_id="r",
        workspace_root=str(root),
    )
    ws = Workspace(str(root), state)
    ws.save_state()
    return ws


def _make_ticket(labels: list[str]) -> "TicketData":
    """Build a minimal TicketData with the given labels."""
    from integrations.base.tracker import TicketData
    return TicketData(
        id="TEST-1",
        url="https://jira.example.com/browse/TEST-1",
        summary="t",
        description="d",
        labels=labels,
    )


def _make_repo_config() -> MagicMock:
    """Build a MagicMock RepoConfig with the fields _create_workspace_for_ticket reads."""
    cfg = MagicMock()
    cfg.git.clone_url = "x"
    cfg.git.depth = 1
    cfg.vcs.provider = "github"
    cfg.vcs.github.default_branch = "main"
    cfg.vcs.github.branch_prefix = "ai/"
    return cfg


def _make_orchestrator(fake_ws, tracker_mock):
    """Build a real Orchestrator with the heavy deps stubbed.

    Bypasses __init__ and injects only the attributes _create_workspace_for_ticket
    accesses: _workspace_manager (whose .create returns our fake workspace) and
    _tracker (so we can assert add_comment was called).
    """
    from orchestrator.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._workspace_manager = MagicMock()
    orch._workspace_manager.create.return_value = fake_ws
    orch._tracker = tracker_mock
    orch._global_config = MagicMock()
    orch._emit = MagicMock()
    return orch


@pytest.mark.asyncio
async def test_valid_label_persists_model_and_no_comment(fake_ws):
    """Single valid label -> state.model is set, no comment posted."""
    from orchestrator.ticket_prioritizer import PrioritizedTicket

    tracker = AsyncMock()
    tracker._request = AsyncMock(side_effect=Exception("skip jira fetch"))
    tracker.add_comment = AsyncMock()

    orch = _make_orchestrator(fake_ws, tracker)
    pt = PrioritizedTicket(
        ticket=_make_ticket(["model-opus"]), repo_id="r", project_id="p",
    )

    await orch._create_workspace_for_ticket(pt, "p", _make_repo_config())

    # Reload state from disk to confirm persistence
    reloaded = Workspace(str(fake_ws.root))
    assert reloaded.state.model == SHORT_NAME_TO_MODEL["opus"]
    tracker.add_comment.assert_not_called()


@pytest.mark.asyncio
async def test_no_label_leaves_model_empty(fake_ws):
    """No model-* label -> state.model stays empty, no comment posted."""
    from orchestrator.ticket_prioritizer import PrioritizedTicket

    tracker = AsyncMock()
    tracker._request = AsyncMock(side_effect=Exception("skip jira fetch"))
    tracker.add_comment = AsyncMock()

    orch = _make_orchestrator(fake_ws, tracker)
    pt = PrioritizedTicket(
        ticket=_make_ticket(["ai-pipeline"]), repo_id="r", project_id="p",
    )

    await orch._create_workspace_for_ticket(pt, "p", _make_repo_config())

    reloaded = Workspace(str(fake_ws.root))
    assert reloaded.state.model == ""
    tracker.add_comment.assert_not_called()


@pytest.mark.asyncio
async def test_conflicting_labels_post_comment_and_leave_model_empty(fake_ws):
    """Two model-* labels -> state.model stays empty, comment posted once."""
    from orchestrator.ticket_prioritizer import PrioritizedTicket

    tracker = AsyncMock()
    tracker._request = AsyncMock(side_effect=Exception("skip jira fetch"))
    tracker.add_comment = AsyncMock()

    orch = _make_orchestrator(fake_ws, tracker)
    pt = PrioritizedTicket(
        ticket=_make_ticket(["model-opus", "model-haiku"]),
        repo_id="r", project_id="p",
    )

    await orch._create_workspace_for_ticket(pt, "p", _make_repo_config())

    reloaded = Workspace(str(fake_ws.root))
    assert reloaded.state.model == ""
    tracker.add_comment.assert_called_once()
    call_args = tracker.add_comment.call_args
    assert call_args.args[0] == "TEST-1"
    body = call_args.args[1]
    assert "model-opus" in body
    assert "model-haiku" in body


@pytest.mark.asyncio
async def test_comment_post_failure_does_not_abort_workspace_creation(fake_ws):
    """If add_comment raises, workspace creation still completes."""
    from orchestrator.ticket_prioritizer import PrioritizedTicket

    tracker = AsyncMock()
    tracker._request = AsyncMock(side_effect=Exception("skip jira fetch"))
    tracker.add_comment = AsyncMock(side_effect=RuntimeError("Jira down"))

    orch = _make_orchestrator(fake_ws, tracker)
    pt = PrioritizedTicket(
        ticket=_make_ticket(["model-llama"]),  # unknown -> warning -> add_comment raises
        repo_id="r", project_id="p",
    )

    # Should not raise
    ws = await orch._create_workspace_for_ticket(pt, "p", _make_repo_config())
    assert ws is fake_ws
    tracker.add_comment.assert_called_once()
```

- [ ] **Step 4.2: Run tests to verify they fail**

Run: `pytest tests/unit/test_orchestrator_model_label.py -v`
Expected: FAIL — `state.model` stays `""` even for the valid-label case (resolver isn't called yet); `add_comment` isn't called for the conflict case.

- [ ] **Step 4.3: Add the resolver call to `_create_workspace_for_ticket`**

In [orchestrator/orchestrator.py](../../orchestrator/orchestrator.py), the `_create_workspace_for_ticket` method begins at line 537. After the existing `ws = self._workspace_manager.create(...)` block (which currently ends near line 559 with `title=pt.ticket.summary,)` and before the `# Write ticket data as markdown` line, add:

```python
        # Per-ticket model label resolution (see docs/.../2026-04-30-per-ticket-model-label-design.md)
        from orchestrator.model_resolver import resolve_ticket_model
        resolution = resolve_ticket_model(pt.ticket.labels)
        if resolution.model:
            ws.state.model = resolution.model
            ws.save_state()
        if resolution.warning and self._tracker is not None:
            try:
                await self._tracker.add_comment(pt.ticket.id, resolution.warning)
            except Exception as e:
                logger.warning(
                    "Failed to post model-label warning to %s: %s",
                    pt.ticket.id, e,
                )
```

The exact insertion point is right after this existing block (around line 559):

```python
        ws = self._workspace_manager.create(
            ...
            title=pt.ticket.summary,
        )
        # <-- INSERT THE NEW BLOCK HERE -->

        # Write ticket data as markdown
        ticket_md = _ticket_to_markdown(pt.ticket)
```

The `from orchestrator.model_resolver import resolve_ticket_model` import can also be hoisted to the top of the file alongside other `from orchestrator.*` imports (~line 26 area) — either is fine; do whichever matches existing import style. Hoisting is preferred:

At the top of the file, find the existing block of imports near `from orchestrator.agent_runtime import AgentRuntime` (line 26). Add a new import line:

```python
from orchestrator.model_resolver import resolve_ticket_model
```

Then in `_create_workspace_for_ticket`, drop the inline `from orchestrator.model_resolver import resolve_ticket_model` line.

- [ ] **Step 4.4: Run tests to verify they pass**

Run: `pytest tests/unit/test_orchestrator_model_label.py -v`
Expected: PASS — all four tests green.

- [ ] **Step 4.5: Run the full test suite**

Run: `pytest tests/unit -v --tb=short`
Expected: PASS — no regressions in any existing test. The new field `model` defaults to `""` and is filtered out of unknown old fields by `_load_state`, so existing fixtures are unaffected.

- [ ] **Step 4.6: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_orchestrator_model_label.py
git commit -m "feat(orchestrator): resolve and snapshot per-ticket model label at workspace creation"
```

---

## Final verification

- [ ] **Step 5.1: Run the full unit test suite once more**

Run: `pytest tests/unit -q`
Expected: all tests pass.

- [ ] **Step 5.2: Manual smoke check (optional, no automation)**

Inspect: a fresh workspace's `state.json` should contain `"model": ""` by default, and `"model": "claude-opus-4-7"` when the source ticket carried `model-opus`. No code change — just `cat workspace/.../state.json` after running the pipeline against a labeled ticket.

- [ ] **Step 5.3: Confirm spec coverage**

Walk through [the spec](../specs/2026-04-30-per-ticket-model-label-design.md) and tick each item:
- [ ] Resolver maps labels → model id (Task 1)
- [ ] `model-` prefix, lowercase short names (Task 1)
- [ ] No label → fall back (Task 1)
- [ ] Multiple labels → warning (Task 1)
- [ ] Unknown short name → warning (Task 1)
- [ ] Case-insensitive short name match (Task 1)
- [ ] `WorkspaceState.model` field added (Task 2)
- [ ] `state.json` persistence is automatic via `asdict` (Task 2 — verified by Task 4 reload test)
- [ ] `agent_runtime` prefers `state.model` over agent frontmatter (Task 3)
- [ ] Empty `state.model` falls through to existing chain (Task 3)
- [ ] `_create_workspace_for_ticket` calls resolver and persists (Task 4)
- [ ] Warning posted as Jira comment on conflict / unknown (Task 4)
- [ ] Comment-post failure non-fatal (Task 4)
- [ ] Backwards compatible with existing `state.json` files (Task 2 — `_load_state` filter)
