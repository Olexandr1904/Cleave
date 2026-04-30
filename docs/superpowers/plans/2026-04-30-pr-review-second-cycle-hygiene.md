# PR Review Second-Cycle Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pick up reviewer thread replies on the next cycle, route them through classification with full conversation context, auto-resolve acknowledgments, skip GitHub-resolved threads, and self-heal stuck re-investigation flags.

**Architecture:** Add a `thread_ids` chain to each resolution-report entry that records the parent comment plus every reply (ours and theirs) we've seen. On each cycle, set-subtract stored chain from the live thread to find new replies. Pass full thread as context to the classifier. Threads marked resolved on GitHub are skipped via a new GraphQL field on `get_pr_comments`.

**Tech Stack:** Python 3.10+, asyncio, pytest, pytest-asyncio.

**Spec:** [docs/superpowers/specs/2026-04-30-pr-review-second-cycle-hygiene-design.md](../specs/2026-04-30-pr-review-second-cycle-hygiene-design.md)

---

## File Structure

**New files:**
- `tests/unit/test_thread_replies.py` — unit tests for thread grouping, context building, and per-thread classification
- `tests/unit/test_pr_review_second_cycle.py` — integration test for the full 2-cycle flow

**Modified files:**
- `integrations/base/vcs.py` — `PRComment` gets `is_thread_resolved`
- `integrations/github/github_adapter.py` — `get_pr_comments` runs GraphQL for `isResolved`
- `orchestrator/comment_classifier.py` — `classify_comments` accepts `thread_context`, helper `_render_thread_context_block`
- `orchestrator/resolution_report.py` — module-level helpers `format_thread_ids`/`parse_thread_ids`
- `orchestrator/orchestrator.py` — Phase 4 split into Steps A–E, helpers `_group_comments_into_threads` and `_build_thread_context`, ack short-circuit, Filter B regex update, `_reinvestigate_pending` defensive flag clear
- `agents/pr-comment-responder-agent.md` — Thread Context section
- Existing test extensions: `test_comment_classifier.py`, `test_review_decisions_freetext.py`

---

## Task 1: `PRComment.is_thread_resolved` field

