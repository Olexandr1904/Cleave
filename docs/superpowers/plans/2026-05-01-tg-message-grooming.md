# TG Message Grooming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Standardize all Telegram ticket messages with a strict header format, strip markdown from all outgoing text, and fix terse/misleading message bodies.

**Architecture:** New `orchestrator/tg_format.py` provides `tg_header`, `read_ticket_title`, and `strip_markdown`. Orchestrator and command handler import from it. Dead `skip` callback removed. Three doc gaps patched in `docs/telegram.md`.

**Tech Stack:** Python 3.11, pytest, python-telegram-bot; no new dependencies.

---

## File Map

| File | Action |
|---|---|
| `docs/telegram.md` | Add `skip` reply keyword + PR_REVIEW reply anchor docs |
| `orchestrator/tg_format.py` | CREATE — header builder, title reader, markdown stripper |
| `orchestrator/orchestrator.py` | Remove `_tg_header`/`_get_ticket_title`; update 15 message sites; body copy rewrites |
| `orchestrator/escalation_view.py` | Use `tg_header`; remove backtick formatting |
| `integrations/telegram/command_handler.py` | Remove dead `skip` callback; add `tg_header` to 12 confirmation messages |
| `tests/unit/test_tg_format.py` | CREATE — unit tests for new module |
| `tests/unit/test_escalation_view.py` | Add header format + no-backtick assertions |

---

## Task 1: Docs + Dead Code

**Files:**
- Modify: `docs/telegram.md`
- Modify: `integrations/telegram/command_handler.py:809-827`

- [ ] **Step 1: Add `skip` and PR_REVIEW reply docs to telegram.md**

In `docs/telegram.md`, find the "Replies and unblock flow" section. The current text ends the section after describing `retry`. Replace that passage so it reads:

```
- The reply text is stored on `state.human_input_reply` and made available as additional context when the agent resumes.
- The workspace transitions out of BLOCKED back to its `previous_state` and the orchestrator wakes immediately.
- If you reply with `retry`, the workspace re-enters its `previous_state` without adding context — useful when the agent just needs another shot.
- If you reply with `skip`, the workspace is advanced to the next stage (e.g. ANALYSIS → DEV) without re-running the blocked stage — useful when you want to move past a stuck agent rather than give it another shot.

The PR_REVIEW notification message (the one with the Review Complete button) is also a reply anchor. Replying to it with any text signals that you have finished reviewing and the pipeline should fetch PR comments. This is equivalent to tapping the Review Complete button.
```

- [ ] **Step 2: Remove the dead `skip` callback block from command_handler.py**

In `integrations/telegram/command_handler.py`, delete the entire `elif action == "skip":` block (currently lines 809–827):

```python
        elif action == "skip":
            ws = next((w for w in workspaces if w.state.ticket_id == ticket_id), None)
            if not ws:
                await self._notifier.send_message(chat_id, f"No active workspace found for {ticket_id}.", reply_to_message_id=message_id)
                return
            _NEXT = {
                Stage.ANALYSIS: Stage.DEV, Stage.DEV: Stage.SCOPE_CHECK,
                Stage.SCOPE_CHECK: Stage.QA, Stage.QA: Stage.PUSHED,
                Stage.PUSHED: Stage.PR_REVIEW, Stage.PR_REVIEW: Stage.DONE,
            }
            prev = ws.state.previous_state or ws.state.current_state
            target = _NEXT.get(prev, Stage.DONE)
            ws.state.human_input_pending = False
            ws.state.error = None
            ws.transition(target)
            ws.save_state()
            await self._notifier.send_message(chat_id, f"Skipped {prev} for {ticket_id}. Advanced to {target}.", reply_to_message_id=message_id)
            if hasattr(self, '_wake_fn') and self._wake_fn:
                self._wake_fn()
```

The `elif action == "clear_gradle":` block immediately following becomes the next branch after `elif action == "retry":`.

- [ ] **Step 3: Run tests**

```bash
pytest tests/unit/test_command_handler.py -v
```

Expected: all existing tests pass.

- [ ] **Step 4: Commit**

```bash
git add docs/telegram.md integrations/telegram/command_handler.py
git commit -m "docs: fix 3 tg docs gaps; remove dead skip callback"
```

---

## Task 2: Create orchestrator/tg_format.py (TDD)

**Files:**
- Create: `tests/unit/test_tg_format.py`
- Create: `orchestrator/tg_format.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_tg_format.py`:

```python
"""Tests for orchestrator/tg_format.py."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.tg_format import read_ticket_title, strip_markdown, tg_header

_SEP = "_" * 30


class TestTgHeader:
    def test_with_emoji_and_title(self):
        result = tg_header("❌", "acme", "T-123", "Fix login crash")
        assert result == f"❌ [acme] T-123\nFix login crash\n{_SEP}"

    def test_without_title(self):
        result = tg_header("✅", "acme", "T-123")
        assert result == f"✅ [acme] T-123\n{_SEP}"

    def test_empty_title_omits_title_line(self):
        result = tg_header("✅", "acme", "T-123", "")
        lines = result.splitlines()
        assert lines[0] == "✅ [acme] T-123"
        assert lines[1] == _SEP
        assert len(lines) == 2

    def test_no_emoji_omits_leading_space(self):
        result = tg_header("", "acme", "T-123", "Some title")
        assert result.startswith("[acme]")
        assert not result.startswith(" ")

    def test_separator_is_exactly_30_underscores(self):
        result = tg_header("✅", "acme", "T-1")
        sep_line = result.splitlines()[-1]
        assert sep_line == _SEP
        assert len(sep_line) == 30

    def test_project_id_no_repo(self):
        result = tg_header("🔔", "myproject", "TICK-99", "Some issue")
        assert "[myproject]" in result
        assert "/" not in result.splitlines()[0]


class TestStripMarkdown:
    def test_removes_inline_backticks(self):
        assert strip_markdown("`fix`") == "fix"

    def test_removes_double_backtick_code(self):
        assert strip_markdown("send `retry T-1 from dev` to restart") == "send retry T-1 from dev to restart"

    def test_removes_bold(self):
        assert strip_markdown("**Status: PASS**") == "Status: PASS"

    def test_removes_bold_with_surrounding_text(self):
        assert strip_markdown("Result: **PASS** confirmed") == "Result: PASS confirmed"

    def test_removes_triple_backtick_blocks(self):
        text = "```python\ncode here\n```"
        result = strip_markdown(text)
        assert "```" not in result
        assert "code here" in result

    def test_removes_table_separator_row(self):
        text = "| Check | Result |\n|---|---|\n| Files | None |"
        result = strip_markdown(text)
        assert "|---|" not in result
        assert "---|" not in result

    def test_converts_table_row_to_plain(self):
        text = "| Unauthorized files | None |"
        result = strip_markdown(text)
        assert "|" not in result
        assert "Unauthorized files" in result
        assert "None" in result

    def test_removes_heading_hashes(self):
        assert strip_markdown("## Summary") == "Summary"
        assert strip_markdown("# Title") == "Title"

    def test_leaves_unicode_bullets_untouched(self):
        text = "• item one\n• item two"
        assert strip_markdown(text) == text

    def test_leaves_box_drawing_untouched(self):
        text = "─" * 30
        assert strip_markdown(text) == text

    def test_leaves_emojis_untouched(self):
        text = "✅ done\n❌ failed"
        assert strip_markdown(text) == text

    def test_leaves_arrows_untouched(self):
        assert strip_markdown("ANALYSIS → DEV") == "ANALYSIS → DEV"

    def test_empty_string(self):
        assert strip_markdown("") == ""

    def test_plain_text_unchanged(self):
        text = "Pipeline stuck at ANALYSIS. Check reports/ for details."
        assert strip_markdown(text) == text

    def test_multiline_table(self):
        text = (
            "The diff was clean:\n\n"
            "| Check | Result |\n"
            "|---|---|\n"
            "| Unauthorized files | None |\n"
            "| Protected files | None |\n\n"
            "Advances to QA."
        )
        result = strip_markdown(text)
        assert "|" not in result
        assert "Unauthorized files" in result
        assert "None" in result
        assert "Advances to QA." in result


class TestReadTicketTitle:
    def test_reads_summary_field(self, tmp_path):
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()
        (meta_dir / "ticket.json").write_text(
            json.dumps({"summary": "Fix login crash on Samsung", "key": "T-1"}),
            encoding="utf-8",
        )
        ws = SimpleNamespace(meta_dir=meta_dir)
        assert read_ticket_title(ws) == "Fix login crash on Samsung"

    def test_returns_empty_when_file_missing(self, tmp_path):
        ws = SimpleNamespace(meta_dir=tmp_path / "nonexistent")
        assert read_ticket_title(ws) == ""

    def test_returns_empty_on_corrupt_json(self, tmp_path):
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()
        (meta_dir / "ticket.json").write_text("not valid json", encoding="utf-8")
        ws = SimpleNamespace(meta_dir=meta_dir)
        assert read_ticket_title(ws) == ""

    def test_returns_empty_when_summary_key_missing(self, tmp_path):
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()
        (meta_dir / "ticket.json").write_text(
            json.dumps({"key": "T-1", "status": "Open"}), encoding="utf-8",
        )
        ws = SimpleNamespace(meta_dir=meta_dir)
        assert read_ticket_title(ws) == ""

    def test_handles_no_meta_dir_attribute(self):
        ws = SimpleNamespace()  # no meta_dir attribute
        assert read_ticket_title(ws) == ""
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_tg_format.py -v
```

Expected: `ModuleNotFoundError: No module named 'orchestrator.tg_format'`

- [ ] **Step 3: Implement orchestrator/tg_format.py**

Create `orchestrator/tg_format.py`:

```python
"""Shared Telegram message formatting helpers.

Extracted so Orchestrator and CommandHandler produce consistent ticket
headers without depending on each other.
"""
from __future__ import annotations

import json
import re
from typing import Any

_SEP = "_" * 30
_BOX_SEP = "─" * 30


def tg_header(emoji: str, project_id: str, ticket_id: str, title: str = "") -> str:
    """Build the standard ticket message header.

    Format (with title):
        ❌ [project_id] ticket_id
        Ticket title here
        ______________________________

    Format (no title):
        ❌ [project_id] ticket_id
        ______________________________

    Emoji prefix is omitted (no leading space) when emoji is empty string.
    """
    first = f"{emoji} [{project_id}] {ticket_id}" if emoji else f"[{project_id}] {ticket_id}"
    parts = [first]
    if title:
        parts.append(title)
    parts.append(_SEP)
    return "\n".join(parts)


def read_ticket_title(workspace: Any) -> str:
    """Read ticket summary from meta/ticket.json. Returns empty string on any failure."""
    try:
        ticket_file = workspace.meta_dir / "ticket.json"
        if ticket_file.exists():
            data = json.loads(ticket_file.read_text(encoding="utf-8"))
            return data.get("summary", "")
    except Exception:
        pass
    return ""


