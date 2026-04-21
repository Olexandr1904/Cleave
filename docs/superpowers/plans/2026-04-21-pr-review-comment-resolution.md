# PR Review Comment Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automate PR review comment handling — classify with extreme skepticism, auto-fix trivial issues, reply on GitHub for won't-fix, escalate ambiguous to Telegram, loop until clean.

**Architecture:** A comment classifier (thin wrapper around the pr-comment-responder agent) classifies each PR comment. Auto-decisions execute immediately. Escalated comments go to TG with structured messages; user replies stored in `state.pending_review_comments`. When all decisions are in, fixes are applied, GitHub replies posted, resolution report written, and the pipeline loops back to PR_REVIEW if commits were pushed.

**Tech Stack:** Python 3.10+, asyncio, httpx (GitHub API), pytest, unittest.mock.

**Spec:** [docs/superpowers/specs/2026-04-21-pr-review-comment-resolution-design.md](../specs/2026-04-21-pr-review-comment-resolution-design.md)

---

## File Structure

**New files:**
- `orchestrator/comment_classifier.py` — sends comments to agent, parses structured JSON response
- `tests/unit/test_comment_classifier.py` — classifier unit tests
- `tests/unit/test_pr_review_flow.py` — full action flow tests

**Modified files:**
- `integrations/base/vcs.py` — add `resolve_comment` to interface
- `integrations/github/github_adapter.py` — implement `resolve_comment` via GraphQL
- `workspace/workspace.py` — add `pending_review_comments`, `review_cycle` fields to WorkspaceState
- `orchestrator/orchestrator.py` — rewrite `_action_fetch_pr_comments` with classify → handle → escalate → execute flow
- `agents/pr-comment-responder-agent.md` — rewrite with extreme skepticism rules + structured JSON output
- `integrations/telegram/command_handler.py` — handle replies to escalated comment messages

---

## Task 1: Add `resolve_comment` to VCS interface + GitHub adapter