**Files:**
- Modify: `integrations/base/vcs.py`
- Modify: `integrations/github/github_adapter.py:163-178` (preserve default `False` for now)

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_thread_replies.py` (create the file):

```python
"""Tests for PR-review second-cycle hygiene: thread replies, isResolved, defenses."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestPRCommentIsThreadResolved:
    def test_default_is_false(self):
        from integrations.base.vcs import PRComment

        c = PRComment(id=1, body="x")
        assert c.is_thread_resolved is False

    def test_can_be_set_true(self):
        from integrations.base.vcs import PRComment

        c = PRComment(id=1, body="x", is_thread_resolved=True)
        assert c.is_thread_resolved is True
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_thread_replies.py::TestPRCommentIsThreadResolved -v`
Expected: FAIL — field doesn't exist.

- [ ] **Step 3: Add the field**

Edit `integrations/base/vcs.py`. Currently `PRComment` is:

```python
@dataclass
class PRComment:
    """A review comment on a pull request."""
    id: int
    body: str
    path: str = ""
    line: int | None = None
    author: str = ""
    in_reply_to_id: int | None = None
```

Add `is_thread_resolved: bool = False`:

```python
@dataclass
class PRComment:
    """A review comment on a pull request."""
    id: int
    body: str
    path: str = ""
    line: int | None = None
    author: str = ""
    in_reply_to_id: int | None = None
    is_thread_resolved: bool = False
```

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/unit/test_thread_replies.py -v`
Expected: PASS — 2 tests.

- [ ] **Step 5: Confirm github_adapter still compiles**

Run: `python -c "from integrations.github.github_adapter import GitHubAdapter"`
Expected: no errors. The adapter constructs `PRComment` without the new field; default `False` makes this a no-op.

- [ ] **Step 6: Commit**

```bash
git add integrations/base/vcs.py tests/unit/test_thread_replies.py
git commit -m "feat(pr-review): add is_thread_resolved to PRComment"
```

---

## Task 2: `thread_ids` format helpers in `resolution_report`

**Files:**
- Modify: `orchestrator/resolution_report.py`
- Test: `tests/unit/test_resolution_report.py` (extend existing)

- [ ] **Step 1: Write failing tests**

Add to the end of `tests/unit/test_resolution_report.py`:

```python
class TestThreadIdsFormat:
    def test_format_empty_list(self):
        from orchestrator.resolution_report import format_thread_ids

        assert format_thread_ids([]) == ""

    def test_format_single_id(self):
        from orchestrator.resolution_report import format_thread_ids

        assert format_thread_ids([12345]) == "12345"

    def test_format_chain(self):
        from orchestrator.resolution_report import format_thread_ids

        assert format_thread_ids([12345, 67890, 67891]) == "12345,67890,67891"

    def test_parse_empty_string(self):
        from orchestrator.resolution_report import parse_thread_ids

        assert parse_thread_ids("") == []

    def test_parse_single(self):
        from orchestrator.resolution_report import parse_thread_ids

        assert parse_thread_ids("12345") == [12345]

    def test_parse_chain(self):
        from orchestrator.resolution_report import parse_thread_ids

        assert parse_thread_ids("12345,67890,67891") == [12345, 67890, 67891]

    def test_parse_handles_whitespace(self):
        from orchestrator.resolution_report import parse_thread_ids

        assert parse_thread_ids("12345, 67890 ,67891") == [12345, 67890, 67891]

    def test_parse_skips_empty_segments(self):
        from orchestrator.resolution_report import parse_thread_ids

        assert parse_thread_ids("12345,,67890,") == [12345, 67890]

    def test_round_trip(self):
        from orchestrator.resolution_report import format_thread_ids, parse_thread_ids

        ids = [100, 200, 300]
        assert parse_thread_ids(format_thread_ids(ids)) == ids
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_resolution_report.py::TestThreadIdsFormat -v`
Expected: FAIL — helpers don't exist.

- [ ] **Step 3: Implement the helpers**

In `orchestrator/resolution_report.py`, add at module scope (after the imports, before `read_entries`):

```python
def format_thread_ids(ids: list[int]) -> str:
    """Serialize a thread-IDs chain for the resolution report."""
    return ",".join(str(i) for i in ids)


def parse_thread_ids(value: str) -> list[int]:
    """Parse a `Thread Ids` field back to a list of int."""
    if not value:
        return []
    out: list[int] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError:
            continue
    return out
```

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/unit/test_resolution_report.py -v`
Expected: PASS — all tests including new TestThreadIdsFormat.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/resolution_report.py tests/unit/test_resolution_report.py
git commit -m "feat(pr-review): add thread_ids format/parse helpers"
```

---

## Task 3: `classify_comments` accepts `thread_context`

**Files:**
- Modify: `orchestrator/comment_classifier.py`
- Modify: `tests/unit/test_comment_classifier.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_comment_classifier.py`:

```python
class TestThreadContext:
    @pytest.mark.asyncio
    async def test_thread_context_passed_through_to_runtime(self):
        from orchestrator.comment_classifier import classify_comments

        runtime = MagicMock()
        result = MagicMock()
        result.success = True
        result.output = '[{"comment_id": 1, "classification": "ESCALATE", "verdict": "Valid", "reason": "ok"}]'
        runtime.execute = AsyncMock(return_value=result)

        ws = MagicMock()
        comments = [SimpleNamespace(id=1, author="C", path="x.kt", line=1, body="b")]

        thread_ctx = {1: [
            {"author": "C", "body": "fix this", "posted_at": "2026-04-30 09:15"},
            {"author": "bot", "body": "Will fix: ok", "posted_at": "2026-04-30 09:20"},
            {"author": "C", "body": "wait, also handle null", "posted_at": "2026-04-30 11:42"},
        ]}

        await classify_comments(comments, ws, runtime, thread_context=thread_ctx)

        ctx = runtime.execute.call_args.kwargs["extra_context"]
        block = ctx["thread_context_block"]
        assert "fix this" in block
        assert "Will fix: ok" in block
        assert "wait, also handle null" in block
        assert "@C" in block
        assert "@bot" in block

    @pytest.mark.asyncio
    async def test_default_thread_context_block_is_empty_string(self):
        from orchestrator.comment_classifier import classify_comments

        runtime = MagicMock()
        result = MagicMock()
        result.success = True
        result.output = "[]"
        runtime.execute = AsyncMock(return_value=result)
        ws = MagicMock()

        await classify_comments([], ws, runtime)

        ctx = runtime.execute.call_args.kwargs["extra_context"]
        assert ctx["thread_context_block"] == ""

    @pytest.mark.asyncio
    async def test_thread_context_block_only_for_comments_with_context(self):
        from orchestrator.comment_classifier import classify_comments

        runtime = MagicMock()
        result = MagicMock()
        result.success = True
        result.output = '[{"comment_id": 1, "classification": "ESCALATE", "verdict": "Valid", "reason": "ok"}]'
        runtime.execute = AsyncMock(return_value=result)

        ws = MagicMock()
        # Two comments — only one has context
        comments = [
            SimpleNamespace(id=1, author="C", path="x.kt", line=1, body="b"),
            SimpleNamespace(id=2, author="C", path="y.kt", line=1, body="b"),
        ]
        thread_ctx = {1: [
            {"author": "C", "body": "look here", "posted_at": "2026-04-30 09:15"},
        ]}

        await classify_comments(comments, ws, runtime, thread_context=thread_ctx)

        block = runtime.execute.call_args.kwargs["extra_context"]["thread_context_block"]
        assert "## Prior conversation for comment 1" in block
        assert "## Prior conversation for comment 2" not in block
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_comment_classifier.py::TestThreadContext -v`
Expected: FAIL — kwarg not supported.

- [ ] **Step 3: Implement the helper + signature update**

Edit `orchestrator/comment_classifier.py`. Add a module-level helper before `classify_comments`:

```python
def _render_thread_context_block(
    thread_context: dict[int, list[dict]] | None,
    comments: list[Any],
) -> str:
    """Render the per-comment thread-context block for the agent prompt.

    Empty if thread_context is None/empty or no comment has context.
    """
    if not thread_context:
        return ""
    out_parts: list[str] = []
    comment_ids = {c.id for c in comments}
    for cid in sorted(thread_context.keys()):
        if cid not in comment_ids:
            continue
        entries = thread_context.get(cid) or []
        if not entries:
            continue
        out_parts.append(f"## Prior conversation for comment {cid}\n")
        for e in entries:
            author = e.get("author", "?")
            body = (e.get("body", "") or "").strip()
            posted = e.get("posted_at", "")
            out_parts.append(f"@{author} ({posted}): {body}\n")
        out_parts.append("\n")
    return "".join(out_parts).strip()
```

Update `classify_comments` signature and `extra_context`:

```python
async def classify_comments(
    comments: list[Any],
    workspace: Any,
    agent_runtime: Any,
    *,
    operator_hint: str = "",
    thread_context: dict[int, list[dict]] | None = None,
) -> list[ClassifiedComment]:
    ...
    result = await agent_runtime.execute(
        agent_id="pr-comment-responder-agent",
        workspace=workspace,
        extra_context={
            "pr_comments_json": json.dumps(comment_data, indent=2),
            "operator_hint": operator_hint,
            "thread_context_block": _render_thread_context_block(thread_context, comments),
        },
    )
    ...
```

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/unit/test_comment_classifier.py -v`
Expected: PASS — all classifier tests.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/comment_classifier.py tests/unit/test_comment_classifier.py
git commit -m "feat(pr-review): classify_comments accepts thread_context"
```

---

## Task 4: Agent prompt — Thread Context section

**Files:**
- Modify: `agents/pr-comment-responder-agent.md`

This task is content-only.

- [ ] **Step 1: Read the existing agent prompt**

Run: `cat agents/pr-comment-responder-agent.md`

Locate the `## Operator Hint` section (added in the prior PR-review work). The new section goes immediately after it.

- [ ] **Step 2: Insert the Thread Context section**

After the `## Operator Hint` block (and its trailing operator-hint placeholder line), insert:

```markdown
## Thread Context

If `{thread_context_block}` is non-empty, the comment you're classifying has prior replies. Read the full conversation. Decide based on the *latest* concern the reviewer raised — earlier replies are context, not the issue to resolve.

If the latest reply is acknowledgment ("ok thanks", "lgtm", "great", "👍", or similar), classify as `AUTO_REJECT` with `reason: "Reviewer acknowledged — no action needed"`. The pipeline detects this exact reason and posts a polite "Acknowledged." reply (not "Won't fix:") on GitHub before resolving.

Thread context block:
{thread_context_block}
```

The standalone `{thread_context_block}` line on its own ensures the block degrades to a clean blank line when empty.

- [ ] **Step 3: Commit**

```bash
git add agents/pr-comment-responder-agent.md
git commit -m "feat(pr-review): agent prompt — Thread Context + acknowledgment rule"
```

---

## Task 5: `github_adapter.get_pr_comments` fetches `isResolved`

**Files:**
- Modify: `integrations/github/github_adapter.py:163-178`
- Modify: `tests/unit/test_thread_replies.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_thread_replies.py`:

```python
class TestGetPRCommentsResolvedFlag:
    @pytest.mark.asyncio
    async def test_populates_is_thread_resolved_from_graphql(self):
        from integrations.github.github_adapter import GitHubAdapter

        adapter = GitHubAdapter.__new__(GitHubAdapter)
        adapter._owner = "acme"
        adapter._name = "app"
        adapter._repo_path = "repos/acme/app"
        adapter._token = "tok"
        adapter._client = MagicMock()

        rest_response = [
            {"id": 100, "body": "fix this", "path": "x.kt", "line": 5,
             "user": {"login": "C"}, "in_reply_to_id": None},
            {"id": 101, "body": "Will fix: ok", "path": "x.kt", "line": 5,
             "user": {"login": "bot"}, "in_reply_to_id": 100},
            {"id": 200, "body": "another concern", "path": "y.kt", "line": 5,
             "user": {"login": "C"}, "in_reply_to_id": None},
        ]
        graphql_response = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
            {"isResolved": True, "comments": {"nodes": [{"databaseId": 100}, {"databaseId": 101}]}},
            {"isResolved": False, "comments": {"nodes": [{"databaseId": 200}]}},
        ]}}}}}

        adapter._request = AsyncMock(return_value=rest_response)
        adapter._graphql_request = AsyncMock(return_value=graphql_response)

        comments = await adapter.get_pr_comments(42)

        by_id = {c.id: c for c in comments}
        assert by_id[100].is_thread_resolved is True
        assert by_id[101].is_thread_resolved is True
        assert by_id[200].is_thread_resolved is False

    @pytest.mark.asyncio
    async def test_graphql_failure_falls_back_to_unresolved(self):
        """When GraphQL fails, all comments default to is_thread_resolved=False."""
        from integrations.github.github_adapter import GitHubAdapter

        adapter = GitHubAdapter.__new__(GitHubAdapter)
        adapter._owner = "acme"
        adapter._name = "app"
        adapter._repo_path = "repos/acme/app"
        adapter._token = "tok"
        adapter._client = MagicMock()

        rest_response = [
            {"id": 100, "body": "x", "path": "x.kt", "line": 5,
             "user": {"login": "C"}, "in_reply_to_id": None},
        ]
        adapter._request = AsyncMock(return_value=rest_response)
        adapter._graphql_request = AsyncMock(side_effect=RuntimeError("rate limit"))

        comments = await adapter.get_pr_comments(42)

        assert len(comments) == 1
        assert comments[0].is_thread_resolved is False
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_thread_replies.py::TestGetPRCommentsResolvedFlag -v`
Expected: FAIL — `is_thread_resolved` is always default False (Task 1's default behavior, not populated from GraphQL).

- [ ] **Step 3: Update `get_pr_comments`**

Edit `integrations/github/github_adapter.py`. Replace the existing `get_pr_comments`:

```python
async def get_pr_comments(self, pr_number: int) -> list[PRComment]:
    """Get all review comments on a PR, including each comment's thread-resolved status."""
    data = await self._request(
        "GET", f"{self._repo_path}/pulls/{pr_number}/comments"
    )

    # Build comment_id → is_thread_resolved map via GraphQL.
    # On failure, all comments default to is_thread_resolved=False.
    resolved_map: dict[int, bool] = {}
    query = """
    query($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
          reviewThreads(first: 100) {
            nodes {
              isResolved
              comments(first: 50) { nodes { databaseId } }
            }
          }
        }
      }
    }
    """
    try:
        gql = await self._graphql_request(query, {
            "owner": self._owner, "name": self._name, "number": pr_number,
        })
        threads = (
            gql.get("data", {}).get("repository", {})
            .get("pullRequest", {}).get("reviewThreads", {}).get("nodes", [])
        )
        for thread in threads:
            is_resolved = bool(thread.get("isResolved"))
            for cm in (thread.get("comments", {}) or {}).get("nodes", []) or []:
                cid = cm.get("databaseId")
                if cid is not None:
                    resolved_map[cid] = is_resolved
    except Exception as e:
        logger.warning("GraphQL isResolved fetch failed for PR #%d: %s", pr_number, e)

    return [
        PRComment(
            id=c["id"],
            body=c.get("body", ""),
            path=c.get("path", ""),
            line=c.get("line"),
            author=c.get("user", {}).get("login", ""),
            in_reply_to_id=c.get("in_reply_to_id"),
            is_thread_resolved=resolved_map.get(c["id"], False),
        )
        for c in data
    ]