def strip_markdown(text: str) -> str:
    """Remove markdown syntax that renders as raw characters in Telegram.

    Handles: **bold**, `inline code`, ```blocks```, | tables |,
    |---|---| separator rows, ## headings.
    Does not touch Unicode (bullets •, box-drawing ─, emojis, arrows →).
    """
    if not text:
        return text

    # Triple-backtick code blocks (with optional language tag)
    text = re.sub(r"```[^\n]*\n?", "", text)

    # Inline backticks
    text = re.sub(r"`([^`\n]*)`", r"\1", text)

    # Bold: **text**
    text = re.sub(r"\*\*([^*\n]*)\*\*", r"\1", text)

    # Italic: *text* (single asterisk, not touching **)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)

    # Headings: ## Title → Title
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)

    # Table separator rows: |---|---| → remove line entirely
    text = re.sub(r"(?m)^\|[-| :]+\|\s*$\n?", "", text)

    # Table data rows: | A | B | → A — B
    def _table_row(m: re.Match) -> str:
        cells = [c.strip() for c in m.group(0).split("|") if c.strip()]
        return " — ".join(cells)

    text = re.sub(r"(?m)^\|.+\|.*$", _table_row, text)

    # Collapse 3+ blank lines left by removed blocks
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/test_tg_format.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/tg_format.py tests/unit/test_tg_format.py
git commit -m "feat: add tg_format module (tg_header, read_ticket_title, strip_markdown)"
```

---

## Task 3: Update orchestrator.py

**Files:**
- Modify: `orchestrator/orchestrator.py`

This task replaces every `_tg_header` / `_get_ticket_title` call, fixes four hardcoded formats, applies `strip_markdown` to agent output, and rewrites terse message bodies. Work through the file top-to-bottom.

- [ ] **Step 1: Add import and remove helper methods**

At the top of `orchestrator/orchestrator.py`, add after the existing imports block:

```python
from orchestrator import tg_format
```

Then delete the two static methods (around lines 350–367):

```python
    @staticmethod
    def _get_ticket_title(workspace: Workspace) -> str:
        """Read ticket summary from meta/ticket.json, or return empty string."""
        ticket_file = workspace.meta_dir / "ticket.json"
        if ticket_file.exists():
            try:
                data = json.loads(ticket_file.read_text(encoding="utf-8"))
                return data.get("summary", "")
            except (json.JSONDecodeError, KeyError):
                pass
        return ""

    @staticmethod
    def _tg_header(emoji: str, state: Any, title: str) -> str:
        """Build a standard TG message header line, including ticket title if available."""
        header = f"{emoji} [{state.company_id}/{state.repo_id}] {state.ticket_id}"
        if title:
            header += f"\n{title}"
        return header
```

- [ ] **Step 2: Update QA warnings message (around line 968)**

Replace:

```python
                    sep = "─" * 30
                    title = self._get_ticket_title(workspace)
                    hdr = self._tg_header("⚠️", state, title)
                    await self._notifier.send_message(chat_id, (
                        f"{hdr}\n"
                        f"{sep}\n"
                        f"QA passed but with warnings:\n"
                        + "\n".join(f"  • {w}" for w in warnings)
                        + f"\n\nCI on GitHub will be the authoritative gate.\n"
                        f"{sep}"
                    ))
```

With:

```python
                    title = tg_format.read_ticket_title(workspace)
                    hdr = tg_format.tg_header("⚠️", state.company_id, state.ticket_id, title)
                    await self._notifier.send_message(chat_id, (
                        f"{hdr}\n"
                        f"QA passed but with warnings:\n"
                        + "\n".join(f"  • {w}" for w in warnings)
                        + f"\n\nCI on GitHub will be the authoritative gate."
                    ))
```

- [ ] **Step 3: Update `_notify_deferred` (around line 1055)**

Replace:

```python
        hdr = self._tg_header("⏱", state, title)
```

With:

```python
        hdr = tg_format.tg_header("⏱", state.company_id, state.ticket_id, title)
```

Also replace the `_get_ticket_title` call above it:

```python
        title = self._get_ticket_title(workspace)
```

With:

```python
        title = tg_format.read_ticket_title(workspace)
```

- [ ] **Step 4: Update `_notify_failed` (around line 1093) — all three variants**

Replace:

```python
        title = self._get_ticket_title(workspace)
        hdr = self._tg_header("❌", state, title)
        first_line = (error or "").splitlines()[0] if error else ""
        buttons = [Button(label="Retry", action=f"retry:{state.ticket_id}")]
        if looks_like_aapt2_arch_mismatch(error):
            sep = "─" * 30
            msg = (
                f"{hdr}\n"
                f"FAILED at {state.previous_state or '?'}.\n"
                f"⚠️ Architecture mismatch (x86-64 aapt2 on non-x86 host).\n"
                f"{sep}\n"
                f"{ARCH_MISMATCH_HELP}"
            )
        elif looks_like_gradle_cache_corruption(error):
            msg = (
                f"{hdr}\n"
                f"FAILED at {state.previous_state or '?'}. Error: {first_line}.\n"
                f"Detected Gradle cache corruption — tap below to clear it."
            )
            buttons.insert(
                0,
                Button(label="🧹 Clear cache & retry", action=f"clear_gradle:{state.ticket_id}"),
            )
        else:
            msg = (
                f"{hdr}\n"
                f"FAILED at {state.previous_state or '?'}. Error: {first_line}."
            )