**Files:**
- Modify: `integrations/base/vcs.py:54`
- Modify: `integrations/github/github_adapter.py:141`
- Test: `tests/unit/test_github_adapter.py` (or new `tests/unit/test_vcs_resolve.py`)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_vcs_resolve.py`:

```python
"""Tests for VCS resolve_comment."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from integrations.github.github_adapter import GitHubAdapter


@pytest.fixture
def adapter():
    return GitHubAdapter(token="test-token", owner="acme", repo="acme-app")


class TestResolveComment:
    @pytest.mark.asyncio
    async def test_resolve_calls_graphql(self, adapter):
        with patch.object(adapter, "_graphql_request", new=AsyncMock(return_value={"data": {"resolveReviewThread": {"thread": {"id": "T_123"}}}})) as mock_gql:
            await adapter.resolve_comment(42, 12345)
            mock_gql.assert_called_once()
            call_args = mock_gql.call_args
            assert "resolveReviewThread" in call_args[1].get("query", call_args[0][0] if call_args[0] else "")

    @pytest.mark.asyncio
    async def test_resolve_handles_already_resolved(self, adapter):
        with patch.object(adapter, "_graphql_request", new=AsyncMock(return_value={"data": {"resolveReviewThread": None}})):
            # Should not raise
            await adapter.resolve_comment(42, 12345)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_vcs_resolve.py -v`
Expected: FAIL — `resolve_comment` not implemented, `_graphql_request` not found

- [ ] **Step 3: Add `resolve_comment` to the VCS interface**

In `integrations/base/vcs.py`, after the existing `reply_to_comment` method (line 54), add:

```python
    @abstractmethod
    async def resolve_comment(self, pr_number: int, comment_id: int) -> None:
        """Mark a PR review comment thread as resolved."""
```

- [ ] **Step 4: Add `_graphql_request` helper to GitHubAdapter**

In `integrations/github/github_adapter.py`, add after `_request` method:

```python
    async def _graphql_request(self, query: str, variables: dict | None = None) -> dict:
        """Execute a GitHub GraphQL request."""
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        response = await self._client.post(
            "https://api.github.com/graphql",
            json=payload,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        response.raise_for_status()
        return response.json()
```

- [ ] **Step 5: Implement `resolve_comment` in GitHubAdapter**

GitHub's GraphQL API resolves review threads, not individual comments. We need to:
1. Find the thread ID for the comment
2. Resolve the thread

```python
    async def resolve_comment(self, pr_number: int, comment_id: int) -> None:
        """Resolve a PR review comment thread via GraphQL.

        GitHub resolves at the thread level. We query the thread ID
        from the comment's node_id, then resolve it.
        """
        # Step 1: Get the comment's node_id via REST
        data = await self._request(
            "GET",
            f"/repos/{self._owner}/{self._repo}/pulls/comments/{comment_id}",
        )
        node_id = data.get("node_id")
        if not node_id:
            logger.warning("No node_id for comment %d, cannot resolve", comment_id)
            return

        # Step 2: Get the review thread ID from the comment
        query = """
        query($nodeId: ID!) {
          node(id: $nodeId) {
            ... on PullRequestReviewComment {
              pullRequestReview {
                pullRequest {
                  reviewThreads(last: 100) {
                    nodes {
                      id
                      isResolved
                      comments(first: 1) {
                        nodes { id }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
        result = await self._graphql_request(query, {"nodeId": node_id})
        threads = (
            result.get("data", {}).get("node", {})
            .get("pullRequestReview", {}).get("pullRequest", {})
            .get("reviewThreads", {}).get("nodes", [])
        )
        thread_id = None
        for thread in threads:
            comment_nodes = thread.get("comments", {}).get("nodes", [])
            if any(c.get("id") == node_id for c in comment_nodes):
                thread_id = thread["id"]
                break

        if not thread_id:
            logger.warning("Could not find thread for comment %d", comment_id)
            return
        if threads and any(t.get("isResolved") for t in threads if t.get("id") == thread_id):
            return  # Already resolved

        # Step 3: Resolve the thread
        mutation = """
        mutation($threadId: ID!) {
          resolveReviewThread(input: {threadId: $threadId}) {
            thread { id isResolved }
          }
        }
        """
        await self._graphql_request(mutation, {"threadId": thread_id})
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/unit/test_vcs_resolve.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add integrations/base/vcs.py integrations/github/github_adapter.py tests/unit/test_vcs_resolve.py
git commit -m "feat(vcs): add resolve_comment via GitHub GraphQL API"
```

---

## Task 2: Add `pending_review_comments` and `review_cycle` to WorkspaceState

**Files:**
- Modify: `workspace/workspace.py:41-71`
- Test: `tests/unit/test_workspace.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_workspace.py`:

```python
class TestPendingReviewComments:
    def test_default_empty(self, tmp_path):
        ws = _make_workspace(tmp_path)
        assert ws.state.pending_review_comments is None
        assert ws.state.review_cycle == 0

    def test_persists_through_save_load(self, tmp_path):
        ws = _make_workspace(tmp_path)
        ws.state.pending_review_comments = [
            {"comment_id": 123, "msg_id": 456, "decision": None}
        ]
        ws.state.review_cycle = 2
        ws.save_state()

        ws2 = Workspace(str(tmp_path))
        assert ws2.state.pending_review_comments == [
            {"comment_id": 123, "msg_id": 456, "decision": None}
        ]
        assert ws2.state.review_cycle == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_workspace.py::TestPendingReviewComments -v`
Expected: FAIL — `pending_review_comments` not a field

- [ ] **Step 3: Add fields to WorkspaceState**

In `workspace/workspace.py`, add to the `WorkspaceState` dataclass:

```python
    pending_review_comments: list[dict] | None = None
    review_cycle: int = 0
```

- [ ] **Step 4: Run test**

Run: `pytest tests/unit/test_workspace.py::TestPendingReviewComments -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add workspace/workspace.py tests/unit/test_workspace.py
git commit -m "feat(workspace): add pending_review_comments and review_cycle state fields"
```

---

## Task 3: Create comment classifier module

**Files:**
- Create: `orchestrator/comment_classifier.py`
- Create: `tests/unit/test_comment_classifier.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_comment_classifier.py`:

```python
"""Tests for comment classification."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.comment_classifier import ClassifiedComment, parse_classifications


def test_parse_valid_json():
    raw = json.dumps([
        {
            "comment_id": 123,
            "classification": "AUTO_FIX",
            "reason": "Missing @PreviewAcme annotation",
            "suggested_fix": "Replace @Preview with @PreviewAcme on line 10",
        },
        {
            "comment_id": 456,
            "classification": "ESCALATE",
            "reason": "Reviewer suggests dimen resource — valid but adds scope",
            "suggested_fix": "",
        },
    ])
    results = parse_classifications(raw)
    assert len(results) == 2
    assert results[0].classification == "AUTO_FIX"
    assert results[0].comment_id == 123
    assert results[1].classification == "ESCALATE"


def test_parse_invalid_json_returns_all_escalate():
    results = parse_classifications("not json at all")
    assert results == []


def test_parse_missing_fields_defaults_to_escalate():
    raw = json.dumps([{"comment_id": 789}])
    results = parse_classifications(raw)
    assert len(results) == 1
    assert results[0].classification == "ESCALATE"
    assert results[0].reason == "classification missing"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_comment_classifier.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Create the module**

Create `orchestrator/comment_classifier.py`:

```python
"""Comment classification — sends PR comments to the responder agent for judgment."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

VALID_CLASSIFICATIONS = {"AUTO_FIX", "AUTO_REJECT", "ESCALATE"}


@dataclass
class ClassifiedComment:
    comment_id: int
    classification: str  # AUTO_FIX | AUTO_REJECT | ESCALATE
    reason: str
    suggested_fix: str = ""
    # Original comment data (filled by caller)
    author: str = ""
    file: str = ""
    line: int | None = None
    body: str = ""
    code_context: str = ""


def parse_classifications(raw_output: str) -> list[ClassifiedComment]:
    """Parse agent's structured JSON output into ClassifiedComment list.

    If parsing fails or fields are missing, defaults to ESCALATE.
    """
    try:
        data = json.loads(raw_output)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Failed to parse classification JSON")
        return []

    if not isinstance(data, list):
        logger.warning("Classification output is not a list")
        return []

    results = []
    for item in data:
        if not isinstance(item, dict):
            continue
        classification = item.get("classification", "ESCALATE")
        if classification not in VALID_CLASSIFICATIONS:
            classification = "ESCALATE"
        results.append(ClassifiedComment(
            comment_id=item.get("comment_id", 0),
            classification=classification,
            reason=item.get("reason", "classification missing"),
            suggested_fix=item.get("suggested_fix", ""),
        ))
    return results


async def classify_comments(
    comments: list[Any],
    workspace: Any,
    agent_runtime: Any,
) -> list[ClassifiedComment]:
    """Run the pr-comment-responder agent to classify PR comments.

    Returns ClassifiedComment list with original comment data attached.
    """
    # Build comment summary for the agent
    comment_data = []
    for c in comments:
        comment_data.append({
            "comment_id": c.id,
            "author": c.author,
            "file": c.path,
            "line": c.line,
            "body": c.body,
        })

    # Run the agent
    result = await agent_runtime.execute(
        agent_id="pr-comment-responder-agent",
        workspace=workspace,
        extra_context={"pr_comments_json": json.dumps(comment_data, indent=2)},
    )

    if not result.success:
        logger.error("PR comment responder failed: %s", result.error)
        # Default: escalate everything
        return [
            ClassifiedComment(
                comment_id=c.id, classification="ESCALATE",
                reason="Agent failed", author=c.author,
                file=c.path, line=c.line, body=c.body,
            )
            for c in comments
        ]

    classified = parse_classifications(result.output)

    # Attach original comment data
    comment_map = {c.id: c for c in comments}
    for cc in classified:
        orig = comment_map.get(cc.comment_id)
        if orig:
            cc.author = orig.author
            cc.file = orig.path
            cc.line = orig.line
            cc.body = orig.body

    return classified
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_comment_classifier.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/comment_classifier.py tests/unit/test_comment_classifier.py
git commit -m "feat(orchestrator): add comment classifier module"
```

---

## Task 4: Rewrite pr-comment-responder agent prompt

**Files:**
- Modify: `agents/pr-comment-responder-agent.md`

- [ ] **Step 1: Rewrite the agent prompt**

Replace the content of `agents/pr-comment-responder-agent.md`:

```markdown
---
agent:
  id: "pr-comment-responder-agent"
  name: "Rivera"
  title: "PR Review Analyst"
  model: ""

persona:
  role: "Code Review Skeptic"
  style: "Extremely skeptical, investigation-first"
  identity: "Assumes every PR comment may be wrong until proven otherwise"

core_principles:
  - "Be extremely skeptical (x100) — assume comments may be incorrect"
  - "Investigate thoroughly before classifying"
  - "Default to ESCALATE unless very confident"
  - "Never auto-fix anything that changes behavior"
  - "Never auto-reject valid feedback"

tools:
  - read_file
  - list_directory
  - search_code

inputs:
  - reports/pr-review-comments.md
  - reports/ba.md
  - meta/ticket.md
  - rules/arch-rules.md

outputs:
  - reports/pr-comments.md

decision_policy:
  when_to_run: "State is PR_REVIEW, after comments are fetched"
  max_iterations: 3
---

# PR Review Comment Classifier

## Your Task

You receive PR comments as JSON in the `{pr_comments_json}` context variable.
Classify each comment with EXTREME SKEPTICISM (x100).

## Process

For EACH comment:

1. **Read the comment** — understand what the reviewer is asking
2. **Investigate the codebase** — read the file, check the context, verify if the issue exists
3. **Classify** — based on your investigation, not assumptions

## Classification Rules

**AUTO_FIX** — use ONLY when ALL of these are true:
- The issue clearly exists (you verified it)
- The fix is trivial (naming, import, annotation swap)
- The fix does NOT change behavior
- The fix is within the ticket's scope

**AUTO_REJECT** — use ONLY when ALL of these are true:
- The suggestion is clearly about a different concern than the ticket
- Implementing it would change files not in the ticket scope
- It's a feature request, not a bug/quality fix

**ESCALATE** — use for EVERYTHING ELSE:
- You're not 100% sure
- The fix is non-trivial
- It might change behavior
- It's a matter of opinion/style
- The reviewer might be right but you need human judgment

**When in doubt → ESCALATE. Never guess.**

## Output Format

Return ONLY valid JSON array (no markdown, no code fences):

```json
[
  {
    "comment_id": 12345,
    "classification": "AUTO_FIX",
    "reason": "Project uses @PreviewAcme, this file has bare @Preview",
    "suggested_fix": "Replace @Preview with @PreviewAcme on line 10"
  },
  {
    "comment_id": 67890,
    "classification": "ESCALATE",
    "reason": "Reviewer suggests using dimen resource — valid convention but adds scope",
    "suggested_fix": ""
  }
]
```

## Constraints

- Return ONLY the JSON array — no explanations, no markdown wrapping
- Every comment must appear in the output
- `reason` must be under 200 characters
- `suggested_fix` must be empty for ESCALATE and AUTO_REJECT
- Do NOT modify any files — classification only
```

- [ ] **Step 2: Commit**

```bash
git add agents/pr-comment-responder-agent.md
git commit -m "feat(agents): rewrite pr-comment-responder with extreme skepticism classifier"
```

---

## Task 5: Rewrite `_action_fetch_pr_comments` with full flow

**Files:**
- Modify: `orchestrator/orchestrator.py:882-955`
- Test: `tests/unit/test_pr_review_flow.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_pr_review_flow.py`:

```python
"""Tests for the PR review comment resolution flow."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from orchestrator.stage_verifier import ActionResult
from orchestrator.comment_classifier import ClassifiedComment


def _fake_workspace(
    state: str = "PR_REVIEW",
    pr_number: int = 42,
    human_input_reply: str | None = None,
    pending_review_comments: list | None = None,
    review_cycle: int = 0,
) -> MagicMock:
    ws = MagicMock()
    ws.state = SimpleNamespace(
        ticket_id="T-1",
        company_id="acme",
        repo_id="acme-app",
        current_state=state,
        previous_state=None,
        branch="feature/t-1",
        pr_url="https://github.com/acme/app/pull/42",
        pr_number=pr_number,
        stage_iterations={"pr_review": 0},
        error=None,
        human_input_reply=human_input_reply,
        pending_review_comments=pending_review_comments,
        review_cycle=review_cycle,
        escalation_msg_id=None,
        escalation_chat_id=None,
        last_updated_at=None,
    )
    ws.source_dir = "/tmp/fake/source"
    ws.reports_dir = MagicMock()
    ws.reports_dir.__truediv__ = MagicMock(return_value=MagicMock())
    return ws


class TestPrReviewWaitForReviewed:
    @pytest.mark.asyncio
    async def test_skips_when_no_reviewed_signal(self):
        from orchestrator.orchestrator import Orchestrator
        orch = MagicMock(spec=Orchestrator)
        ws = _fake_workspace(human_input_reply=None)
        stage_def = SimpleNamespace(delay_minutes=0)

        result = await Orchestrator._action_fetch_pr_comments(orch, ws, stage_def)
        assert result.skipped is True


class TestPrReviewNoComments:
    @pytest.mark.asyncio
    async def test_returns_done_when_no_comments(self):
        from orchestrator.orchestrator import Orchestrator
        orch = MagicMock(spec=Orchestrator)
        orch._get_vcs_for_workspace = MagicMock(return_value=(MagicMock(), MagicMock()))
        vcs = orch._get_vcs_for_workspace.return_value[0]
        vcs.get_pr_comments = AsyncMock(return_value=[])

        ws = _fake_workspace(human_input_reply="reviewed")
        stage_def = SimpleNamespace(delay_minutes=0)

        result = await Orchestrator._action_fetch_pr_comments(orch, ws, stage_def)
        assert result.success is True
        assert result.next_state == "DONE"


class TestPrReviewAllAutoHandled:
    @pytest.mark.asyncio
    async def test_auto_fix_returns_pr_review_for_loop(self):
        from orchestrator.orchestrator import Orchestrator
        orch = MagicMock(spec=Orchestrator)
        orch._get_vcs_for_workspace = MagicMock(return_value=(MagicMock(), MagicMock()))
        orch._notifier = None
        orch._agent_runtime = MagicMock()
        vcs = orch._get_vcs_for_workspace.return_value[0]
        vcs.get_pr_comments = AsyncMock(return_value=[
            SimpleNamespace(id=1, author="bot", path="a.kt", line=10, body="fix typo"),
        ])
        vcs.resolve_comment = AsyncMock()

        classified = [ClassifiedComment(
            comment_id=1, classification="AUTO_FIX",
            reason="typo", suggested_fix="fix it",
            author="bot", file="a.kt", line=10, body="fix typo",
        )]

        ws = _fake_workspace(human_input_reply="reviewed")
        stage_def = SimpleNamespace(delay_minutes=0)

        with patch("orchestrator.orchestrator.classify_comments", new=AsyncMock(return_value=classified)):
            with patch("orchestrator.orchestrator._apply_fixes", new=AsyncMock(return_value=True)):
                result = await Orchestrator._action_fetch_pr_comments(orch, ws, stage_def)

        assert result.success is True
        assert result.next_state == "PR_REVIEW"


class TestPrReviewEscalated:
    @pytest.mark.asyncio
    async def test_escalated_returns_skipped(self):
        from orchestrator.orchestrator import Orchestrator
        orch = MagicMock(spec=Orchestrator)
        orch._get_vcs_for_workspace = MagicMock(return_value=(MagicMock(), MagicMock()))
        orch._notifier = AsyncMock()
        orch._notifier.send_message = AsyncMock(return_value=999)
        orch._get_chat_id = MagicMock(return_value="123")
        orch._agent_runtime = MagicMock()
        vcs = orch._get_vcs_for_workspace.return_value[0]
        vcs.get_pr_comments = AsyncMock(return_value=[
            SimpleNamespace(id=1, author="reviewer", path="a.kt", line=10, body="change this"),
        ])

        classified = [ClassifiedComment(
            comment_id=1, classification="ESCALATE",
            reason="ambiguous", author="reviewer",
            file="a.kt", line=10, body="change this",
        )]

        ws = _fake_workspace(human_input_reply="reviewed")
        stage_def = SimpleNamespace(delay_minutes=0)

        with patch("orchestrator.orchestrator.classify_comments", new=AsyncMock(return_value=classified)):
            result = await Orchestrator._action_fetch_pr_comments(orch, ws, stage_def)

        assert result.skipped is True
        assert ws.state.pending_review_comments is not None
        assert len(ws.state.pending_review_comments) == 1


class TestPrReviewDecisionsExecuted:
    @pytest.mark.asyncio
    async def test_all_decisions_in_executes(self):
        from orchestrator.orchestrator import Orchestrator
        orch = MagicMock(spec=Orchestrator)
        orch._get_vcs_for_workspace = MagicMock(return_value=(MagicMock(), MagicMock()))
        orch._notifier = None
        orch._agent_runtime = MagicMock()
        vcs = orch._get_vcs_for_workspace.return_value[0]
        vcs.reply_to_comment = AsyncMock()
        vcs.resolve_comment = AsyncMock()

        ws = _fake_workspace(
            human_input_reply=None,
            pending_review_comments=[
                {"comment_id": 1, "msg_id": 999, "decision": "won't fix: out of scope",
                 "author": "rev", "file": "a.kt", "line": 10, "body": "change this", "reason": "ambiguous"},
            ],
        )
        stage_def = SimpleNamespace(delay_minutes=0)

        with patch("orchestrator.orchestrator._write_resolution_report"):
            result = await Orchestrator._action_fetch_pr_comments(orch, ws, stage_def)

        assert result.success is True
        assert result.next_state == "DONE"
        vcs.reply_to_comment.assert_called_once()
        vcs.resolve_comment.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_pr_review_flow.py -v`
Expected: FAIL — new functions don't exist yet

- [ ] **Step 3: Rewrite `_action_fetch_pr_comments`**

Replace the method in `orchestrator/orchestrator.py`:

```python
    async def _action_fetch_pr_comments(
        self, workspace: Workspace, stage_def: Any,
    ) -> ActionResult:
        """PR review comment resolution flow."""
        from orchestrator.comment_classifier import classify_comments

        state = workspace.state
        pr_number = state.pr_number

        if not pr_number:
            return ActionResult(success=True, next_state="DONE", error="", metadata={})

        # Phase 1: Check if we have pending escalated decisions
        pending = state.pending_review_comments or []
        undecided = [c for c in pending if c.get("decision") is None]

        if pending and not undecided:
            # All decisions are in — execute them
            return await self._execute_review_decisions(workspace)

        if undecided:
            # Still waiting for TG replies
            return ActionResult(success=False, next_state="", error="", metadata={}, skipped=True)

        # Phase 2: Wait for 'reviewed' signal
        reply = (state.human_input_reply or "").lower()
        if "reviewed" not in reply and "proceed" not in reply:
            return ActionResult(success=False, next_state="", error="", metadata={}, skipped=True)

        # Clear signal, bump cycle
        state.human_input_reply = None
        state.review_cycle = (state.review_cycle or 0) + 1
        state.stage_iterations["pr_review"] = 0
        workspace.save_state()

        # Phase 3: Fetch comments
        vcs, repo_config = self._get_vcs_for_workspace(workspace)
        if not vcs:
            return ActionResult(success=True, next_state="DONE", error="", metadata={})

        try:
            comments = await vcs.get_pr_comments(pr_number)
        except Exception as e:
            logger.error("Failed to fetch PR comments for %s: %s", state.ticket_id, e)
            return ActionResult(success=False, next_state="", error=f"Failed to fetch PR comments: {e}", metadata={})

        if not comments:
            return ActionResult(success=True, next_state="DONE", error="", metadata={})

        # Write comments to reports for reference
        comment_md = "# PR Review Comments\n\n"
        for c in comments:
            comment_md += f"## Comment by {c.author}\n"
            if c.path:
                comment_md += f"File: `{c.path}`"
                if c.line:
                    comment_md += f" (line {c.line})"
                comment_md += "\n"
            comment_md += f"\n{c.body}\n\n---\n\n"
        (workspace.reports_dir / "pr-review-comments.md").write_text(comment_md, encoding="utf-8")

        # Phase 4: Classify
        classified = await classify_comments(comments, workspace, self._agent_runtime)

        # Phase 5: Auto-handle
        auto_fixed = []
        auto_rejected = []
        escalated = []

        for cc in classified:
            if cc.classification == "AUTO_FIX":
                auto_fixed.append(cc)
                try:
                    await vcs.resolve_comment(pr_number, cc.comment_id)
                except Exception as e:
                    logger.warning("Failed to resolve comment %d: %s", cc.comment_id, e)
            elif cc.classification == "AUTO_REJECT":
                try:
                    await vcs.reply_to_comment(pr_number, cc.comment_id, f"Won't fix: {cc.reason}")
                    await vcs.resolve_comment(pr_number, cc.comment_id)
                except Exception as e:
                    logger.warning("Failed to reply/resolve comment %d: %s", cc.comment_id, e)
                auto_rejected.append(cc)
            else:
                escalated.append(cc)

        # Phase 6: Send auto-handled summary to TG
        if (auto_fixed or auto_rejected) and self._notifier:
            chat_id = self._get_chat_id(workspace)
            if chat_id:
                sep = "─" * 30
                lines = [f"🤖 [{state.company_id}/{state.repo_id}] {state.ticket_id} — PR #{pr_number}"]
                lines.append(f"Auto-processed {len(auto_fixed) + len(auto_rejected)} comment(s):")
                lines.append(sep)
                for af in auto_fixed:
                    lines.append(f"✅ FIX: {af.reason} ({af.file}:{af.line or '?'})")
                for ar in auto_rejected:
                    lines.append(f"❌ REJECT: {ar.body[:60]} — {ar.reason}")
                    lines.append(f"   → replied on GitHub")
                lines.append(sep)
                if escalated:
                    lines.append(f"Waiting for your decisions on {len(escalated)} escalated comment(s).")
                await self._notifier.send_message(chat_id, "\n".join(lines))

        # Phase 7: Escalate remaining
        if not escalated:
            _write_resolution_report(workspace, classified, auto_fixed, auto_rejected, [], state.review_cycle)
            if auto_fixed:
                # Fixes were made (by the fix agent in a subsequent step) — loop back
                return ActionResult(success=True, next_state="PR_REVIEW", error="", metadata={})
            return ActionResult(success=True, next_state="DONE", error="", metadata={})

        # Store escalated with TG msg_ids
        pending_comments = []
        for cc in escalated:
            msg_id = await self._send_escalated_comment_tg(workspace, cc, pr_number)
            pending_comments.append({
                "comment_id": cc.comment_id,
                "msg_id": msg_id,
                "decision": None,
                "author": cc.author,
                "file": cc.file,
                "line": cc.line,
                "body": cc.body,
                "reason": cc.reason,
            })

        state.pending_review_comments = pending_comments
        # Also store auto-handled for the resolution report later
        state._auto_fixed = [{"comment_id": c.comment_id, "reason": c.reason, "file": c.file, "line": c.line} for c in auto_fixed]
        state._auto_rejected = [{"comment_id": c.comment_id, "reason": c.reason, "body": c.body} for c in auto_rejected]
        workspace.save_state()

        return ActionResult(success=False, next_state="", error="", metadata={}, skipped=True)

    async def _send_escalated_comment_tg(self, workspace: Workspace, cc: Any, pr_number: int) -> int:
        """Send a single escalated comment to TG. Returns the message ID."""
        state = workspace.state
        sep = "─" * 30
        msg = (
            f"💬 [{state.company_id}/{state.repo_id}] {state.ticket_id} — PR #{pr_number}\n"
            f"Comment by @{cc.author} on {cc.file}:{cc.line or '?'}\n"
            f"{sep}\n"
            f"Code context:\n  {cc.body[:200]}\n\n"
            f"Agent assessment:\n  {cc.reason}\n"
            f"{sep}\n"
            f"↩️ Reply: fix / skip / won't fix [reason]"
        )
        chat_id = self._get_chat_id(workspace)
        if chat_id and self._notifier:
            return await self._notifier.send_message(chat_id, msg)
        return 0

    async def _execute_review_decisions(self, workspace: Workspace) -> ActionResult:
        """Execute all pending review decisions (fix/skip/won't fix)."""
        state = workspace.state
        pending = state.pending_review_comments or []
        pr_number = state.pr_number

        vcs, _ = self._get_vcs_for_workspace(workspace)
        fixes_needed = []
        wont_fix = []
        skipped = []

        for c in pending:
            decision = (c.get("decision") or "").lower().strip()
            if decision == "fix":
                fixes_needed.append(c)
            elif decision.startswith("won't fix") or decision.startswith("wont fix"):
                reason = decision.split(":", 1)[1].strip() if ":" in decision else "Operator decision"
                wont_fix.append({**c, "wont_fix_reason": reason})
                if vcs and pr_number:
                    try:
                        await vcs.reply_to_comment(pr_number, c["comment_id"], f"Won't fix: {reason}")
                        await vcs.resolve_comment(pr_number, c["comment_id"])
                    except Exception as e:
                        logger.warning("Failed to reply/resolve %d: %s", c["comment_id"], e)
            else:
                skipped.append(c)

        # Apply fixes via dev-agent if needed
        has_fixes = bool(fixes_needed)
        if fixes_needed:
            # Write fix instructions for the dev agent
            fix_md = "# PR Comment Fixes Required\n\n"
            for f in fixes_needed:
                fix_md += f"## Fix: {f['file']}:{f.get('line', '?')}\n"
                fix_md += f"Comment by @{f['author']}: {f['body'][:200]}\n"
                fix_md += f"Reason: {f['reason']}\n\n"
            (workspace.reports_dir / "pr-comment-fixes.md").write_text(fix_md, encoding="utf-8")

        # Write resolution report
        auto_fixed = getattr(state, '_auto_fixed', [])
        auto_rejected = getattr(state, '_auto_rejected', [])
        _write_resolution_report(
            workspace, None, auto_fixed, auto_rejected,
            pending, state.review_cycle,
        )

        # Clear pending
        state.pending_review_comments = None
        workspace.save_state()

        if has_fixes:
            return ActionResult(success=True, next_state="DEV", error="", metadata={})
        return ActionResult(success=True, next_state="DONE", error="", metadata={})


def _write_resolution_report(
    workspace: Any,
    classified: list | None,
    auto_fixed: list,
    auto_rejected: list,
    escalated_decisions: list,
    cycle: int,
) -> None:
    """Write or append to reports/pr-review-resolution.md."""
    report_path = workspace.reports_dir / "pr-review-resolution.md"
    existing = report_path.read_text(encoding="utf-8") if report_path.exists() else ""

    lines = []
    if not existing:
        lines.append(f"# PR Review Resolution — {workspace.state.ticket_id}\n")
        lines.append(f"PR: #{workspace.state.pr_number}\n")

    total = len(auto_fixed) + len(auto_rejected) + len(escalated_decisions)
    lines.append(f"\n## Review Cycle {cycle}")
    lines.append(f"Comments this cycle: {total}\n")

    for af in auto_fixed:
        lines.append(f"### Comment #{af.get('comment_id', '?')} — AUTO_FIX")
        lines.append(f"File: {af.get('file', '?')}:{af.get('line', '?')}")
        lines.append(f"Reason: {af.get('reason', '')}")
        lines.append(f"Status: FIXED")
        lines.append(f"Mark as Resolved: YES\n")

    for ar in auto_rejected:
        lines.append(f"### Comment #{ar.get('comment_id', '?')} — AUTO_REJECT")
        lines.append(f"Reason: {ar.get('reason', '')}")
        lines.append(f"Status: WON'T_FIX")
        lines.append(f"GitHub reply: Posted")
        lines.append(f"Mark as Resolved: YES\n")

    for ed in escalated_decisions:
        decision = (ed.get("decision") or "skip").lower()
        status = "FIXED" if decision == "fix" else "WON'T_FIX" if "won't fix" in decision else "SKIPPED"
        lines.append(f"### Comment #{ed.get('comment_id', '?')} — ESCALATED")
        lines.append(f"File: {ed.get('file', '?')}:{ed.get('line', '?')}")
        lines.append(f"By: @{ed.get('author', '?')}")
        lines.append(f"Decision: {ed.get('decision', 'skip')}")
        lines.append(f"Status: {status}")
        commented = "YES" if "won't fix" in decision else "NO"
        lines.append(f"GitHub reply: {'Posted' if commented == 'YES' else 'N/A'}")
        lines.append(f"Mark as Resolved: {'YES' if status != 'SKIPPED' else 'NO'}\n")

    fixed = len(auto_fixed) + sum(1 for e in escalated_decisions if (e.get("decision") or "").lower() == "fix")
    wont_fix = len(auto_rejected) + sum(1 for e in escalated_decisions if "won't fix" in (e.get("decision") or "").lower())
    commented = wont_fix  # every won't-fix gets a GitHub reply
    skip = sum(1 for e in escalated_decisions if (e.get("decision") or "").lower() not in ("fix",) and "won't fix" not in (e.get("decision") or "").lower())

    lines.append(f"## Resolution Summary — Cycle {cycle}")
    lines.append(f"Fixed: {fixed} | Won't Fix: {wont_fix} | Commented: {commented} | Skipped: {skip}\n")

    report_path.write_text(existing + "\n".join(lines), encoding="utf-8")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_pr_review_flow.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run full test suite**

Run: `pytest tests/unit/ -v`
Expected: PASS — no regressions

- [ ] **Step 6: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_pr_review_flow.py
git commit -m "feat(orchestrator): rewrite PR review flow with classify → handle → escalate → execute"
```

---

## Task 6: Handle TG replies to escalated comments

**Files:**
- Modify: `integrations/telegram/command_handler.py`

- [ ] **Step 1: Update `handle_reply` to match escalated comment msg_ids**

In the `handle_reply` method, add a new branch before the existing BLOCKED check. After the `PR_REVIEW` branch (line ~458), add:

```python
            # Escalated PR comment: match by pending_review_comments msg_ids
            if ws.state.current_state == "PR_REVIEW" and ws.state.pending_review_comments:
                for c in ws.state.pending_review_comments:
                    if c.get("msg_id") == reply_to_msg_id:
                        c["decision"] = text.strip()
                        ws.save_state()
                        undecided = [x for x in ws.state.pending_review_comments if x.get("decision") is None]
                        if undecided:
                            await self._notifier.send_message(
                                chat_id,
                                f"Got it ({text.strip()}). {len(undecided)} comment(s) remaining.",
                            )
                        else:
                            await self._notifier.send_message(
                                chat_id,
                                f"All decisions in for {ws.state.ticket_id}. Executing now.",
                            )
                            if hasattr(self, '_wake_fn') and self._wake_fn:
                                self._wake_fn()
                        return True
```

- [ ] **Step 2: Commit**

```bash
git add integrations/telegram/command_handler.py
git commit -m "feat(telegram): handle replies to escalated PR review comments"
```

---

## Task 7: Update feature docs + final integration check

**Files:**
- Modify: `docs/features/orchestrator.md`
- Modify: `docs/features/index.md`

- [ ] **Step 1: Add changelog entry**

Append to `docs/features/orchestrator.md` changelog table:

```
| 2026-04-21 | Rewrote PR review flow: classify comments with extreme skepticism via pr-comment-responder agent, auto-fix trivial issues, reply on GitHub for won't-fix, escalate ambiguous to Telegram, resolution report, review cycle loop. |
```

- [ ] **Step 2: Run full test suite**

Run: `pytest -v`
Expected: ALL PASS

- [ ] **Step 3: Run linter**

Run: `ruff check orchestrator/comment_classifier.py orchestrator/orchestrator.py integrations/github/github_adapter.py`

- [ ] **Step 4: Commit**

```bash
git add docs/features/orchestrator.md docs/features/index.md
git commit -m "docs: PR review comment resolution feature"
```