```

The adapter's `_owner` and `_name` are existing private fields — verify with `grep -n "self._owner\|self._name" integrations/github/github_adapter.py | head`. If they're named differently in your codebase, adapt the GraphQL variables accordingly.

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/unit/test_thread_replies.py -v`
Expected: PASS — TestPRCommentIsThreadResolved + TestGetPRCommentsResolvedFlag.

- [ ] **Step 5: Commit**

```bash
git add integrations/github/github_adapter.py tests/unit/test_thread_replies.py
git commit -m "feat(pr-review): get_pr_comments populates is_thread_resolved via GraphQL"
```

---

## Task 6: Helper `_group_comments_into_threads`

**Files:**
- Modify: `orchestrator/orchestrator.py`
- Modify: `tests/unit/test_thread_replies.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_thread_replies.py`:

```python
class TestGroupCommentsIntoThreads:
    def test_parent_without_replies(self):
        from orchestrator.orchestrator import Orchestrator
        from integrations.base.vcs import PRComment

        comments = [PRComment(id=100, body="x", in_reply_to_id=None)]
        threads = Orchestrator._group_comments_into_threads(comments)
        assert threads == {100: [comments[0]]}

    def test_parent_with_replies(self):
        from orchestrator.orchestrator import Orchestrator
        from integrations.base.vcs import PRComment

        parent = PRComment(id=100, body="parent", in_reply_to_id=None)
        r1 = PRComment(id=101, body="reply 1", in_reply_to_id=100)
        r2 = PRComment(id=102, body="reply 2", in_reply_to_id=100)
        comments = [parent, r1, r2]
        threads = Orchestrator._group_comments_into_threads(comments)
        assert threads == {100: [parent, r1, r2]}

    def test_multiple_threads(self):
        from orchestrator.orchestrator import Orchestrator
        from integrations.base.vcs import PRComment

        p1 = PRComment(id=100, body="p1", in_reply_to_id=None)
        r1 = PRComment(id=101, body="r1", in_reply_to_id=100)
        p2 = PRComment(id=200, body="p2", in_reply_to_id=None)
        comments = [p1, r1, p2]
        threads = Orchestrator._group_comments_into_threads(comments)
        assert threads == {100: [p1, r1], 200: [p2]}

    def test_orphan_reply_discarded_with_warning(self, caplog):
        from orchestrator.orchestrator import Orchestrator
        from integrations.base.vcs import PRComment

        p1 = PRComment(id=100, body="p1", in_reply_to_id=None)
        orphan = PRComment(id=999, body="orphan", in_reply_to_id=42)  # parent 42 missing
        with caplog.at_level("WARNING"):
            threads = Orchestrator._group_comments_into_threads([p1, orphan])
        assert threads == {100: [p1]}
        assert any("orphan" in rec.message.lower() or "missing" in rec.message.lower()
                   or "999" in rec.message for rec in caplog.records)
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_thread_replies.py::TestGroupCommentsIntoThreads -v`
Expected: FAIL — `_group_comments_into_threads` doesn't exist.

- [ ] **Step 3: Add the helper to Orchestrator**

In `orchestrator/orchestrator.py`, add a `@staticmethod` near the other static helpers (look for `_git_diff_files`, `_git_head_sha` around line 305):

```python
@staticmethod
def _group_comments_into_threads(
    comments: list[Any],
) -> dict[int, list[Any]]:
    """Group flat comment list into per-thread lists keyed by parent id.

    Orphan replies (parent missing) are logged as warnings and discarded.
    Per-thread order matches input order: parent first, then replies as encountered.
    """
    by_parent: dict[int, list[Any]] = {c.id: [c] for c in comments if c.in_reply_to_id is None}
    for c in comments:
        if c.in_reply_to_id is None:
            continue
        parent_id = c.in_reply_to_id
        if parent_id not in by_parent:
            logger.warning(
                "Orphan reply %s (parent %s missing) — discarded",
                c.id, parent_id,
            )
            continue
        by_parent[parent_id].append(c)
    return by_parent
```

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/unit/test_thread_replies.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_thread_replies.py
git commit -m "feat(pr-review): _group_comments_into_threads helper"
```

---

## Task 7: Helper `_build_thread_context`

**Files:**
- Modify: `orchestrator/orchestrator.py`
- Modify: `tests/unit/test_thread_replies.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_thread_replies.py`:

```python
class TestBuildThreadContext:
    def test_single_parent_no_replies(self):
        from orchestrator.orchestrator import Orchestrator
        from integrations.base.vcs import PRComment

        thread = [PRComment(id=100, body="parent body", author="C")]
        ctx = Orchestrator._build_thread_context(thread)
        assert ctx == [{"author": "C", "body": "parent body", "posted_at": ""}]

    def test_parent_with_bot_and_reviewer_replies(self):
        from orchestrator.orchestrator import Orchestrator
        from integrations.base.vcs import PRComment

        thread = [
            PRComment(id=100, body="fix this", author="reviewer"),
            PRComment(id=101, body="Will fix: ok", author="bot"),
            PRComment(id=102, body="also handle null", author="reviewer"),
        ]
        ctx = Orchestrator._build_thread_context(thread)
        assert len(ctx) == 3
        assert ctx[0]["author"] == "reviewer"
        assert ctx[1]["author"] == "bot"
        assert ctx[1]["body"] == "Will fix: ok"
        assert ctx[2]["body"] == "also handle null"

    def test_includes_bot_replies(self):
        """Spec: include bot replies so the agent sees prior commitment."""
        from orchestrator.orchestrator import Orchestrator
        from integrations.base.vcs import PRComment

        thread = [
            PRComment(id=100, body="fix this", author="reviewer"),
            PRComment(id=101, body="Will fix: ok", author="bot"),
        ]
        ctx = Orchestrator._build_thread_context(thread)
        bot_entries = [e for e in ctx if e["author"] == "bot"]
        assert len(bot_entries) == 1
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_thread_replies.py::TestBuildThreadContext -v`
Expected: FAIL.

- [ ] **Step 3: Add the helper**

In `orchestrator/orchestrator.py`, near `_group_comments_into_threads`:

```python
@staticmethod
def _build_thread_context(thread: list[Any]) -> list[dict]:
    """Build the {author, body, posted_at} list for a thread.

    Includes the bot's own replies so the agent sees prior commitment.
    posted_at is currently empty (PRComment doesn't carry created_at);
    the agent uses chronological order via the list order itself.
    """
    return [
        {
            "author": c.author or "",
            "body": c.body or "",
            "posted_at": "",  # PRComment doesn't carry timestamps yet
        }
        for c in thread
    ]
```

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/unit/test_thread_replies.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_thread_replies.py
git commit -m "feat(pr-review): _build_thread_context helper"
```

---

## Task 8: Phase 4 Step A (isResolved skip) + Filter B regex update

**Files:**
- Modify: `orchestrator/orchestrator.py:1707-1716` (current Filter B + filter loop)
- Modify: `tests/unit/test_thread_replies.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_thread_replies.py`:

```python
class TestFilterBRegex:
    def test_will_fix_recognized_as_bot_reply(self):
        """The bot's 'Will fix:' reply should mark its parent as bot-replied."""
        # We test the regex directly via the filter logic in _action_fetch_pr_comments.
        # The simplest approach: import the constant and check with str.startswith.
        from orchestrator.orchestrator import _BOT_REPLY_PREFIXES

        assert "will fix" in _BOT_REPLY_PREFIXES
        assert "won't fix" in _BOT_REPLY_PREFIXES
        assert "wont fix" in _BOT_REPLY_PREFIXES
        assert "fixed" in _BOT_REPLY_PREFIXES
        assert "acknowledged" in _BOT_REPLY_PREFIXES
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_thread_replies.py::TestFilterBRegex -v`
Expected: FAIL — `_BOT_REPLY_PREFIXES` doesn't exist as a module-level constant.

- [ ] **Step 3: Extract Filter B prefixes to module-level constant + add new entries**

In `orchestrator/orchestrator.py`, add at module scope (near other constants, around top of file):

```python
_BOT_REPLY_PREFIXES = (
    "won't fix", "wont fix", "will fix", "fixed", "acknowledged",
)
```

Then update the existing Filter B usage (around line 1707-1710 in `_action_fetch_pr_comments`):

```python
replied_to_ids = set()
for c in all_comments:
    if c.in_reply_to_id and c.body.strip().lower().startswith(_BOT_REPLY_PREFIXES):
        replied_to_ids.add(c.in_reply_to_id)
```

- [ ] **Step 4: Add Step A — isResolved skip**

In `_action_fetch_pr_comments`, between the comment fetch and the existing Filter A/B/C, add Step A. Find the line that fetches `all_comments = await vcs.get_pr_comments(pr_number)` (around line 1701) and add the filter just before the existing filters:

```python
all_comments = await vcs.get_pr_comments(pr_number)

# Phase 4 Step A: drop comments belonging to threads the reviewer manually
# resolved on GitHub. The graphql lookup in get_pr_comments populates
# is_thread_resolved per comment.
all_comments = [c for c in all_comments if not c.is_thread_resolved]
```