```

With:

```python
        title = tg_format.read_ticket_title(workspace)
        hdr = tg_format.tg_header("❌", state.company_id, state.ticket_id, title)
        first_line = (error or "").splitlines()[0] if error else ""
        buttons = [Button(label="Retry", action=f"retry:{state.ticket_id}")]
        stage = state.previous_state or "?"
        sep_inner = "─" * 30
        if looks_like_aapt2_arch_mismatch(error):
            msg = (
                f"{hdr}\n"
                f"FAILED at {stage}.\n"
                f"Architecture mismatch (x86-64 aapt2 on non-x86 host).\n"
                f"{sep_inner}\n"
                f"{ARCH_MISMATCH_HELP}"
            )
        elif looks_like_gradle_cache_corruption(error):
            msg = (
                f"{hdr}\n"
                f"FAILED at {stage}. Error: {first_line}.\n"
                f"Detected Gradle cache corruption — tap below to clear it."
            )
            buttons.insert(
                0,
                Button(label="🧹 Clear cache & retry", action=f"clear_gradle:{state.ticket_id}"),
            )
        else:
            msg = (
                f"{hdr}\n"
                f"FAILED at {stage}.\n\n"
                f"Reason: {first_line}\n\n"
                f"Options:\n"
                f"- Tap Retry to re-run from {stage}\n"
                f"- Send \"retry {state.ticket_id} from dev\" to restart from an earlier stage"
            )
```

Note: `sep_inner = "─" * 30` is defined just before the `if` block and is used only in the arch-mismatch body separator.

- [ ] **Step 5: Update `_notify_rerun` (around line 1140)**

Replace:

```python
        title = self._get_ticket_title(workspace)
        hdr = self._tg_header("🔄", state, title)
```

With:

```python
        title = tg_format.read_ticket_title(workspace)
        hdr = tg_format.tg_header("🔄", state.company_id, state.ticket_id, title)
```

- [ ] **Step 6: Update PR created message (around line 1332) — with body rewrite**

Replace:

```python
                title = self._get_ticket_title(workspace)
                hdr = self._tg_header("🔗", state, title)
                msg = (
                    f"{hdr}\n"
                    f"{sep}\n"
                    f"PR created: {pr_url}\n\n"
                    f"Please review the code."
                )
```

With:

```python
                title = tg_format.read_ticket_title(workspace)
                hdr = tg_format.tg_header("🔗", state.company_id, state.ticket_id, title)
                msg = (
                    f"{hdr}\n"
                    f"PR opened: {pr_url}\n\n"
                    f"Review the diff and merge when ready. The pipeline will wait.\n\n"
                    f"If there are review comments, Sickle will escalate them one by one "
                    f"for your decision (Fix or Won't Fix). Reply to any escalation message "
                    f"to provide context.\n\n"
                    f"When done: tap Review Complete or reply to this message."
                )
```

Also remove the `sep = "─" * 30` line in this block (it is no longer used after removing `{sep}\n`).

- [ ] **Step 7: Update `_build_gate_summary` (around line 2421) — both branches**

Replace both occurrences of `self._tg_header` and `self._get_ticket_title` in this method:

```python
        title = self._get_ticket_title(workspace)
```
→
```python
        title = tg_format.read_ticket_title(workspace)
```

Replace the PR_REVIEW branch header:

```python
            hdr = self._tg_header("⏸", state, title)
            text = (
                f"{hdr}\n"
                f"{sep}\n"
                f"PR: {state.pr_url or 'N/A'}\n"
                f"{summary}"
            )
```

With:

```python
            hdr = tg_format.tg_header("⏸", state.company_id, state.ticket_id, title)
            text = (
                f"{hdr}\n"
                f"PR: {state.pr_url or 'N/A'}\n"
                f"{summary}"
            )
```

Replace the ANALYSIS/QA/other branch header:

```python
        hdr = self._tg_header("⏸", state, title)
        text = (
            f"{hdr}\n"
            f"{sep}\n"
            f"{gate_title}"
        )
```

With:

```python
        hdr = tg_format.tg_header("⏸", state.company_id, state.ticket_id, title)
        text = (
            f"{hdr}\n"
            f"{gate_title}"
        )
```

Also wrap the BA summary extraction with `strip_markdown`. Find the line:

```python
                        summary = line.strip()[:200]
```

Change to:

```python
                        summary = tg_format.strip_markdown(line.strip()[:200])
```

- [ ] **Step 8: Update `_handle_escalate` (around line 2263) — strip markdown + fix backticks**

Replace:

```python
        title = self._get_ticket_title(workspace)
        hdr = self._tg_header("🔔", state, title)
        stage_id_str = stage.lower() if isinstance(stage, str) else str(stage).lower()
        if is_max_iterations:
            stage_def = self._workflow.stages.get(stage_id_str)
            cap = stage_def.max_iterations if stage_def else "?"
            iterations = state.stage_iterations.get(stage_id_str, 0)
            header = f"{hdr}\nStage: {stage} — iteration limit reached ({iterations}/{cap})\n{sep}\n"
        else:
            header = f"{hdr}\nStage: {stage}\n{sep}\n"

        reason = self._build_blocked_reason(workspace, stage_id_str)
        if is_max_iterations:
            hint = (
                f"\n{sep}\n"
                f"↩️ Reply with context to resume, or send:\n"
                f"  `retry {state.ticket_id} from {stage_id_str}` — reset counter and re-run\n"
                f"  `retry {state.ticket_id} from dev` — restart from dev"
            )
        else:
            hint = f"\n{sep}\n↩️ Reply with your answer or additional context."
