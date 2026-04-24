# PR Review Pipeline Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the PR review loop by unifying reports directories, making the resolution report the single source of truth, and verifying fixes via git diff before replying on GitHub.

**Architecture:** Merge `workspace_root/reports/` and `source/reports/` into one directory. The resolution report persists comment decisions across cycles — already-decided comments are skipped. After push, verify each PENDING fix by checking the git diff. Only reply "Fixed" when the file was actually modified.

**Tech Stack:** Python 3.10+, asyncio, pytest.

**Spec:** [docs/superpowers/specs/2026-04-24-pr-review-pipeline-redesign.md](../specs/2026-04-24-pr-review-pipeline-redesign.md)

---

## File Structure

**New files:**
- `orchestrator/resolution_report.py` — read/write/update the resolution report
- `tests/unit/test_resolution_report.py` — unit tests

**Modified files:**
- `workspace/workspace.py:118-119` — `reports_dir` points to `source/reports/`
- `workspace/workspace_manager.py:85` — create `source/reports/` instead of `workspace_root/reports/`
- `orchestrator/agent_runtime.py:141-157` — simplify context injection (one directory)
- `orchestrator/orchestrator.py:1072` — push handler: verify fixes after push
- `orchestrator/orchestrator.py:1173` — rewrite `_action_fetch_pr_comments`
- `orchestrator/orchestrator.py:1346` — rewrite `_execute_review_decisions`
- `orchestrator/orchestrator.py:1795` — remove old `_write_resolution_report`

---

## Task 1: Create resolution report module

**Files:**
- Create: `orchestrator/resolution_report.py`
- Create: `tests/unit/test_resolution_report.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_resolution_report.py`:

```python
"""Tests for the resolution report — single source of truth for PR comment decisions."""

from __future__ import annotations

from pathlib import Path

from orchestrator.resolution_report import (
    add_entry,
    read_entries,
    update_entry,
)


class TestReadEntries:
    def test_empty_file(self, tmp_path):
        report = tmp_path / "pr-review-resolution.md"
        entries = read_entries(report)
        assert entries == {}

    def test_reads_existing_entries(self, tmp_path):
        report = tmp_path / "pr-review-resolution.md"
        report.write_text(
            "# PR Review Resolution — T-1\nPR: #42\n\n"
            "## Comment #12345\n"
            "- File: a.kt:10\n"
            "- Author: @Copilot\n"
            "- Body: Fix this\n"
            "- Decision: FIX\n"
            "- Verified: PENDING\n"
        )
        entries = read_entries(report)
        assert 12345 in entries
        assert entries[12345]["decision"] == "FIX"
        assert entries[12345]["verified"] == "PENDING"
        assert entries[12345]["file"] == "a.kt:10"


class TestAddEntry:
    def test_creates_file_if_missing(self, tmp_path):
        report = tmp_path / "pr-review-resolution.md"
        add_entry(report, "T-1", 42, 12345, {
            "file": "a.kt:10",
            "author": "@Copilot",
            "body": "Fix this",
            "decision": "FIX",
            "decided_by": "auto-classifier",
            "verified": "PENDING",
        })
        assert report.exists()
        entries = read_entries(report)
        assert 12345 in entries

    def test_appends_to_existing(self, tmp_path):
        report = tmp_path / "pr-review-resolution.md"
        add_entry(report, "T-1", 42, 111, {"file": "a.kt:1", "decision": "FIX", "verified": "PENDING"})
        add_entry(report, "T-1", 42, 222, {"file": "b.kt:2", "decision": "WON'T_FIX"})
        entries = read_entries(report)
        assert len(entries) == 2


class TestUpdateEntry:
    def test_updates_existing_field(self, tmp_path):
        report = tmp_path / "pr-review-resolution.md"
        add_entry(report, "T-1", 42, 12345, {
            "file": "a.kt:10",
            "decision": "FIX",
            "verified": "PENDING",
        })
        update_entry(report, 12345, {"verified": "YES", "verify_commit": "abc123"})
        entries = read_entries(report)
        assert entries[12345]["verified"] == "YES"
        assert entries[12345]["verify_commit"] == "abc123"

    def test_preserves_other_fields(self, tmp_path):
        report = tmp_path / "pr-review-resolution.md"
        add_entry(report, "T-1", 42, 12345, {
            "file": "a.kt:10",
            "decision": "FIX",
            "verified": "PENDING",
        })
        update_entry(report, 12345, {"verified": "YES"})
        entries = read_entries(report)
        assert entries[12345]["file"] == "a.kt:10"
        assert entries[12345]["decision"] == "FIX"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_resolution_report.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create the module**

Create `orchestrator/resolution_report.py`:

```python
"""Resolution report — single source of truth for PR comment decisions.

Each PR comment gets one permanent entry. Decisions persist across review
cycles. The PR review action reads this before classifying to skip
already-decided comments.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path


def read_entries(report_path: Path) -> dict[int, dict[str, str]]:
    """Parse the resolution report. Returns {comment_id: {field: value}}."""
    if not report_path.exists():
        return {}

    content = report_path.read_text(encoding="utf-8")
    entries: dict[int, dict[str, str]] = {}
    current_id: int | None = None
    current_fields: dict[str, str] = {}

    for line in content.splitlines():
        # Match "## Comment #12345"
        m = re.match(r"^## Comment #(\d+)", line)
        if m:
            if current_id is not None:
                entries[current_id] = current_fields
            current_id = int(m.group(1))
            current_fields = {}
            continue

        # Match "- Field: Value"
        m = re.match(r"^- (\w[\w\s]*?):\s*(.+)$", line)
        if m and current_id is not None:
            key = m.group(1).strip().lower().replace(" ", "_")
            current_fields[key] = m.group(2).strip()

    if current_id is not None:
        entries[current_id] = current_fields

    return entries


def add_entry(
    report_path: Path,
    ticket_id: str,
    pr_number: int,
    comment_id: int,
    fields: dict[str, str],
) -> None:
    """Add a new comment entry to the resolution report."""
    if not report_path.exists():
        report_path.write_text(
            f"# PR Review Resolution — {ticket_id}\nPR: #{pr_number}\n\n",
            encoding="utf-8",
        )

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    lines = [f"\n## Comment #{comment_id}\n"]
    # Add decided_at if not provided
    if "decided_at" not in fields:
        fields["decided_at"] = timestamp
    for key, value in fields.items():
        display_key = key.replace("_", " ").title()
        lines.append(f"- {display_key}: {value}\n")

    with open(report_path, "a", encoding="utf-8") as f:
        f.writelines(lines)


def update_entry(
    report_path: Path,
    comment_id: int,
    updates: dict[str, str],
) -> None:
    """Update fields on an existing comment entry in place."""
    if not report_path.exists():
        return

    content = report_path.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)
    result: list[str] = []
    in_target = False
    updated_keys: set[str] = set()

    for line in lines:
        m = re.match(r"^## Comment #(\d+)", line)
        if m:
            # Leaving previous section — append any new fields
            if in_target:
                for key, value in updates.items():
                    if key not in updated_keys:
                        display_key = key.replace("_", " ").title()
                        result.append(f"- {display_key}: {value}\n")
            in_target = int(m.group(1)) == comment_id
            updated_keys = set()
            result.append(line)
            continue

        if in_target:
            fm = re.match(r"^- (\w[\w\s]*?):\s*(.+)$", line)
            if fm:
                key = fm.group(1).strip().lower().replace(" ", "_")
                if key in updates:
                    display_key = key.replace("_", " ").title()
                    result.append(f"- {display_key}: {updates[key]}\n")
                    updated_keys.add(key)
                    continue
        result.append(line)

    # If we were still in target at EOF
    if in_target:
        for key, value in updates.items():
            if key not in updated_keys:
                display_key = key.replace("_", " ").title()
                result.append(f"- {display_key}: {value}\n")

    report_path.write_text("".join(result), encoding="utf-8")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_resolution_report.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/resolution_report.py tests/unit/test_resolution_report.py
git commit -m "feat: resolution report module — single source of truth for PR comment decisions"
```

---

## Task 2: Unify reports directory

**Files:**
- Modify: `workspace/workspace.py:118-119`
- Modify: `workspace/workspace_manager.py:85`
- Modify: `orchestrator/agent_runtime.py:141-157`

- [ ] **Step 1: Change `reports_dir` property**

In `workspace/workspace.py`, change the `reports_dir` property (line 118-119):

```python
@property
def reports_dir(self) -> Path:
    return self._root / "source" / "reports"
```

Also check if `AdminWorkspace` has the same property (line ~272) and update if so.

- [ ] **Step 2: Update workspace creation**

In `workspace/workspace_manager.py`, line 85, change:

```python
(workspace_root / "source" / "reports").mkdir(parents=True, exist_ok=True)
```

Remove the old `(workspace_root / "reports").mkdir(exist_ok=True)` line.

- [ ] **Step 3: Simplify context injection**

In `orchestrator/agent_runtime.py`, lines 141-157, simplify to read only `meta_dir`:

```python
        # 3. Workspace context files (read from meta_dir)
        # Note: reports/ is inside source/ — agent reads them directly via tools
        context_sections: list[str] = []
        context_dir = workspace.meta_dir
        if context_dir.exists():
            for ctx_file in sorted(context_dir.iterdir()):
                if ctx_file.is_file():
                    try:
                        file_content = ctx_file.read_text(encoding="utf-8")
                        if len(file_content) > 5000:
                            file_content = file_content[:5000] + "\n...(truncated)"
                        context_sections.append(
                            f"<context file=\"{ctx_file.name}\">\n{file_content}\n</context>"
                        )
                    except (UnicodeDecodeError, OSError):
                        pass

        if context_sections:
            prompt_body += "\n\n## Workspace Context\n\n" + "\n\n".join(context_sections)
```

- [ ] **Step 4: Remove "write to both locations" workarounds**

In `orchestrator/orchestrator.py`, remove the two `source_reports` blocks:
- Lines ~1297-1300 (in `_action_fetch_pr_comments`)
- Lines ~1385-1388 (in `_execute_review_decisions`)

These are no longer needed — `workspace.reports_dir` now IS `source/reports/`.

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/ -q`
Expected: PASS (fix any test that hardcodes the old reports path)

- [ ] **Step 6: Commit**

```bash
git add workspace/workspace.py workspace/workspace_manager.py orchestrator/agent_runtime.py orchestrator/orchestrator.py
git commit -m "refactor: unify reports directory — source/reports/ is the single location"
```

---

## Task 3: Rewrite `_action_fetch_pr_comments` with resolution report

**Files:**
- Modify: `orchestrator/orchestrator.py:1173`

- [ ] **Step 1: Rewrite the method**

Replace `_action_fetch_pr_comments` (starting at line 1173) with:

```python
    async def _action_fetch_pr_comments(
        self, workspace: Workspace, stage_def: Any,
    ) -> ActionResult:
        """PR review comment resolution flow — resolution report is source of truth."""
        from orchestrator.comment_classifier import classify_comments
        from orchestrator.resolution_report import read_entries, add_entry, update_entry

        state = workspace.state
        pr_number = state.pr_number

        if not pr_number:
            return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})

        report_path = workspace.reports_dir / "pr-review-resolution.md"

        # Phase 1: Check pending verifications from previous cycle
        vcs, repo_config = self._get_vcs_for_workspace(workspace)
        existing = read_entries(report_path)
        pending_verifications = {
            cid: e for cid, e in existing.items()
            if e.get("verified") == "PENDING"
        }
        if pending_verifications and vcs:
            diff_files = self._git_diff_files(workspace)
            for cid, entry in pending_verifications.items():
                target_file = entry.get("file", "").split(":")[0]  # strip line number
                if any(f.endswith(target_file) or target_file.endswith(f) for f in diff_files):
                    sha = self._git_head_sha(workspace)
                    update_entry(report_path, cid, {
                        "verified": "YES",
                        "verified_at": self._now(),
                        "verify_commit": sha[:8],
                        "github_reply": f"Fixed in commit {sha[:8]}",
                        "resolved": "YES",
                    })
                    try:
                        await vcs.reply_to_comment(pr_number, cid, f"Fixed in commit {sha[:8]}.")
                        await vcs.resolve_comment(pr_number, cid)
                    except Exception as e:
                        logger.warning("Failed to reply/resolve %d: %s", cid, e)
                    self._log_pipeline(workspace, f"Verified fix: {target_file} in diff (commit {sha[:8]})")
                else:
                    fail_count = int(entry.get("fail_count", "0")) + 1
                    update_entry(report_path, cid, {
                        "verified": "FAILED",
                        "fail_count": str(fail_count),
                    })
                    if fail_count >= 2:
                        self._log_pipeline(workspace, f"Fix failed twice for comment #{cid}: {target_file}")
                        if self._notifier:
                            chat_id = self._get_chat_id(workspace)
                            if chat_id:
                                await self._notifier.send_message(chat_id, (
                                    f"⚠️ [{state.company_id}/{state.repo_id}] {state.ticket_id}\n"
                                    f"Dev-agent failed to fix comment #{cid} ({target_file}) after 2 attempts.\n"
                                    f"Please fix manually or skip."
                                ))

        # Phase 2: Check pending escalated decisions
        pending = state.pending_review_comments or []
        undecided = [c for c in pending if c.get("decision") is None]
        if pending and not undecided:
            return await self._execute_review_decisions(workspace)
        if undecided:
            return ActionResult(success=False, next_state="", error="", metadata={}, skipped=True)

        # Phase 3: Wait for "reviewed" signal
        reply = (state.human_input_reply or "").lower()
        if "reviewed" not in reply and "proceed" not in reply:
            return ActionResult(success=False, next_state="", error="", metadata={}, skipped=True)

        state.human_input_reply = None
        state.review_cycle = (state.review_cycle or 0) + 1
        state.stage_iterations["pr_review"] = 0
        workspace.save_state()

        # Phase 4: Fetch + filter comments
        if not vcs:
            return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})

        try:
            all_comments = await vcs.get_pr_comments(pr_number)
        except Exception as e:
            logger.error("Failed to fetch PR comments for %s: %s", state.ticket_id, e)
            return ActionResult(success=False, next_state="", error=f"Failed to fetch: {e}", metadata={})

        # Filter: root only, skip already-decided (by comment ID in resolution report)
        existing = read_entries(report_path)  # re-read after verification updates
        decided_ids = set(existing.keys())
        replied_to_ids = {c.in_reply_to_id for c in all_comments if c.in_reply_to_id}
        comments = [
            c for c in all_comments
            if not c.in_reply_to_id and c.id not in decided_ids
        ]
        logger.info("PR #%d: %d total, %d decided, %d new", pr_number, len(all_comments), len(decided_ids), len(comments))

        # Check: no new comments AND no PENDING verifications → DONE
        still_pending = any(e.get("verified") == "PENDING" for e in read_entries(report_path).values())
        if not comments and not still_pending:
            self._log_pipeline(workspace, f"PR review complete. All comments resolved. → DONE")
            return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})
        if not comments and still_pending:
            # Fixes pending verification — need another push cycle
            return ActionResult(success=False, next_state="", error="", metadata={}, skipped=True)

        # Write comments for reference
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

        # Phase 5: Classify new comments
        classified = await classify_comments(comments, workspace, self._agent_runtime)

        auto_fixed, auto_rejected, escalated = [], [], []
        for cc in classified:
            if cc.classification == "AUTO_FIX":
                auto_fixed.append(cc)
                add_entry(report_path, state.ticket_id, pr_number, cc.comment_id, {
                    "file": f"{cc.file}:{cc.line or '?'}",
                    "author": f"@{cc.author}",
                    "body": cc.body[:200],
                    "decision": "FIX",
                    "decided_by": "auto-classifier",
                    "verified": "PENDING",
                    "fail_count": "0",
                })
            elif cc.classification == "AUTO_REJECT":
                try:
                    await vcs.reply_to_comment(pr_number, cc.comment_id, f"Won't fix: {cc.reason}")
                    await vcs.resolve_comment(pr_number, cc.comment_id)
                except Exception as e:
                    logger.warning("Failed to reply/resolve %d: %s", cc.comment_id, e)
                auto_rejected.append(cc)
                add_entry(report_path, state.ticket_id, pr_number, cc.comment_id, {
                    "file": f"{cc.file}:{cc.line or '?'}",
                    "author": f"@{cc.author}",
                    "body": cc.body[:200],
                    "decision": "WON'T_FIX",
                    "decided_by": "auto-classifier",
                    "reason": cc.reason,
                    "github_reply": f"Won't fix: {cc.reason}",
                    "resolved": "YES",
                })
            else:
                escalated.append(cc)

        # TG summary
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
                lines.append(sep)
                if escalated:
                    lines.append(f"Waiting for your decisions on {len(escalated)} escalated comment(s).")
                await self._notifier.send_message(chat_id, "\n".join(lines))

        summary = f"PR review cycle {state.review_cycle}: {len(auto_fixed)} fix, {len(auto_rejected)} rejected, {len(escalated)} escalated"
        self._log_pipeline(workspace, f"{summary}. See pr-review-resolution.md")

        if not escalated:
            if auto_fixed:
                fix_md = "# PR Comment Fixes Required\n\n"
                for af in auto_fixed:
                    fix_md += f"## Fix: {af.file}:{af.line or '?'}\n"
                    fix_md += f"Comment by @{af.author}: {af.body[:200]}\n"
                    fix_md += f"What to do: {af.suggested_fix or af.reason}\n\n"
                (workspace.reports_dir / "pr-comment-fixes.md").write_text(fix_md, encoding="utf-8")
                return ActionResult(success=True, next_state=Stage.DEV, error="", metadata={})
            return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})

        # Escalate remaining
        pending_comments = []
        for cc in escalated:
            msg_id = await self._send_escalated_comment_tg(workspace, cc, pr_number)
            pending_comments.append({
                "comment_id": cc.comment_id, "msg_id": msg_id, "decision": None,
                "author": cc.author, "file": cc.file, "line": cc.line,
                "body": cc.body, "reason": cc.reason,
            })
        state.pending_review_comments = pending_comments
        workspace.save_state()
        return ActionResult(success=False, next_state="", error="", metadata={}, skipped=True)
```

- [ ] **Step 2: Add helper methods**

Add to the Orchestrator class:

```python
    @staticmethod
    def _git_diff_files(workspace: Workspace) -> set[str]:
        """Get set of files changed in the latest commit."""
        import subprocess
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace.source_dir), "diff", "HEAD~1", "--name-only"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return set(result.stdout.strip().splitlines())
        except Exception:
            pass
        return set()

    @staticmethod
    def _git_head_sha(workspace: Workspace) -> str:
        """Get current HEAD sha."""
        import subprocess
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace.source_dir), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return "unknown"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/unit/ -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add orchestrator/orchestrator.py
git commit -m "feat: rewrite PR review with resolution report as source of truth"
```

---

## Task 4: Rewrite `_execute_review_decisions` with resolution report

**Files:**
- Modify: `orchestrator/orchestrator.py:1346`

- [ ] **Step 1: Rewrite the method**

Replace `_execute_review_decisions`:

```python
    async def _execute_review_decisions(self, workspace: Workspace) -> ActionResult:
        """Execute all pending review decisions and record in resolution report."""
        from orchestrator.resolution_report import add_entry, read_entries

        state = workspace.state
        pending = state.pending_review_comments or []
        pr_number = state.pr_number
        report_path = workspace.reports_dir / "pr-review-resolution.md"

        vcs, _ = self._get_vcs_for_workspace(workspace)
        fixes_needed = []
        wont_fix = []
        skipped_comments = []

        def _is_fix(d: str) -> bool:
            d = d.lower().strip()
            return d in ("fix", "fxi", "fifx", "fixx", "fx", "fi", "yes", "fix it")

        for c in pending:
            decision = (c.get("decision") or "").lower().strip()
            if _is_fix(decision):
                fixes_needed.append(c)
                add_entry(report_path, state.ticket_id, pr_number, c["comment_id"], {
                    "file": f"{c.get('file', '?')}:{c.get('line', '?')}",
                    "author": f"@{c.get('author', '?')}",
                    "body": c.get("body", "")[:200],
                    "decision": "FIX",
                    "decided_by": "operator",
                    "verified": "PENDING",
                    "fail_count": "0",
                })
            elif decision.startswith("won't fix") or decision.startswith("wont fix"):
                reason = decision.split(":", 1)[1].strip() if ":" in decision else "Operator decision"
                wont_fix.append(c)
                add_entry(report_path, state.ticket_id, pr_number, c["comment_id"], {
                    "file": f"{c.get('file', '?')}:{c.get('line', '?')}",
                    "author": f"@{c.get('author', '?')}",
                    "body": c.get("body", "")[:200],
                    "decision": "WON'T_FIX",
                    "decided_by": "operator",
                    "reason": reason,
                    "github_reply": f"Won't fix: {reason}",
                    "resolved": "YES",
                })
                if vcs and pr_number:
                    try:
                        await vcs.reply_to_comment(pr_number, c["comment_id"], f"Won't fix: {reason}")
                        await vcs.resolve_comment(pr_number, c["comment_id"])
                    except Exception as e:
                        logger.warning("Failed to reply/resolve %d: %s", c["comment_id"], e)
            else:
                skipped_comments.append(c)
                add_entry(report_path, state.ticket_id, pr_number, c["comment_id"], {
                    "file": f"{c.get('file', '?')}:{c.get('line', '?')}",
                    "author": f"@{c.get('author', '?')}",
                    "body": c.get("body", "")[:200],
                    "decision": "SKIP",
                    "decided_by": "operator",
                })

        if fixes_needed:
            fix_md = "# PR Comment Fixes Required\n\n"
            for f in fixes_needed:
                fix_md += f"## Fix: {f['file']}:{f.get('line', '?')}\n"
                fix_md += f"Comment by @{f['author']}: {f['body'][:200]}\n"
                fix_md += f"Reason: {f['reason']}\n\n"
            (workspace.reports_dir / "pr-comment-fixes.md").write_text(fix_md, encoding="utf-8")

        state.pending_review_comments = None
        workspace.save_state()

        if fixes_needed:
            return ActionResult(success=True, next_state=Stage.DEV, error="", metadata={})

        if skipped_comments:
            if self._notifier:
                chat_id = self._get_chat_id(workspace)
                if chat_id:
                    sep = "─" * 30
                    lines = [f"⚠️ [{state.company_id}/{state.repo_id}] {state.ticket_id}"]
                    lines.append(sep)
                    lines.append(f"{len(skipped_comments)} comment(s) skipped (unresolved on GitHub):")
                    for sc in skipped_comments:
                        lines.append(f"  • @{sc.get('author','?')} on {sc.get('file','?')}:{sc.get('line','?')}")
                    lines.append(sep)
                    await self._notifier.send_message(chat_id, "\n".join(lines))

        return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/unit/ -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add orchestrator/orchestrator.py
git commit -m "feat: _execute_review_decisions writes to resolution report"
```

---

## Task 5: Add push verification

**Files:**
- Modify: `orchestrator/orchestrator.py:1072` (`_action_push_and_open_pr`)

- [ ] **Step 1: Replace the push-to-existing-PR block**

In `_action_push_and_open_pr`, replace the existing-PR handler to verify fixes after push:

```python
        # If PR already exists, push new commits and verify fixes
        if state.pr_number and state.pr_url:
            vcs, repo_config = self._get_vcs_for_workspace(workspace)
            if vcs:
                try:
                    branch = state.branch
                    if branch:
                        await vcs.push(str(workspace.source_dir), branch, force=True)
                        logger.info("Pushed updates to existing PR #%d for %s", state.pr_number, state.ticket_id)
                except Exception as e:
                    logger.warning("Failed to push to existing PR: %s", e)

            # Verify fixes: check git diff against PENDING entries in resolution report
            from orchestrator.resolution_report import read_entries, update_entry
            report_path = workspace.reports_dir / "pr-review-resolution.md"
            entries = read_entries(report_path)
            diff_files = self._git_diff_files(workspace)
            sha = self._git_head_sha(workspace)

            verified_count = 0
            for cid, entry in entries.items():
                if entry.get("verified") != "PENDING":
                    continue
                target_file = entry.get("file", "").split(":")[0]
                if any(f.endswith(target_file) or target_file.endswith(f) for f in diff_files):
                    update_entry(report_path, cid, {
                        "verified": "YES",
                        "verified_at": self._now(),
                        "verify_commit": sha[:8],
                        "github_reply": f"Fixed in commit {sha[:8]}",
                        "resolved": "YES",
                    })
                    if vcs:
                        try:
                            await vcs.reply_to_comment(state.pr_number, cid, f"Fixed in commit {sha[:8]}.")
                            await vcs.resolve_comment(state.pr_number, cid)
                        except Exception as e:
                            logger.warning("Failed to reply/resolve %d: %s", cid, e)
                    verified_count += 1
                    self._log_pipeline(workspace, f"Verified fix: comment #{cid} ({target_file} in diff)")
                else:
                    fail_count = int(entry.get("fail_count", "0")) + 1
                    update_entry(report_path, cid, {"verified": "FAILED", "fail_count": str(fail_count)})
                    self._log_pipeline(workspace, f"Fix NOT verified: comment #{cid} ({target_file} not in diff, attempt {fail_count})")

            return ActionResult(
                success=True, next_state=Stage.PR_REVIEW, error="",
                metadata={"pr_url": state.pr_url, "pr_number": state.pr_number},
            )
```

- [ ] **Step 2: Remove `comments_to_resolve` from WorkspaceState**

In `workspace/workspace.py`, remove line 80:

```python
    comments_to_resolve: list[int] | None = None
```

- [ ] **Step 3: Remove old `_write_resolution_report` function**

Delete the module-level `_write_resolution_report` function (starting at line ~1795) from `orchestrator/orchestrator.py` — it's replaced by the resolution_report module.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/orchestrator.py workspace/workspace.py
git commit -m "feat: verify fixes via git diff after push, remove old resolution writer"
```

---

## Task 6: Enrich pipeline log + final integration

**Files:**
- Modify: `orchestrator/orchestrator.py` (pipeline log entries)
- Modify: `docs/features/orchestrator.md`

- [ ] **Step 1: Update pipeline log entries**

Find all `self._log_pipeline` calls and ensure they include commit hashes where relevant. In `_handle_agent_stage`, after agent completes:

```python
        # Include commit hash if stage produced a commit
        sha_info = ""
        if stage_id == "dev":
            sha = self._git_head_sha(workspace)
            if sha != "unknown":
                sha_info = f" Commit: {sha[:8]}."
        self._log_pipeline(workspace, f"{stage_id} ({stage_def.agent}) completed.{sha_info} Output: `reports/{stage_def.agent}-output.md`")
```

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/unit/ -v`
Expected: ALL PASS

- [ ] **Step 3: Run linter**

Run: `ruff check orchestrator/resolution_report.py orchestrator/orchestrator.py`

- [ ] **Step 4: Update feature docs**

Append to `docs/features/orchestrator.md` changelog.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/orchestrator.py docs/features/orchestrator.md
git commit -m "feat: enriched pipeline log with commit hashes and resolution references"
```