- [ ] **Step 5: Test the isResolved filter**

Add to `tests/unit/test_thread_replies.py`:

```python
class TestStepAResolvedFilter:
    @pytest.mark.asyncio
    async def test_resolved_thread_comments_dropped(self, tmp_path, monkeypatch):
        from orchestrator.orchestrator import Orchestrator
        from integrations.base.vcs import PRComment

        orch = Orchestrator.__new__(Orchestrator)
        orch._notifier = MagicMock()
        orch._notifier.send_message = AsyncMock(return_value=999)
        orch._events = None
        orch._get_chat_id = MagicMock(return_value="chat-1")
        orch._get_ticket_title = MagicMock(return_value="Ticket")
        orch._tg_header = MagicMock(return_value="hdr")
        orch._agent_runtime = MagicMock()
        orch._log_pipeline = MagicMock()
        orch._now = MagicMock(return_value="2026-04-30T10:00:00Z")
        orch._git_diff_files = MagicMock(return_value=set())
        orch._git_head_sha = MagicMock(return_value="abcd1234")

        vcs = MagicMock()
        vcs.reply_to_comment = AsyncMock()
        vcs.resolve_comment = AsyncMock()
        vcs.get_pr_comments = AsyncMock(return_value=[
            PRComment(id=100, body="resolved-thread comment", author="C",
                      path="x.kt", line=1, in_reply_to_id=None,
                      is_thread_resolved=True),
            PRComment(id=200, body="open-thread comment", author="C",
                      path="y.kt", line=1, in_reply_to_id=None,
                      is_thread_resolved=False),
        ])
        orch._get_vcs_for_workspace = MagicMock(return_value=(vcs, None))

        ws = MagicMock()
        ws.reports_dir = tmp_path
        ws.state = SimpleNamespace(
            ticket_id="T-1", company_id="acme", repo_id="app",
            pr_number=42, current_state="PR_REVIEW",
            pending_review_comments=[], review_cycle=0,
            stage_iterations={}, human_input_reply="reviewed",
            last_verified_sha="",
        )
        ws.save_state = MagicMock()

        # Force classify_comments to return ESCALATE for whatever it gets
        from orchestrator.comment_classifier import ClassifiedComment
        async def fake_classify(comments, _ws, _runtime, *, operator_hint="", thread_context=None):
            return [
                ClassifiedComment(
                    comment_id=c.id, classification="ESCALATE", verdict="Valid",
                    reason="r", author=c.author, file=c.path, line=c.line, body=c.body,
                ) for c in comments
            ]
        import orchestrator.orchestrator as omod
        monkeypatch.setattr(omod, "classify_comments", fake_classify, raising=False)

        await orch._action_fetch_pr_comments(ws, stage_def=None)

        # Only comment 200 (unresolved thread) should have been classified;
        # comment 100 was filtered out by Step A.
        # Check pending_review_comments only has the 200 entry.
        pending = ws.state.pending_review_comments or []
        assert any(p.get("comment_id") == 200 for p in pending)
        assert not any(p.get("comment_id") == 100 for p in pending)
```

- [ ] **Step 6: Run — must pass**

Run: `pytest tests/unit/test_thread_replies.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_thread_replies.py
git commit -m "feat(pr-review): Step A drops resolved threads + Filter B prefixes constant"
```

---

## Task 9: Phase 4 Step B (split into new-parents vs threads-with-new-replies)

**Files:**
- Modify: `orchestrator/orchestrator.py:1701-1730` (Phase 4 fetch + filter region)
- Modify: `tests/unit/test_thread_replies.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_thread_replies.py`:

```python
class TestStepBSplitBuckets:
    @pytest.mark.asyncio
    async def test_new_parents_routed_to_classify(self, tmp_path, monkeypatch):
        """A new parent (no entry in resolution report) goes to standard classify."""
        from orchestrator.orchestrator import Orchestrator
        from integrations.base.vcs import PRComment

        orch = Orchestrator.__new__(Orchestrator)
        orch._notifier = MagicMock()
        orch._notifier.send_message = AsyncMock(return_value=999)
        orch._events = None
        orch._get_chat_id = MagicMock(return_value="chat-1")
        orch._get_ticket_title = MagicMock(return_value="Ticket")
        orch._tg_header = MagicMock(return_value="hdr")
        orch._agent_runtime = MagicMock()
        orch._log_pipeline = MagicMock()
        orch._now = MagicMock(return_value="2026-04-30T10:00:00Z")
        orch._git_diff_files = MagicMock(return_value=set())
        orch._git_head_sha = MagicMock(return_value="abcd1234")

        vcs = MagicMock()
        vcs.reply_to_comment = AsyncMock()
        vcs.resolve_comment = AsyncMock()
        vcs.get_pr_comments = AsyncMock(return_value=[
            PRComment(id=100, body="new comment", author="C",
                      path="x.kt", line=1, in_reply_to_id=None),
        ])
        orch._get_vcs_for_workspace = MagicMock(return_value=(vcs, None))

        ws = MagicMock()
        ws.reports_dir = tmp_path
        ws.state = SimpleNamespace(
            ticket_id="T-1", company_id="acme", repo_id="app",
            pr_number=42, current_state="PR_REVIEW",
            pending_review_comments=[], review_cycle=0,
            stage_iterations={}, human_input_reply="reviewed",
            last_verified_sha="",
        )
        ws.save_state = MagicMock()

        from orchestrator.comment_classifier import ClassifiedComment
        captured_thread_context = []
        async def fake_classify(comments, _ws, _runtime, *, operator_hint="", thread_context=None):
            captured_thread_context.append(thread_context)
            return [
                ClassifiedComment(
                    comment_id=c.id, classification="ESCALATE", verdict="Valid",
                    reason="r", author=c.author, file=c.path, line=c.line, body=c.body,
                ) for c in comments
            ]
        import orchestrator.orchestrator as omod
        monkeypatch.setattr(omod, "classify_comments", fake_classify, raising=False)

        await orch._action_fetch_pr_comments(ws, stage_def=None)

        # New parent → no thread_context
        assert captured_thread_context == [None] or captured_thread_context == [{}]

    @pytest.mark.asyncio
    async def test_existing_parent_with_new_reply_routed_to_thread_classify(
        self, tmp_path, monkeypatch
    ):
        """An existing parent (in resolution report) with a new reply goes
        through thread classification with thread_context."""
        from orchestrator.orchestrator import Orchestrator
        from integrations.base.vcs import PRComment
        from orchestrator.resolution_report import add_entry

        orch = Orchestrator.__new__(Orchestrator)
        orch._notifier = MagicMock()
        orch._notifier.send_message = AsyncMock(return_value=999)
        orch._events = None
        orch._get_chat_id = MagicMock(return_value="chat-1")
        orch._get_ticket_title = MagicMock(return_value="Ticket")
        orch._tg_header = MagicMock(return_value="hdr")
        orch._agent_runtime = MagicMock()
        orch._log_pipeline = MagicMock()
        orch._now = MagicMock(return_value="2026-04-30T10:00:00Z")
        orch._git_diff_files = MagicMock(return_value=set())
        orch._git_head_sha = MagicMock(return_value="abcd1234")

        # Pre-seed resolution report: parent 100 already classified, thread_ids=[100, 101]
        report_path = tmp_path / "pr-review-resolution.md"
        add_entry(report_path, "T-1", 42, 100, {
            "classification": "ESCALATE",
            "verdict": "Valid",
            "file": "x.kt",
            "line": "1",
            "author": "C",
            "reason": "old reason",
            "thread_ids": "100,101",
            "decision": "FIX",
            "verified": "PENDING",
        })

        vcs = MagicMock()
        vcs.reply_to_comment = AsyncMock(return_value=None)
        vcs.resolve_comment = AsyncMock()
        vcs.get_pr_comments = AsyncMock(return_value=[
            PRComment(id=100, body="parent", author="C",
                      path="x.kt", line=1, in_reply_to_id=None),
            PRComment(id=101, body="Will fix: ok", author="bot",
                      path="x.kt", line=1, in_reply_to_id=100),
            PRComment(id=200, body="new reply: also handle null", author="C",
                      path="x.kt", line=1, in_reply_to_id=100),
        ])
        orch._get_vcs_for_workspace = MagicMock(return_value=(vcs, None))

        ws = MagicMock()
        ws.reports_dir = tmp_path
        ws.state = SimpleNamespace(
            ticket_id="T-1", company_id="acme", repo_id="app",
            pr_number=42, current_state="PR_REVIEW",
            pending_review_comments=[], review_cycle=1,
            stage_iterations={}, human_input_reply="reviewed",
            last_verified_sha="",
        )
        ws.save_state = MagicMock()

        from orchestrator.comment_classifier import ClassifiedComment
        captured_thread_context = []
        async def fake_classify(comments, _ws, _runtime, *, operator_hint="", thread_context=None):
            captured_thread_context.append(thread_context)
            return [
                ClassifiedComment(
                    comment_id=c.id, classification="ESCALATE", verdict="Valid",
                    reason="updated reason", author=c.author, file=c.path, line=c.line, body=c.body,
                ) for c in comments
            ]
        import orchestrator.orchestrator as omod
        monkeypatch.setattr(omod, "classify_comments", fake_classify, raising=False)

        await orch._action_fetch_pr_comments(ws, stage_def=None)

        # Should have called classify_comments with thread_context for parent 100
        assert any(
            ctx and 100 in ctx
            for ctx in captured_thread_context
        )
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_thread_replies.py::TestStepBSplitBuckets -v`
Expected: FAIL — Step B is not yet implemented; existing flow filters out replies entirely.