```

With:

```python
        title = tg_format.read_ticket_title(workspace)
        hdr = tg_format.tg_header("🔔", state.company_id, state.ticket_id, title)
        stage_id_str = stage.lower() if isinstance(stage, str) else str(stage).lower()
        if is_max_iterations:
            stage_def = self._workflow.stages.get(stage_id_str)
            cap = stage_def.max_iterations if stage_def else "?"
            iterations = state.stage_iterations.get(stage_id_str, 0)
            header = f"{hdr}\nStage: {stage} — iteration limit reached ({iterations}/{cap})\n"
        else:
            header = f"{hdr}\nStage: {stage}\n"

        reason = tg_format.strip_markdown(self._build_blocked_reason(workspace, stage_id_str))
        if is_max_iterations:
            hint = (
                f"\n{sep}\n"
                f"↩️ Reply with context to resume, or send:\n"
                f"  retry {state.ticket_id} from {stage_id_str} — reset counter and re-run\n"
                f"  retry {state.ticket_id} from dev — restart from dev"
            )
        else:
            hint = f"\n{sep}\n↩️ Reply with your answer or additional context."
```

- [ ] **Step 9: Update `_notify_verification_blocked` (around line 2325)**

Replace:

```python
        title = self._get_ticket_title(workspace)
        hdr = self._tg_header("⚠️", workspace.state, title)
        ...
        header = f"{hdr}\nStage: {stage_id} — verification failed\n{sep}\n"
        ...
        agent_reason = self._build_blocked_reason(workspace, stage_id)
        combined = f"Verification failed: {verify_reason}\n\n{agent_reason}"
```

With:

```python
        title = tg_format.read_ticket_title(workspace)
        hdr = tg_format.tg_header("⚠️", workspace.state.company_id, workspace.state.ticket_id, title)
        ...
        header = f"{hdr}\nStage: {stage_id} — verification failed\n"
        ...
        agent_reason = tg_format.strip_markdown(self._build_blocked_reason(workspace, stage_id))
        combined = f"Verification failed: {verify_reason}\n\n{agent_reason}"
```

- [ ] **Step 10: Update `_on_ticket_done` (around line 2130) — hardcoded header + body rewrite**

Replace:

```python
                sep = "─" * 30
                await self._notifier.send_message(chat_id, (
                    f"✅ [{state.company_id}/{state.repo_id}] {state.ticket_id}\n"
                    f"{sep}\n"
                    f"Pipeline complete. PR ready for merge:\n"
                    f"{state.pr_url or 'N/A'}\n"
                    f"{sep}"
                ))
```

With:

```python
                title = tg_format.read_ticket_title(workspace)
                hdr = tg_format.tg_header("✅", state.company_id, state.ticket_id, title)
                await self._notifier.send_message(chat_id, (
                    f"{hdr}\n"
                    f"Pipeline complete.\n\n"
                    f"PR ready for merge: {state.pr_url or 'N/A'}\n\n"
                    f"Jira ticket moved to review status."
                ))
```

- [ ] **Step 11: Update PR auto-processed summary (around line 1904) — hardcoded header**

Replace:

```python
                sep = "─" * 30
                lines = [f"🤖 [{state.company_id}/{state.repo_id}] {state.ticket_id} — PR #{pr_number}"]
                lines.append(f"Auto-processed {len(auto_fixed) + len(auto_rejected)} comment(s):")
                lines.append(sep)
```

With:

```python
                title = tg_format.read_ticket_title(workspace)
                hdr = tg_format.tg_header("🤖", state.company_id, state.ticket_id, title)
                sep = "─" * 30
                lines = [hdr]
                lines.append(f"PR #{pr_number} — Auto-processed {len(auto_fixed) + len(auto_rejected)} comment(s):")
                lines.append(sep)
```

- [ ] **Step 12: Update dev-agent fix failed twice (around line 1764) — header + body rewrite**

Replace:

```python
                        await self._notifier.send_message(
                            chat_id,
                            f"⚠️ [{state.company_id}/{state.repo_id}] {state.ticket_id}\n"
                            f"Dev-agent failed to fix comment #{cid} twice "
                            f"({entry.get('file', '?')}:{entry.get('line', '?')})",
                        )
```

With:

```python
                        title = tg_format.read_ticket_title(workspace)
                        hdr = tg_format.tg_header("⚠️", state.company_id, state.ticket_id, title)
                        await self._notifier.send_message(
                            chat_id,
                            f"{hdr}\n"
                            f"Dev-agent failed to apply the fix for comment #{cid} twice.\n\n"
                            f"File: {entry.get('file', '?')}:{entry.get('line', '?')}\n\n"
                            f"Options:\n"
                            f"- Reply \"fix\" to retry once more\n"
                            f"- Reply \"won't fix: <reason>\" to close the comment without fixing",
                        )
```

- [ ] **Step 13: Update re-investigation failed (around line 1658) — header + body rewrite**

Replace:

```python
                    if self._notifier:
                        chat_id = self._get_chat_id(workspace)
                        if chat_id:
                            await self._notifier.send_message(
                                chat_id,
                                f"⚠ Re-investigation failed for @{c.get('author','?')} "
                                f"on {c.get('file','?')}:{c.get('line','?')}. "
                                f"Reply `fix` or `won't fix` to close.",
                            )