- [ ] **Step 3: Implement Steps B and D in `_action_fetch_pr_comments`**

In `orchestrator/orchestrator.py`, locate the existing Phase 4 logic (around line 1701-1730). It currently does:

```python
all_comments = await vcs.get_pr_comments(pr_number)
all_comments = [c for c in all_comments if not c.is_thread_resolved]  # Step A from Task 8

decided_ids = set(entries.keys())
replied_to_ids = set()
for c in all_comments:
    if c.in_reply_to_id and c.body.strip().lower().startswith(_BOT_REPLY_PREFIXES):
        replied_to_ids.add(c.in_reply_to_id)

comments = [c for c in all_comments
            if not c.in_reply_to_id
            and c.id not in replied_to_ids
            and c.id not in decided_ids]
```

Replace with the Step B/D split:

```python
all_comments = await vcs.get_pr_comments(pr_number)
# Step A: drop comments in resolved threads
all_comments = [c for c in all_comments if not c.is_thread_resolved]

decided_ids = set(entries.keys())
threads = self._group_comments_into_threads(all_comments)

# Build per-parent thread_ids cache from resolution report
from orchestrator.resolution_report import parse_thread_ids
known_thread_ids: dict[int, set[int]] = {}
for cid, entry in entries.items():
    known_thread_ids[cid] = set(parse_thread_ids(entry.get("thread_ids", "")))

# Step B: split into "new parents" vs "threads with new replies"
new_parents: list[Any] = []
threads_with_new_replies: list[tuple[int, list[Any], list[int]]] = []  # (parent_id, thread, new_ids)

for parent_id, thread in threads.items():
    if parent_id not in decided_ids:
        # New parent — classify as today
        new_parents.append(thread[0])  # the parent comment
        continue

    # Existing parent — is there anything new in this thread?
    current_ids = {c.id for c in thread}
    seen_ids = known_thread_ids.get(parent_id, {parent_id})
    new_ids = current_ids - seen_ids
    if new_ids:
        threads_with_new_replies.append((parent_id, thread, sorted(new_ids)))

# Hand off to classification:
# - Step C: new_parents → existing classify path (no thread_context)
# - Step D: threads_with_new_replies → classify with thread_context

# Compose unified comments list for classify_comments
comments_to_classify = list(new_parents)
thread_context: dict[int, list[dict]] = {}
for parent_id, thread, _new_ids in threads_with_new_replies:
    parent = thread[0]
    comments_to_classify.append(parent)
    thread_context[parent_id] = self._build_thread_context(thread)
```

Then update the `classify_comments` invocation later in the method to pass `thread_context`:

```python
classified = await classify_comments(
    comments_to_classify, workspace, self._agent_runtime,
    thread_context=thread_context if thread_context else None,
)
```

After classification + entry write, populate the entry's `thread_ids`. The simplest place is at the existing `add_entry`/`update_entry` call sites. For new parents:

```python
add_entry(report_path, state.ticket_id, pr_number, cc.comment_id, {
    ...existing fields...,
    "thread_ids": format_thread_ids([cc.comment_id]),
})
```

For threads-with-new-replies, after handling the classification result:

```python
# Update the existing entry's thread_ids to include all newly-seen IDs
parent_id = cc.comment_id
seen = known_thread_ids.get(parent_id, {parent_id})
thread = next((t for pid, t, _ in threads_with_new_replies if pid == parent_id), None)
if thread is not None:
    new_chain = sorted(seen | {c.id for c in thread})
    update_entry(report_path, parent_id, {
        "thread_ids": format_thread_ids(new_chain),
        "verdict": cc.verdict,
        "reason": cc.reason,
    })
```

Add the `format_thread_ids` import:

```python
from orchestrator.resolution_report import (
    add_entry, read_entries, update_entry,
    format_thread_ids, parse_thread_ids,
)
```

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/unit/test_thread_replies.py -v`
Expected: PASS — TestStepBSplitBuckets + earlier tests still pass.

Run regression: `pytest tests/unit/test_pr_comment_decision_echo.py tests/unit/test_reinvestigation.py tests/unit/test_unanswered_recall.py tests/unit/test_escalation_view.py tests/unit/test_comment_classifier.py tests/unit/test_review_decisions_freetext.py tests/unit/test_github_reply_at_decision.py tests/unit/test_git_diff_range.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_thread_replies.py
git commit -m "feat(pr-review): Step B splits new parents from threads with new replies"
```

---

## Task 10: Acknowledgment short-circuit (`Acknowledged.` reply, ACK_RESOLVED decision)

**Files:**
- Modify: `orchestrator/orchestrator.py` (AUTO_REJECT branch in `_action_fetch_pr_comments`)
- Modify: `tests/unit/test_thread_replies.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_thread_replies.py`:

```python
class TestAcknowledgmentShortCircuit:
    @pytest.mark.asyncio
    async def test_ack_short_circuit_posts_acknowledged_not_wont_fix(
        self, tmp_path, monkeypatch
    ):
        """When agent returns 'Reviewer acknowledged' reason, post 'Acknowledged.'
        not 'Won't fix:' and record ACK_RESOLVED."""
        from orchestrator.orchestrator import Orchestrator
        from integrations.base.vcs import PRComment
        from orchestrator.resolution_report import add_entry

        orch = Orchestrator.__new__(Orchestrator)
        orch._notifier = MagicMock()
        orch._notifier.send_message = AsyncMock(return_value=999)
        orch._events = None
        orch._get_chat_id = MagicMock(return_value="chat-1")
        orch._get_ticket_title = MagicMock(return_value="Ticket")
        orch._tg_header = MagicMock(return_value="hdr")
        orch._agent_runtime = MagicMock()
        orch._log_pipeline = MagicMock()
        orch._now = MagicMock(return_value="2026-04-30T10:00:00Z")
        orch._git_diff_files = MagicMock(return_value=set())
        orch._git_head_sha = MagicMock(return_value="abcd1234")

        # Pre-seed an existing entry for parent 100
        report_path = tmp_path / "pr-review-resolution.md"
        add_entry(report_path, "T-1", 42, 100, {
            "classification": "ESCALATE",
            "verdict": "Valid",
            "file": "x.kt",
            "line": "1",
            "author": "C",
            "reason": "original",
            "thread_ids": "100,101",
            "decision": "FIX",
            "verified": "YES",
        })

        vcs = MagicMock()
        vcs.reply_to_comment = AsyncMock(return_value=None)
        vcs.resolve_comment = AsyncMock()
        vcs.get_pr_comments = AsyncMock(return_value=[
            PRComment(id=100, body="parent", author="C", path="x.kt", line=1, in_reply_to_id=None),
            PRComment(id=101, body="Will fix: ok", author="bot", path="x.kt", line=1, in_reply_to_id=100),
            PRComment(id=200, body="lgtm thanks!", author="C", path="x.kt", line=1, in_reply_to_id=100),
        ])
        orch._get_vcs_for_workspace = MagicMock(return_value=(vcs, None))

        ws = MagicMock()
        ws.reports_dir = tmp_path
        ws.state = SimpleNamespace(
            ticket_id="T-1", company_id="acme", repo_id="app",
            pr_number=42, current_state="PR_REVIEW",
            pending_review_comments=[], review_cycle=2,
            stage_iterations={}, human_input_reply="reviewed",
            last_verified_sha="",
        )
        ws.save_state = MagicMock()

        from orchestrator.comment_classifier import ClassifiedComment
        async def fake_classify(comments, _ws, _runtime, *, operator_hint="", thread_context=None):
            return [
                ClassifiedComment(
                    comment_id=100, classification="AUTO_REJECT", verdict="Valid",
                    reason="Reviewer acknowledged — no action needed",
                    author="C", file="x.kt", line=1, body="parent",
                ),
            ]
        import orchestrator.orchestrator as omod
        monkeypatch.setattr(omod, "classify_comments", fake_classify, raising=False)

        await orch._action_fetch_pr_comments(ws, stage_def=None)

        # Bot must have posted "Acknowledged." (NOT "Won't fix:")
        vcs.reply_to_comment.assert_awaited()
        bodies = [call.args[2] for call in vcs.reply_to_comment.await_args_list]
        assert any("Acknowledged" in b for b in bodies)
        assert not any(b.startswith("Won't fix:") for b in bodies)
        # And resolved
        vcs.resolve_comment.assert_awaited()
        # Resolution report records ACK_RESOLVED
        report_text = report_path.read_text()
        assert "ACK_RESOLVED" in report_text
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_thread_replies.py::TestAcknowledgmentShortCircuit -v`
Expected: FAIL — current AUTO_REJECT path always posts `"Won't fix: <reason>"`.

- [ ] **Step 3: Add the short-circuit in the AUTO_REJECT branch**

In `orchestrator/orchestrator.py`, find the AUTO_REJECT branch in `_action_fetch_pr_comments` (around line 1716, search for `elif cc.classification == "AUTO_REJECT":`). Replace:

```python
elif cc.classification == "AUTO_REJECT":
    github_reply_status = "Posted"
    resolved_status = "YES"
    if vcs and pr_number:
        try:
            await vcs.reply_to_comment(pr_number, cc.comment_id, f"Won't fix: {cc.reason}")
        except Exception as e:
            logger.warning("Failed to reply on comment %d: %s", cc.comment_id, e)
            github_reply_status = f"Failed: {str(e)[:120]}"
            resolved_status = "NO"
        # ...resolve handling...
    add_entry(report_path, state.ticket_id, pr_number, cc.comment_id, {
        "classification": "AUTO_REJECT",
        "verdict": cc.verdict,
        ...
    })
    auto_rejected.append(cc)