```

With:

```python
                    if self._notifier:
                        chat_id = self._get_chat_id(workspace)
                        if chat_id:
                            title = tg_format.read_ticket_title(workspace)
                            hdr = tg_format.tg_header("⚠️", state.company_id, state.ticket_id, title)
                            await self._notifier.send_message(
                                chat_id,
                                f"{hdr}\n"
                                f"Re-investigation failed for @{c.get('author','?')}'s comment "
                                f"on {c.get('file','?')}:{c.get('line','?')}.\n\n"
                                f"The agent was unable to re-classify this comment after your hint. Options:\n"
                                f"- Reply \"fix\" to send the dev-agent in anyway\n"
                                f"- Reply \"won't fix: <reason>\" to close the comment on GitHub",
                            )
```

Note: `state` is available in this context as `workspace.state`. Verify the local variable name at that line — if only `workspace` is in scope, use `workspace.state.company_id` and `workspace.state.ticket_id`.

- [ ] **Step 14: Update PR review pause (around line 2090) — hardcoded header**

Replace:

```python
                    lines = [f"⏸ [{state.company_id}/{state.repo_id}] {state.ticket_id} — PR review pause"]
                    lines.append(sep)
                    lines.append(f"{len(skipped_comments)} comment(s) marked Skip — still open on the PR:")
```

With:

```python
                    title = tg_format.read_ticket_title(workspace)
                    hdr = tg_format.tg_header("⏸", state.company_id, state.ticket_id, title)
                    lines = [hdr]
                    lines.append(f"{len(skipped_comments)} comment(s) marked Skip — still open on the PR:")
```

The `sep = "─" * 30` and two `lines.append(sep)` calls that follow remain unchanged as internal section separators.

- [ ] **Step 15: Run existing tests**

```bash
pytest tests/unit/ -v -x
```

Expected: all tests pass. Fix any import errors or AttributeError from removed methods.

- [ ] **Step 16: Commit**

```bash
git add orchestrator/orchestrator.py
git commit -m "refactor: migrate orchestrator to tg_format; strip markdown from agent output; rewrite terse messages"
```

---

## Task 4: Update escalation_view.py

**Files:**
- Modify: `orchestrator/escalation_view.py`
- Modify: `tests/unit/test_escalation_view.py`

- [ ] **Step 1: Update escalation_view.py — header + backtick removal**

Replace the entire content of `orchestrator/escalation_view.py` with:

```python
"""Shared renderer for escalated PR comment TG messages.

Extracted so command_handler can re-send messages for the recall flow
without depending on Orchestrator internals. Accepts either a dict or
an attribute-style object (e.g., ClassifiedComment) for the comment.
"""
from __future__ import annotations

from typing import Any

from integrations.base.notifier import Button
from orchestrator import tg_format


def _g(o: Any, key: str, default: Any = None) -> Any:
    """Read key from dict-or-attribute object."""
    if isinstance(o, dict):
        return o.get(key, default)
    return getattr(o, key, default)


def build_escalated_comment_message(
    state: Any,
    cc: Any,
    pr_number: int,
    ticket_title: str = "",
    *,
    recall: bool = False,
) -> tuple[str, list[Button]]:
    """Build (text, buttons) for an escalated PR comment.

    cc may be a dict or an object with attributes. Required keys/attrs:
    comment_id, author, file, line, body, reason, verdict (optional).
    """
    sep = "─" * 30
    recall_prefix = "🔁 (still pending) " if recall else ""
    hdr = tg_format.tg_header(
        f"{recall_prefix}💬",
        state.company_id,
        state.ticket_id,
        ticket_title,
    )

    verdict = _g(cc, "verdict", "Unsure")
    reason = _g(cc, "reason", "") or ""
    if verdict in ("Valid", "Not valid"):
        assessment_line = f"  {verdict} — {reason}"
    else:
        assessment_line = f"  {reason}"

    body = _g(cc, "body", "") or ""
    text = (
        f"{hdr}\n"
        f"PR #{pr_number} — Comment by @{_g(cc, 'author', '?')} "
        f"on {_g(cc, 'file', '?')}:{_g(cc, 'line', '?')}\n"
        f"{sep}\n"
        f"Suggestion:\n  {body[:300]}\n\n"
        f"Agent assessment:\n{assessment_line}\n"
        f"{sep}\n"
        "Tap a button below, or reply to this message with:\n"
        "  - fix — re-engage dev-agent\n"
        "  - won't fix: <reason> — post the reason on GitHub and resolve\n"
        "  - free text — re-investigate with your hint\n"
    )

    comment_key = f"{state.ticket_id}:{_g(cc, 'comment_id')}"
    buttons = [
        Button(label="Fix", action=f"pr_fix:{comment_key}"),
        Button(label="Won't Fix", action=f"pr_wontfix:{comment_key}"),
    ]
    return text, buttons
```

- [ ] **Step 2: Update tests for escalation_view.py**

In `tests/unit/test_escalation_view.py`, add three new test methods to the `TestBuildMessage` class:

```python
    def test_header_uses_underscore_separator(self):
        text, _ = build_escalated_comment_message(
            _state(), _cc(), pr_number=42, ticket_title="A ticket",
        )
        assert "_" * 30 in text

    def test_no_backticks_in_message(self):
        text, _ = build_escalated_comment_message(
            _state(), _cc(), pr_number=42, ticket_title="A ticket",
        )
        assert "`" not in text

    def test_header_contains_ticket_and_project(self):
        text, _ = build_escalated_comment_message(
            _state(), _cc(), pr_number=42, ticket_title="Fix crash",
        )
        assert "[acme]" in text
        assert "T-1" in text
        assert "Fix crash" in text
```

Also update the recall test — the assertion still holds but verify it passes:

```python
    def test_recall_message_has_prefix(self):
        text, _ = build_escalated_comment_message(
            _state(), _cc(), pr_number=42, ticket_title="A ticket", recall=True,
        )
        assert text.startswith("🔁 (still pending)")
```

This passes unchanged because `tg_header("🔁 (still pending) 💬", ...)` produces text that starts with `🔁 (still pending)`.

- [ ] **Step 3: Run escalation_view tests**

```bash
pytest tests/unit/test_escalation_view.py -v
```

Expected: all tests pass including the three new ones.

- [ ] **Step 4: Commit**

```bash
git add orchestrator/escalation_view.py tests/unit/test_escalation_view.py
git commit -m "refactor: escalation_view uses tg_format header; remove backtick formatting"
```

---

## Task 5: Update command_handler.py — headers on all ticket confirmations

**Files:**
- Modify: `integrations/telegram/command_handler.py`

- [ ] **Step 1: Add import at top of command_handler.py**

After the existing imports in `integrations/telegram/command_handler.py`, add:

```python
from orchestrator import tg_format
```

- [ ] **Step 2: Add headers to FIX/WON'T FIX reply confirmations (around line 546)**

Find the `confirm` line and the two `send_message` calls that follow it (rows #23, #24):

```python
                ws.save_state()
                confirm = f"✓ Recognized as {recorded_label}. {decision_label}"
```

Replace with:

```python
                ws.save_state()
                _title = tg_format.read_ticket_title(ws)
                _emoji = "✅" if decision == "fix" else "❌"
                _hdr = tg_format.tg_header(_emoji, ws.state.company_id, ws.state.ticket_id, _title)
                confirm = f"{_hdr}\n✓ Recognized as {recorded_label}. {decision_label}"
```

- [ ] **Step 3: Add header to PR review signal reply (around line 590)**

Replace:

```python
                await self._notifier.send_message(
                    chat_id,
                    f"Got it. Fetching PR comments for {ws.state.ticket_id} now.",
                )
```

With:

```python
                _title = tg_format.read_ticket_title(ws)
                _hdr = tg_format.tg_header("🔍", ws.state.company_id, ws.state.ticket_id, _title)
                await self._notifier.send_message(
                    chat_id,
                    f"{_hdr}\nGot it. Fetching PR comments now.",
                )
```

- [ ] **Step 4: Add header to skip reply (around line 616)**

Replace:

```python
                    await self._notifier.send_message(
                        chat_id,
                        f"Skipped {prev} for {ws.state.ticket_id}. Advanced to {resume_state}.",
                    )
```

With:

```python
                    _title = tg_format.read_ticket_title(ws)
                    _hdr = tg_format.tg_header("⏭", ws.state.company_id, ws.state.ticket_id, _title)
                    await self._notifier.send_message(
                        chat_id,
                        f"{_hdr}\nSkipped {prev}. Advanced to {resume_state}.",
                    )
```

- [ ] **Step 5: Add header to retry reply (around line 630)**

Replace:

```python
                    await self._notifier.send_message(
                        chat_id,
                        f"Retrying {resume_state} for {ws.state.ticket_id}.",
                    )
```

With:

```python
                    _title = tg_format.read_ticket_title(ws)
                    _hdr = tg_format.tg_header("🔄", ws.state.company_id, ws.state.ticket_id, _title)
                    await self._notifier.send_message(
                        chat_id,
                        f"{_hdr}\nRetrying {resume_state}.",
                    )
```

- [ ] **Step 6: Add header to resume-with-input reply (around line 646)**

Replace:

```python
                await self._notifier.send_message(
                    chat_id,
                    f"Got it. Resuming {ws.state.ticket_id} from {resume_state} with your input.",
                )
```

With:

```python
                _title = tg_format.read_ticket_title(ws)
                _hdr = tg_format.tg_header("▶", ws.state.company_id, ws.state.ticket_id, _title)
                await self._notifier.send_message(
                    chat_id,
                    f"{_hdr}\nResuming from {resume_state} with your input.",
                )
```

- [ ] **Step 7: Add header to hint staged + remove ellipsis (around line 739)**

Replace:

```python
        msg = (
            f"🔍 Recognized as hint (round {next_round}/3). "
            f"Re-checking @{c.get('author','?')}'s comment on {file_line} with your context…"
        )
        await self._notifier.send_message(chat_id, msg)
```

With:

```python
        _title = tg_format.read_ticket_title(ws)
        _hdr = tg_format.tg_header("🔍", ws.state.company_id, ws.state.ticket_id, _title)
        msg = (
            f"{_hdr}\n"
            f"Recognized as hint (round {next_round}/3). "
            f"Re-checking @{c.get('author','?')}'s comment on {file_line} with your context."
        )
        await self._notifier.send_message(chat_id, msg)
```

- [ ] **Step 8: Add header to hint exhausted + remove backticks (around line 711)**

Replace:

```python
            msg = (
                f"⚠ Hint loop exceeded (3/3) for @{c.get('author','?')} on {file_line}. "
                f"Reply `fix` or `won't fix` to close this comment."
            )
            await self._notifier.send_message(chat_id, msg)