```

with:

```python
elif cc.classification == "AUTO_REJECT":
    is_ack = cc.reason.startswith("Reviewer acknowledged")
    reply_body = "Acknowledged." if is_ack else f"Won't fix: {cc.reason}"
    github_reply_status = "Posted (acknowledged)" if is_ack else "Posted"
    resolved_status = "YES"
    decision_value = "ACK_RESOLVED" if is_ack else "WONT_FIX"
    if vcs and pr_number:
        try:
            await vcs.reply_to_comment(pr_number, cc.comment_id, reply_body)
        except Exception as e:
            logger.warning("Failed to reply on comment %d: %s", cc.comment_id, e)
            github_reply_status = f"Failed: {str(e)[:120]}"
            resolved_status = "NO"
        try:
            await vcs.resolve_comment(pr_number, cc.comment_id)
        except Exception as e:
            logger.warning("Failed to resolve comment %d: %s", cc.comment_id, e)
            resolved_status = "NO"
    add_entry(report_path, state.ticket_id, pr_number, cc.comment_id, {
        "classification": "AUTO_REJECT",
        "verdict": cc.verdict,
        "decision": decision_value,
        "file": cc.file or "",
        "line": str(cc.line or "?"),
        "author": cc.author or "",
        "reason": cc.reason or "",
        "verified": "N/A",
        "github_reply": github_reply_status,
        "resolved": resolved_status,
        "thread_ids": format_thread_ids([cc.comment_id]),
        "cycle": str(state.review_cycle),
    })
    auto_rejected.append(cc)
```

The `decision_value` field captures the new `ACK_RESOLVED` semantic distinct from `WONT_FIX`. Existing AUTO_REJECT (non-ack) entries stay as `WONT_FIX`.

Note: for the threads-with-new-replies path (existing parent gets re-classified as AUTO_REJECT), use `update_entry` instead of `add_entry`. Adapt the field set accordingly.

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/unit/test_thread_replies.py -v`
Expected: PASS.

Run full PR-review regression:
`pytest tests/unit/test_pr_comment_decision_echo.py tests/unit/test_reinvestigation.py tests/unit/test_unanswered_recall.py tests/unit/test_escalation_view.py tests/unit/test_comment_classifier.py tests/unit/test_review_decisions_freetext.py tests/unit/test_github_reply_at_decision.py tests/unit/test_git_diff_range.py tests/unit/test_thread_replies.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_thread_replies.py
git commit -m "feat(pr-review): acknowledgment short-circuit posts 'Acknowledged.' and resolves"
```

---

## Task 11: Append bot reply IDs to `thread_ids` after every bot post

**Files:**
- Modify: `orchestrator/orchestrator.py` (every site that calls `vcs.reply_to_comment`)
- Modify: `tests/unit/test_thread_replies.py`

The `vcs.reply_to_comment` call in github_adapter currently returns `None`. To append the new reply ID, we need it to return the posted reply's ID.

- [ ] **Step 1: Update `reply_to_comment` to return the new comment ID**

In `integrations/base/vcs.py`, change the abstract method:

```python
@abstractmethod
async def reply_to_comment(
    self, pr_number: int, comment_id: int, body: str
) -> int | None:
    """Reply to a review comment. Returns the new reply's comment ID, or None on error."""
    ...
```

In `integrations/github/github_adapter.py`, change the existing implementation:

```python
async def reply_to_comment(self, pr_number: int, comment_id: int, body: str) -> int | None:
    """Reply to a review comment. Returns the new comment ID."""
    try:
        data = await self._request(
            "POST",
            f"{self._repo_path}/pulls/{pr_number}/comments/{comment_id}/replies",
            json={"body": body},
        )
        return data.get("id") if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("Failed to post reply to %d on PR #%d: %s", comment_id, pr_number, e)
        return None
```

- [ ] **Step 2: Write failing tests**

Add to `tests/unit/test_thread_replies.py`:

```python
class TestThreadIdsAppend:
    @pytest.mark.asyncio
    async def test_will_fix_reply_id_appended_to_thread_ids(
        self, tmp_path, monkeypatch
    ):
        """After AUTO_FIX posts 'Will fix: ...', the reply's ID is appended
        to the parent's thread_ids in the resolution report."""
        from orchestrator.orchestrator import Orchestrator
        from integrations.base.vcs import PRComment

        orch = Orchestrator.__new__(Orchestrator)
        orch._notifier = MagicMock()
        orch._notifier.send_message = AsyncMock(return_value=999)
        orch._events = None
        orch._get_chat_id = MagicMock(return_value="chat-1")
        orch._get_ticket_title = MagicMock(return_value="Ticket")
        orch._tg_header = MagicMock(return_value="hdr")
        orch._agent_runtime = MagicMock()
        orch._log_pipeline = MagicMock()
        orch._now = MagicMock(return_value="2026-04-30T10:00:00Z")
        orch._git_diff_files = MagicMock(return_value=set())
        orch._git_head_sha = MagicMock(return_value="abcd1234")

        vcs = MagicMock()
        # The bot's reply gets ID 999
        vcs.reply_to_comment = AsyncMock(return_value=999)
        vcs.resolve_comment = AsyncMock()
        vcs.get_pr_comments = AsyncMock(return_value=[
            PRComment(id=100, body="please fix", author="C",
                      path="x.kt", line=1, in_reply_to_id=None),
        ])
        orch._get_vcs_for_workspace = MagicMock(return_value=(vcs, None))

        ws = MagicMock()
        ws.reports_dir = tmp_path
        ws.state = SimpleNamespace(
            ticket_id="T-1", company_id="acme", repo_id="app",
            pr_number=42, current_state="PR_REVIEW",
            pending_review_comments=[], review_cycle=0,
            stage_iterations={}, human_input_reply="reviewed",
            last_verified_sha="",
        )
        ws.save_state = MagicMock()

        from orchestrator.comment_classifier import ClassifiedComment
        async def fake_classify(comments, _ws, _runtime, *, operator_hint="", thread_context=None):
            return [
                ClassifiedComment(
                    comment_id=100, classification="AUTO_FIX", verdict="Valid",
                    reason="trivial fix", suggested_fix="add @Inject",
                    author="C", file="x.kt", line=1, body="please fix",
                ),
            ]
        import orchestrator.orchestrator as omod
        monkeypatch.setattr(omod, "classify_comments", fake_classify, raising=False)

        await orch._action_fetch_pr_comments(ws, stage_def=None)

        report = (tmp_path / "pr-review-resolution.md").read_text()
        # thread_ids should be "100,999" (parent + bot's Will fix reply)
        assert "Thread Ids: 100,999" in report or "Thread Ids: 100, 999" in report
```

- [ ] **Step 3: Run — must fail**

Run: `pytest tests/unit/test_thread_replies.py::TestThreadIdsAppend -v`
Expected: FAIL — bot reply ID is not currently appended to thread_ids.

- [ ] **Step 4: Update each `vcs.reply_to_comment` call site to capture the ID**

In `orchestrator/orchestrator.py`, every site that calls `vcs.reply_to_comment` should capture the returned ID and append to the parent's `thread_ids`. The sites are:

1. AUTO_FIX classification (near line 1697)
2. AUTO_REJECT classification (near line 1716)
3. `_execute_review_decisions` FIX branch (near line 1842)
4. `_execute_review_decisions` WON'T_FIX branch (near line 1858)
5. Phase 1 verification "Fixed in commit" reply (near line 1611)

Pattern at each site:

```python
new_reply_id = None
try:
    new_reply_id = await vcs.reply_to_comment(pr_number, cc.comment_id, reply_body)
except Exception as e:
    logger.warning("Failed to post reply on comment %d: %s", cc.comment_id, e)
    github_reply_status = f"Failed: {str(e)[:120]}"

# When recording the entry, build the full thread_ids chain
chain = [cc.comment_id]
if new_reply_id is not None:
    chain.append(new_reply_id)
add_entry(..., {"thread_ids": format_thread_ids(chain), ...})
```

For the `_execute_review_decisions` branches (which use `update_entry`), append to the existing chain:

```python
existing_entry = read_entries(report_path).get(cid, {})
existing_chain = parse_thread_ids(existing_entry.get("thread_ids", str(cid)))
if new_reply_id is not None and new_reply_id not in existing_chain:
    existing_chain.append(new_reply_id)
update_entry(report_path, cid, {
    "thread_ids": format_thread_ids(existing_chain),
    ...other field updates...,
})
```

For Phase 1's "Fixed in commit" reply (which uses `update_entry`):

```python
new_reply_id = await vcs.reply_to_comment(pr_number, cid, f"Fixed in commit {sha[:8]}")
existing_chain = parse_thread_ids(entry.get("thread_ids", str(cid)))
if new_reply_id is not None and new_reply_id not in existing_chain:
    existing_chain.append(new_reply_id)
update_entry(report_path, cid, {
    "verified": "YES",
    "fixed_in": sha[:8],
    "verified_at": self._now(),
    "thread_ids": format_thread_ids(existing_chain),
})
```

- [ ] **Step 5: Run — must pass**

Run: `pytest tests/unit/test_thread_replies.py -v`
Expected: PASS.

Run regression: `pytest tests/unit/ -v` (full suite — many existing tests use mocked `vcs.reply_to_comment` returning `None`; those should still work since the code uses `if new_reply_id is not None`).
Expected: PASS — 938+ tests.

- [ ] **Step 6: Commit**

```bash
git add integrations/base/vcs.py integrations/github/github_adapter.py orchestrator/orchestrator.py tests/unit/test_thread_replies.py
git commit -m "feat(pr-review): append bot reply IDs to thread_ids chain"
```

---

## Task 12: `_reinvestigate_pending` defensive flag clear (stale-tick detection)

**Files:**
- Modify: `orchestrator/orchestrator.py` (`_reinvestigate_pending` method)
- Modify: `tests/unit/test_reinvestigation.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_reinvestigation.py` in the `TestOrchestratorReinvestigation` class:

```python
@pytest.mark.asyncio
async def test_stale_flag_self_clears_after_two_no_progress_ticks(
    self, tmp_path, monkeypatch
):
    """If pending_reinvestigation is True for two consecutive ticks
    without progress (last_hint and hint_rounds unchanged), the flag
    self-clears with a TG warning."""
    from orchestrator.orchestrator import Orchestrator
    import orchestrator.orchestrator as orch_mod

    orch = Orchestrator.__new__(Orchestrator)
    orch._notifier = MagicMock()
    orch._notifier.send_message = AsyncMock()
    orch._events = None
    orch._get_chat_id = MagicMock(return_value="chat-1")
    orch._agent_runtime = MagicMock()

    ws = MagicMock()
    ws.reports_dir = tmp_path
    ws.state = SimpleNamespace(
        ticket_id="T-1", company_id="acme", repo_id="app",
        pr_number=42, review_cycle=1, stage_iterations={},
        pending_review_comments=[
            {"comment_id": 1, "msg_ids": [100], "decision": None,
             "author": "C", "file": "x.kt", "line": 1, "body": "b",
             "reason": "r", "verdict": "Valid",
             "hint_rounds": 1, "last_hint": "stuck",
             "pending_reinvestigation": True,
             "reinvestigation_stale_ticks": 1},  # already one no-progress tick
        ],
    )
    ws.save_state = MagicMock()

    # Force a no-op classify (no-result path) to simulate "no progress"
    async def fake_no_progress(comments, workspace, runtime, *, operator_hint=""):
        # Returning [] reproduces the "no result" path which currently doesn't
        # change last_hint/hint_rounds; the stale-tick logic must take over.
        return []
    monkeypatch.setattr(orch_mod, "classify_comments", fake_no_progress, raising=False)

    await orch._reinvestigate_pending(ws)

    c = ws.state.pending_review_comments[0]
    # After this second no-progress tick: flag cleared, counter reset, TG warning
    assert c["pending_reinvestigation"] is False
    assert c["reinvestigation_stale_ticks"] == 0
    orch._notifier.send_message.assert_awaited()
    msg = orch._notifier.send_message.call_args.args[1]
    assert "inactivity" in msg.lower() or "stale" in msg.lower()
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_reinvestigation.py::TestOrchestratorReinvestigation::test_stale_flag_self_clears_after_two_no_progress_ticks -v`
Expected: FAIL — no stale-tick logic yet.

- [ ] **Step 3: Add stale-tick detection in `_reinvestigate_pending`**

In `orchestrator/orchestrator.py`, find `_reinvestigate_pending` and add stale-tick handling. At the start of the per-comment loop iteration:

```python
for c in pending:
    if not c.get("pending_reinvestigation"):
        # Flag is False — also reset stale counter if set
        if c.get("reinvestigation_stale_ticks"):
            c["reinvestigation_stale_ticks"] = 0
        continue
    if c.get("decision") is not None:
        c["pending_reinvestigation"] = False
        c["reinvestigation_stale_ticks"] = 0
        workspace.save_state()
        continue

    # Snapshot pre-classify state to detect "no progress"
    pre_last_hint = c.get("last_hint")
    pre_hint_rounds = c.get("hint_rounds", 0)

    comment_stub = SimpleNamespace(...)  # existing
    ...existing classify call...

    # After the classify call (success or no-result), check for progress
    post_last_hint = c.get("last_hint")
    post_hint_rounds = c.get("hint_rounds", 0)
    no_progress = (post_last_hint == pre_last_hint and post_hint_rounds == pre_hint_rounds)

    if no_progress and c.get("pending_reinvestigation"):
        stale = int(c.get("reinvestigation_stale_ticks", 0) or 0) + 1
        if stale >= 2:
            # Force-clear with TG warning
            c["pending_reinvestigation"] = False
            c["reinvestigation_stale_ticks"] = 0
            workspace.save_state()
            if self._notifier:
                chat_id = self._get_chat_id(workspace)
                if chat_id:
                    await self._notifier.send_message(
                        chat_id,
                        f"⚠ Re-investigation flag cleared due to inactivity for "
                        f"@{c.get('author','?')} on {c.get('file','?')}:{c.get('line','?')}. "
                        f"Reply Fix or Won't Fix to close.",
                    )
        else:
            c["reinvestigation_stale_ticks"] = stale
            workspace.save_state()
    elif not no_progress:
        c["reinvestigation_stale_ticks"] = 0
        workspace.save_state()
```

This wraps the existing classify-and-update logic, adding stale-tick tracking around it. Adapt the surrounding control flow as needed — the existing `continue` statements stay where they are; the stale-tick block runs after the current per-comment processing.

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/unit/test_reinvestigation.py -v`
Expected: PASS — all reinvestigation tests pass.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_reinvestigation.py
git commit -m "feat(pr-review): self-heal stuck pending_reinvestigation flag after 2 stale ticks"
```

---

## Task 13: Second-cycle integration test

**Files:**
- Create: `tests/unit/test_pr_review_second_cycle.py`

- [ ] **Step 1: Write the integration test**

Create `tests/unit/test_pr_review_second_cycle.py`:

```python
"""Integration test: full 2-cycle PR review flow against a fake VCS.

Cycle 1: classify, escalate, operator decides FIX, dev-agent (mocked) commits.
Cycle 2: verify FIX landed, detect new thread reply, detect new top-level comment.
Asserts the thread_ids chain is correct and routing is correct on each cycle.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _orch():
    from orchestrator.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o._notifier = MagicMock()
    o._notifier.send_message = AsyncMock(return_value=999)
    o._events = None
    o._get_chat_id = MagicMock(return_value="chat-1")
    o._get_ticket_title = MagicMock(return_value="Ticket")
    o._tg_header = MagicMock(return_value="hdr")
    o._agent_runtime = MagicMock()
    o._log_pipeline = MagicMock()
    o._now = MagicMock(return_value="2026-04-30T10:00:00Z")
    o._git_diff_files = MagicMock(return_value={"x.kt"})
    o._git_head_sha = MagicMock(return_value="abcd1234")
    return o


@pytest.mark.asyncio
async def test_two_cycle_flow_thread_chain_and_new_parent(tmp_path, monkeypatch):
    from integrations.base.vcs import PRComment
    from orchestrator.orchestrator import Orchestrator
    import orchestrator.orchestrator as orch_mod
    from orchestrator.comment_classifier import ClassifiedComment

    orch = _orch()

    # Mutable VCS state to swap between cycles
    cycle = {"n": 1}

    vcs = MagicMock()

    will_fix_reply_id = 101  # the bot's Will fix reply
    fixed_in_reply_id = 555  # the bot's "Fixed in commit" reply on cycle 2

    posted: list[tuple[int, int, str]] = []  # (pr_number, comment_id, body)

    async def fake_reply(pr_number, comment_id, body):
        posted.append((pr_number, comment_id, body))
        if body.startswith("Will fix"):
            return will_fix_reply_id
        if body.startswith("Fixed in commit"):
            return fixed_in_reply_id
        return 700

    vcs.reply_to_comment = AsyncMock(side_effect=fake_reply)
    vcs.resolve_comment = AsyncMock()

    async def fake_get_comments(_pr):
        if cycle["n"] == 1:
            return [
                PRComment(id=100, body="please add @Inject", author="C",
                          path="x.kt", line=1, in_reply_to_id=None,
                          is_thread_resolved=False),
            ]
        # Cycle 2: parent + our Will fix reply + new thread reply + new top-level
        return [
            PRComment(id=100, body="please add @Inject", author="C",
                      path="x.kt", line=1, in_reply_to_id=None,
                      is_thread_resolved=False),
            PRComment(id=will_fix_reply_id, body="Will fix: trivial",
                      author="bot", path="x.kt", line=1, in_reply_to_id=100,
                      is_thread_resolved=False),
            PRComment(id=200, body="also handle null pls", author="C",
                      path="x.kt", line=1, in_reply_to_id=100,
                      is_thread_resolved=False),
            PRComment(id=300, body="new concern in different file", author="C",
                      path="y.kt", line=2, in_reply_to_id=None,
                      is_thread_resolved=False),
        ]
    vcs.get_pr_comments = fake_get_comments
    orch._get_vcs_for_workspace = MagicMock(return_value=(vcs, None))

    ws = MagicMock()
    ws.reports_dir = tmp_path
    ws.state = SimpleNamespace(
        ticket_id="T-1", company_id="acme", repo_id="app",
        pr_number=42, current_state="PR_REVIEW",
        pending_review_comments=[], review_cycle=0,
        stage_iterations={}, human_input_reply="reviewed",
        last_verified_sha="",
    )
    ws.save_state = MagicMock()

    # Cycle 1: classifier returns AUTO_FIX so it auto-progresses without
    # human input. The thread test focuses on chain correctness, not the
    # operator-in-the-loop path.
    async def fake_classify_cycle1(comments, *_a, **_kw):
        return [
            ClassifiedComment(
                comment_id=c.id, classification="AUTO_FIX", verdict="Valid",
                reason="trivial annotation fix", suggested_fix="add @Inject",
                author=c.author, file=c.path, line=c.line, body=c.body,
            ) for c in comments
        ]

    monkeypatch.setattr(orch_mod, "classify_comments", fake_classify_cycle1, raising=False)

    # === Cycle 1 ===
    await orch._action_fetch_pr_comments(ws, stage_def=None)

    report = (tmp_path / "pr-review-resolution.md").read_text()
    # After cycle 1: parent + our Will fix reply
    assert "Thread Ids: 100,101" in report or "Thread Ids: 100, 101" in report

    # === Cycle 2 ===
    cycle["n"] = 2
    ws.state.review_cycle = 1
    ws.state.human_input_reply = "reviewed"  # operator signals again

    # Cycle 2 classifier: thread re-classification on parent 100 (with thread_context)
    # and standard classification of new parent 300.
    captured_thread_contexts: list[dict | None] = []

    async def fake_classify_cycle2(comments, *_a, thread_context=None, **_kw):
        captured_thread_contexts.append(thread_context)
        results = []
        for c in comments:
            if c.id == 100:
                # Thread reply is "also handle null" — classify as ESCALATE
                results.append(ClassifiedComment(
                    comment_id=100, classification="ESCALATE", verdict="Valid",
                    reason="follow-up: handle null case",
                    author=c.author, file=c.path, line=c.line, body=c.body,
                ))
            else:
                # New parent 300 — classify as ESCALATE
                results.append(ClassifiedComment(
                    comment_id=c.id, classification="ESCALATE", verdict="Valid",
                    reason="new concern", author=c.author,
                    file=c.path, line=c.line, body=c.body,
                ))
        return results

    monkeypatch.setattr(orch_mod, "classify_comments", fake_classify_cycle2, raising=False)

    await orch._action_fetch_pr_comments(ws, stage_def=None)

    # Cycle 2 assertions
    report = (tmp_path / "pr-review-resolution.md").read_text()
    # Parent 100's thread_ids now includes 100, 101, 200
    assert (
        "Thread Ids: 100,101,200" in report
        or "Thread Ids: 100, 101, 200" in report
        or "Thread Ids: 100,200,101" in report  # order tolerance
    )
    # Comment 300 has its own entry with thread_ids = 300
    assert "## Comment #300" in report
    assert "Thread Ids: 300" in report

    # Cycle 2 must have called classifier WITH thread_context for parent 100
    cycle2_ctx = captured_thread_contexts[0] if captured_thread_contexts else None
    assert cycle2_ctx is not None
    assert 100 in cycle2_ctx
    # The thread context for 100 should mention "also handle null"
    parent_thread = cycle2_ctx[100]
    assert any("handle null" in entry.get("body", "") for entry in parent_thread)

    # Pending operator decisions for both 100 and 300
    pending_ids = {p.get("comment_id") for p in (ws.state.pending_review_comments or [])}
    assert 100 in pending_ids
    assert 300 in pending_ids
```

- [ ] **Step 2: Run — should pass with the implementation from Tasks 1-12**

Run: `pytest tests/unit/test_pr_review_second_cycle.py -v`
Expected: PASS.

If it fails, the failure indicates a real integration bug. Diagnose and fix in Task 1-12 implementation, not by relaxing the assertions.

- [ ] **Step 3: Run full regression**

Run: `pytest tests/unit/ -v 2>&1 | tail -10`
Expected: 950+ tests pass, 0 fail.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_pr_review_second_cycle.py
git commit -m "test(pr-review): integration test for second-cycle thread + new-parent flow"
```

---

## Task 14: Documentation update

**Files:**
- Modify: `docs/features/agent-system.md` (or wherever the PR-review feature description lives)
- Modify: `docs/features/pr-comment-responder.md`

- [ ] **Step 1: Update PR review section in agent-system.md**

Find the "PR Review: Verdict, Hints, and Recall" section added in the prior PR-review work. Append a new paragraph:

```markdown
### Second-Cycle Hygiene

On subsequent review cycles, the pipeline detects reviewer thread replies (follow-up questions inside an existing comment thread) and routes them through classification with the full conversation as context. Acknowledgments ("lgtm", "thanks", "ok") auto-resolve with a polite "Acknowledged." reply on GitHub — operator stays out of the loop. Threads the reviewer manually resolved on GitHub (via the UI) are skipped on the next cycle. Each parent comment's resolution-report entry stores a `thread_ids` chain (parent + every reply we've seen, ours and theirs) so set-subtraction against the live thread reveals what's new.

See: [docs/superpowers/specs/2026-04-30-pr-review-second-cycle-hygiene-design.md](../superpowers/specs/2026-04-30-pr-review-second-cycle-hygiene-design.md).
```

- [ ] **Step 2: Add changelog entry to `pr-comment-responder.md`**

If the file has a Change Log section (it should from the prior work), add an entry dated 2026-04-30 describing the second-cycle hygiene additions.

- [ ] **Step 3: Commit**

```bash
git add docs/features/agent-system.md docs/features/pr-comment-responder.md
git commit -m "docs(pr-review): document second-cycle thread-reply handling"
```

---

## Final Verification

- [ ] **Run the full test suite**

Run: `pytest tests/ -v 2>&1 | tail -5`
Expected: PASS — 950+ tests, no regressions.

- [ ] **Inspect the diff**

Run: `git log --oneline master..HEAD`
Expected: ~14 atomic commits, one per task.

- [ ] **Manual smoke test (optional, requires running pipeline against ACME-14463)**

After deploying:

1. Signal `reviewed` in TG for ACME-14463 — observe Phase 1 verifies the (now-correct) cumulative diff (gap-2 fix from prior PR).
2. The previously-deleted SKIP comment `QrCodeRepository.kt:13` re-flows through the new escalation pipeline with verdict line.
3. Operator can free-text-reply to it (re-investigation works).
4. If reviewer adds a thread reply on any of the verified-FIX comments, next cycle classifies it with full context.
5. If reviewer marks any thread resolved on GitHub, next cycle skips it.