```

With:

```python
            _title = tg_format.read_ticket_title(ws)
            _hdr = tg_format.tg_header("⚠️", ws.state.company_id, ws.state.ticket_id, _title)
            msg = (
                f"{_hdr}\n"
                f"Hint limit reached (3/3) for @{c.get('author','?')} on {file_line}. "
                f"Reply \"fix\" or \"won't fix: <reason>\" to close this comment."
            )
            await self._notifier.send_message(chat_id, msg)
```

- [ ] **Step 9: Add header to approve callback confirm (around line 773)**

Replace:

```python
            await self._notifier.send_message(chat_id, f"Approved {ticket_id}. Moving to {next_state}.", reply_to_message_id=message_id)
```

With:

```python
            _title = tg_format.read_ticket_title(ws)
            _hdr = tg_format.tg_header("✅", ws.state.company_id, ws.state.ticket_id, _title)
            await self._notifier.send_message(chat_id, f"{_hdr}\nApproved. Moving to {next_state}.", reply_to_message_id=message_id)
```

- [ ] **Step 10: Add header to reject callback confirm (around line 782)**

Replace:

```python
            await self._notifier.send_message(chat_id, f"Rejected {ticket_id}. Marked as FAILED.", reply_to_message_id=message_id)
```

With:

```python
            _title = tg_format.read_ticket_title(ws)
            _hdr = tg_format.tg_header("❌", ws.state.company_id, ws.state.ticket_id, _title)
            await self._notifier.send_message(chat_id, f"{_hdr}\nRejected. Marked as FAILED.", reply_to_message_id=message_id)
```

- [ ] **Step 11: Add header to reviewed callback confirm (around line 791)**

Replace:

```python
            await self._notifier.send_message(chat_id, f"Got it. Fetching PR comments for {ticket_id} now.", reply_to_message_id=message_id)
```

With:

```python
            _title = tg_format.read_ticket_title(ws)
            _hdr = tg_format.tg_header("🔍", ws.state.company_id, ws.state.ticket_id, _title)
            await self._notifier.send_message(chat_id, f"{_hdr}\nGot it. Fetching PR comments now.", reply_to_message_id=message_id)
```

- [ ] **Step 12: Add header to retry callback confirm (around line 805)**

Replace:

```python
            await self._notifier.send_message(chat_id, f"Retrying {ticket_id} from {target_state}.", reply_to_message_id=message_id)
```

With:

```python
            _title = tg_format.read_ticket_title(ws)
            _hdr = tg_format.tg_header("🔄", ws.state.company_id, ws.state.ticket_id, _title)
            await self._notifier.send_message(chat_id, f"{_hdr}\nRetrying from {target_state}.", reply_to_message_id=message_id)
```

- [ ] **Step 13: Add header to clear_gradle success (around line 861)**

Replace:

```python
            await self._notifier.send_message(
                chat_id,
                f"Cleared {mb:.0f} MB of Gradle transforms cache. Retrying {ticket_id} from {target}.",
                reply_to_message_id=message_id,
            )
```

With:

```python
            _title = tg_format.read_ticket_title(ws)
            _hdr = tg_format.tg_header("🧹", ws.state.company_id, ws.state.ticket_id, _title)
            await self._notifier.send_message(
                chat_id,
                f"{_hdr}\nCleared {mb:.0f} MB of Gradle transforms cache. Retrying from {target}.",
                reply_to_message_id=message_id,
            )
```

- [ ] **Step 14: Add header to clear_gradle error (around line 843)**

Replace:

```python
                await self._notifier.send_message(
                    chat_id, f"Failed to clear Gradle cache: {e}",
                    reply_to_message_id=message_id,
                )
```

With:

```python
                _title = tg_format.read_ticket_title(ws)
                _hdr = tg_format.tg_header("⚠️", ws.state.company_id, ws.state.ticket_id, _title)
                await self._notifier.send_message(
                    chat_id, f"{_hdr}\nFailed to clear Gradle cache: {e}",
                    reply_to_message_id=message_id,
                )
```

- [ ] **Step 15: Add header to pr_fix/pr_wontfix callback confirms (around line 908)**

Replace:

```python
            confirm = (
                f"✓ Recognized as {recorded_label} (matched: {btn_label}). "
                f"{action_tail} on {file_line}."
            )
```

With:

```python
            _title = tg_format.read_ticket_title(ws)
            _emoji = "✅" if action == "pr_fix" else "❌"
            _hdr = tg_format.tg_header(_emoji, ws.state.company_id, ws.state.ticket_id, _title)
            confirm = (
                f"{_hdr}\n"
                f"✓ Recognized as {recorded_label} (matched: {btn_label}). "
                f"{action_tail} on {file_line}."
            )
```

- [ ] **Step 16: Run all unit tests**

```bash
pytest tests/unit/ -v
```

Expected: all tests pass. If a test asserts on exact message text that no longer matches (e.g. checks for `"Retrying T-1 from"` when it now has a header prefix), update that test's assertion to use `in` rather than exact equality, or assert the ticket id is in the text.

- [ ] **Step 17: Commit**

```bash
git add integrations/telegram/command_handler.py
git commit -m "feat: add tg_header to all command_handler ticket confirmations; remove backticks"
```

---

## Self-Review Notes

Run after all tasks complete:

```bash
pytest tests/unit/ -v
grep -r "_tg_header\|_get_ticket_title" orchestrator/ integrations/  # should return nothing
grep -r "company_id/.*repo_id" orchestrator/ integrations/            # should return nothing (old format gone)
grep -rn "\`" orchestrator/escalation_view.py integrations/telegram/command_handler.py  # should return nothing in message strings
```
