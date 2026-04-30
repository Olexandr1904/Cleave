# PR Review Flow Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a one-word verdict to escalated PR comments, replace silent SKIP-on-free-text with capped re-investigation, let operators recall unanswered comments via slash command or button, and ensure every PR comment receives a reply on GitHub from the agent at decision time.

**Architecture:** Verdict is a new field in the classifier output, added to the agent prompt. Free-text replies stage a flag on the comment entry; the orchestrator's existing PR-review tick processes the flag, calls the classifier with `operator_hint`, and re-escalates. Recall is a thin wrapper that re-sends undecided comments and tracks all `msg_id`s per comment so reply-matching works against original or any recall.

**Tech Stack:** Python 3.10+, asyncio, pytest, pytest-asyncio.

**Spec:** [docs/superpowers/specs/2026-04-30-pr-review-flow-improvements-design.md](../specs/2026-04-30-pr-review-flow-improvements-design.md)

---

## File Structure

**New files:**
- `orchestrator/escalation_view.py` — pure function `build_escalated_comment_message` shared by orchestrator and command_handler
- `tests/unit/test_escalation_view.py` — unit tests for the rendering function
- `tests/unit/test_reinvestigation.py` — covers staging → orchestrator-tick → re-classify → re-escalate
- `tests/unit/test_unanswered_recall.py` — covers `/unanswered` command + button + reply-matching across `msg_id` list
- `tests/unit/test_github_reply_at_decision.py` — every comment gets a GitHub reply at decision time (AUTO_FIX, ESCALATE→FIX)

**Modified files:**
- `agents/pr-comment-responder-agent.md` — add Verdict + Operator Hint sections, update example
- `orchestrator/comment_classifier.py` — add `verdict` field to dataclass + parser, accept `operator_hint` kwarg
- `orchestrator/orchestrator.py` — re-investigation phase in `_action_fetch_pr_comments`, refactor `_send_escalated_comment_tg` to use `escalation_view`, append "Show unanswered" button to summary
- `integrations/telegram/command_handler.py` — `_classify_reply` helper, replace SKIP fallthrough, `_stage_reinvestigation`, `_handle_unanswered`, button branch for `unanswered:`, `msg_ids` matching, recognition echoes
- `integrations/telegram/intent_parser.py` — add `unanswered` intent
- `tests/unit/test_pr_comment_decision_echo.py` — extend with synonym + recognition-echo cases
- `tests/unit/test_review_decisions_skip.py` — rename to `test_review_decisions_freetext.py`, replace SKIP-flow assertions
- `tests/unit/test_comment_classifier.py` — extend with verdict parsing cases
- `tests/unit/test_command_handler.py` — extend with `unanswered` intent dispatch case
- `docs/features/agent-system.md` — paragraph about verdict, hint loop, recall command
- `docs/superpowers/specs/2026-04-21-pr-review-comment-resolution-design.md` — see-also link

---

## Task 1: Add `verdict` field to classifier output

**Files:**
- Modify: `orchestrator/comment_classifier.py`
- Modify: `tests/unit/test_comment_classifier.py`

- [ ] **Step 1: Read the existing test file**

Run: `cat tests/unit/test_comment_classifier.py | head -60`
Note the existing test pattern so new tests match style.

- [ ] **Step 2: Write failing tests for `verdict` parsing**

Append to `tests/unit/test_comment_classifier.py`:

```python
class TestVerdict:
    def test_parses_verdict_valid(self):
        raw = json.dumps([{
            "comment_id": 1, "classification": "ESCALATE",
            "reason": "ok", "verdict": "Valid",
        }])
        result = parse_classifications(raw)
        assert len(result) == 1
        assert result[0].verdict == "Valid"

    def test_parses_verdict_not_valid(self):
        raw = json.dumps([{
            "comment_id": 2, "classification": "ESCALATE",
            "reason": "ok", "verdict": "Not valid",
        }])
        result = parse_classifications(raw)
        assert result[0].verdict == "Not valid"

    def test_missing_verdict_defaults_to_unsure_with_warning(self, caplog):
        raw = json.dumps([{
            "comment_id": 3, "classification": "ESCALATE",
            "reason": "ok",
        }])
        with caplog.at_level("WARNING"):
            result = parse_classifications(raw)
        assert result[0].verdict == "Unsure"
        assert any("verdict" in rec.message.lower() for rec in caplog.records)

    def test_invalid_verdict_value_defaults_to_unsure(self):
        raw = json.dumps([{
            "comment_id": 4, "classification": "ESCALATE",
            "reason": "ok", "verdict": "MAYBE",
        }])
        result = parse_classifications(raw)
        assert result[0].verdict == "Unsure"
```

Make sure `import json` and `from orchestrator.comment_classifier import parse_classifications` are imported (probably already).

- [ ] **Step 3: Run the new tests — they must fail**

Run: `pytest tests/unit/test_comment_classifier.py::TestVerdict -v`
Expected: FAIL — `verdict` attribute does not exist yet.

- [ ] **Step 4: Add `verdict` to dataclass and parser**

Edit `orchestrator/comment_classifier.py`:

Add to top:

```python
VALID_VERDICTS = {"Valid", "Not valid"}
```

In `ClassifiedComment` dataclass, add field:

```python
verdict: str = "Unsure"
```

In `parse_classifications`, inside the `for item in data:` loop, after computing `classification`, add:

```python
verdict = item.get("verdict", "")
if verdict not in VALID_VERDICTS:
    logger.warning(
        "Comment %s missing or invalid verdict %r — defaulting to Unsure",
        item.get("comment_id"), verdict,
    )
    verdict = "Unsure"
```

Pass `verdict=verdict` when constructing `ClassifiedComment`.

- [ ] **Step 5: Run tests — must pass**

Run: `pytest tests/unit/test_comment_classifier.py -v`
Expected: PASS — all classifier tests including the new TestVerdict class.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/comment_classifier.py tests/unit/test_comment_classifier.py
git commit -m "feat(pr-review): add verdict field to comment classifier"
```

---

## Task 2: Update agent prompt for verdict + operator hint

**Files:**
- Modify: `agents/pr-comment-responder-agent.md`

This task has no automated test (it's prompt-engineering); the verdict-parsing test in Task 1 covers downstream behavior. The change is content-only.

- [ ] **Step 1: Read the existing agent prompt**

Run: `cat agents/pr-comment-responder-agent.md`

- [ ] **Step 2: Add Verdict requirement after the existing Classification Rules section**

Insert this block after the `**When in doubt, ESCALATE. Never guess.**` line (around line 74):

```markdown
## Verdict (separate from classification)

For EVERY comment — regardless of classification — output a one-word `verdict`:
- `Valid` — the reviewer is correct that the issue exists or applies
- `Not valid` — the reviewer is mistaken or the comment is off-base

Verdict is independent of action:
- AUTO_FIX is almost always `Valid`
- AUTO_REJECT can be `Valid` (issue exists but is out of scope) or `Not valid`
- ESCALATE can be either — commit to your lean even when human judgment is needed

You MUST commit to `Valid` or `Not valid`. The downstream system treats anything else as a parsing error.

## Operator Hint

If `{operator_hint}` is non-empty, an operator has reviewed the previous classification and pushed back. Treat the hint as a strong human signal that the prior classification or verdict may be wrong, but as evidence to investigate — not a command to obey.

- Investigate what the operator pointed at (read the files, check the patterns).
- If the hint reveals new evidence, update your classification, verdict, and reason.
- If the hint is itself wrong, you may return the same verdict — explain why in the reason so the operator sees why their hint didn't change your view.
```

- [ ] **Step 3: Update the example JSON output**

Find the JSON example block (around line 80-94) and add `"verdict": "Valid"` (or `"Not valid"`) to each example object:

```json
[
  {
    "comment_id": 12345,
    "classification": "AUTO_FIX",
    "verdict": "Valid",
    "reason": "Project uses @PreviewAcme, this file has bare @Preview",
    "suggested_fix": "Replace @Preview with @PreviewAcme on line 10"
  },
  {
    "comment_id": 67890,
    "classification": "ESCALATE",
    "verdict": "Valid",
    "reason": "Reviewer suggests using dimen resource — valid convention but adds scope",
    "suggested_fix": ""
  }
]
```

- [ ] **Step 4: Update Constraints section**

Add to the `## Constraints` list:
- `verdict` must be exactly `"Valid"` or `"Not valid"` — no other values allowed.

- [ ] **Step 5: Commit**

```bash
git add agents/pr-comment-responder-agent.md
git commit -m "feat(pr-review): require verdict + accept operator_hint in agent prompt"
```

---

## Task 3: Build the shared escalation message renderer

**Files:**
- Create: `orchestrator/escalation_view.py`
- Create: `tests/unit/test_escalation_view.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_escalation_view.py`:

```python
"""Tests for the shared escalated-comment message renderer."""

from __future__ import annotations

from types import SimpleNamespace

from orchestrator.escalation_view import build_escalated_comment_message


def _state():
    return SimpleNamespace(ticket_id="T-1", company_id="acme", repo_id="app")


def _cc():
    return {
        "comment_id": 99, "author": "Copilot", "file": "app/x.kt", "line": 10,
        "body": "Use @Inject here", "reason": "Repo convention requires @Inject.",
        "verdict": "Valid",
    }


class TestBuildMessage:
    def test_includes_verdict_line(self):
        text, _ = build_escalated_comment_message(
            _state(), _cc(), pr_number=42, ticket_title="A ticket",
        )
        assert "Valid — Repo convention requires @Inject." in text

    def test_not_valid_verdict_renders(self):
        cc = _cc()
        cc["verdict"] = "Not valid"
        cc["reason"] = "Reviewer is wrong, file follows existing pattern."
        text, _ = build_escalated_comment_message(
            _state(), cc, pr_number=42, ticket_title="A ticket",
        )
        assert "Not valid — Reviewer is wrong" in text

    def test_initial_message_no_recall_prefix(self):
        text, _ = build_escalated_comment_message(
            _state(), _cc(), pr_number=42, ticket_title="A ticket",
        )
        assert "still pending" not in text

    def test_recall_message_has_prefix(self):
        text, _ = build_escalated_comment_message(
            _state(), _cc(), pr_number=42, ticket_title="A ticket", recall=True,
        )
        assert text.startswith("🔁 (still pending)")

    def test_buttons_are_fix_and_wont_fix(self):
        _, buttons = build_escalated_comment_message(
            _state(), _cc(), pr_number=42, ticket_title="A ticket",
        )
        assert [b.label for b in buttons] == ["Fix", "Won't Fix"]
        assert buttons[0].action == "pr_fix:T-1:99"
        assert buttons[1].action == "pr_wontfix:T-1:99"

    def test_unsure_verdict_falls_back_to_reason_only(self):
        cc = _cc()
        cc["verdict"] = "Unsure"
        text, _ = build_escalated_comment_message(
            _state(), cc, pr_number=42, ticket_title="A ticket",
        )
        # Defensive fallback: render reason without verdict prefix
        assert "Repo convention requires @Inject." in text
        assert "Unsure —" not in text
```

- [ ] **Step 2: Run — they must fail**

Run: `pytest tests/unit/test_escalation_view.py -v`
Expected: FAIL — `escalation_view` module does not exist.

- [ ] **Step 3: Implement `escalation_view.py`**

Create `orchestrator/escalation_view.py`:

```python
"""Shared renderer for escalated PR comment TG messages.

Extracted so command_handler can re-send messages for the recall flow
without depending on Orchestrator internals.
"""

from __future__ import annotations

from typing import Any

from integrations.telegram.types import Button


def build_escalated_comment_message(
    state: Any,
    cc: dict[str, Any],
    pr_number: int,
    ticket_title: str = "",
    *,
    recall: bool = False,
) -> tuple[str, list[Button]]:
    """Build (text, buttons) for an escalated PR comment.

    cc is a dict-shaped comment record with keys: comment_id, author, file,
    line, body, reason, verdict (optional).
    """
    sep = "─" * 30
    title_part = f" — {ticket_title}" if ticket_title else ""
    hdr_prefix = "🔁 (still pending) " if recall else ""
    hdr = (
        f"{hdr_prefix}💬 [{state.company_id}/{state.repo_id}] {state.ticket_id}"
        f"{title_part} — PR #{pr_number}"
    )

    verdict = cc.get("verdict", "Unsure")
    reason = cc.get("reason", "") or ""
    if verdict in ("Valid", "Not valid"):
        assessment_line = f"  {verdict} — {reason}"
    else:
        assessment_line = f"  {reason}"

    body = cc.get("body", "") or ""
    text = (
        f"{hdr}\n"
        f"Comment by @{cc.get('author','?')} on {cc.get('file','?')}:{cc.get('line','?')}\n"
        f"{sep}\n"
        f"Suggestion:\n  {body[:300]}\n\n"
        f"Agent assessment:\n{assessment_line}\n"
        f"{sep}\n"
        "Tap a button below, or reply to this message with:\n"
        "  • `fix` — re-engage dev-agent\n"
        "  • `won't fix: <reason>` — post the reason on GitHub and resolve\n"
        "  • free text — re-investigate with your hint\n"
    )

    comment_key = f"{state.ticket_id}:{cc['comment_id']}"
    buttons = [
        Button(label="Fix", action=f"pr_fix:{comment_key}"),
        Button(label="Won't Fix", action=f"pr_wontfix:{comment_key}"),
    ]
    return text, buttons
```

- [ ] **Step 4: Verify the Button import path**

Run: `grep -rn "class Button" /home/admin0/tot/integrations/telegram/`
Expected: shows the `Button` dataclass location. If it's `integrations/telegram/notifier.py` or similar, update the import in `escalation_view.py` accordingly. The orchestrator imports it as `from integrations.telegram.types import Button` based on existing code — verify and align.

- [ ] **Step 5: Run tests — must pass**

Run: `pytest tests/unit/test_escalation_view.py -v`
Expected: PASS — 6 tests.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/escalation_view.py tests/unit/test_escalation_view.py
git commit -m "feat(pr-review): extract escalation message renderer for shared use"
```

---

## Task 4: Refactor `_send_escalated_comment_tg` to use the renderer

**Files:**
- Modify: `orchestrator/orchestrator.py:1674-1706`
- Modify: `tests/unit/test_pr_comment_decision_echo.py`

- [ ] **Step 1: Update the existing test fixture in test_pr_comment_decision_echo.py**

The existing test passes `cc = SimpleNamespace(...)` to `_send_escalated_comment_tg`. The renderer expects a dict, so adjust either the orchestrator call site to convert or pass dicts. To keep the orchestrator-side simple, convert at the boundary.

In `tests/unit/test_pr_comment_decision_echo.py` `_make_orch_for_send`, the existing tests pass `cc` as `SimpleNamespace`. Add a conversion helper or update tests to pass dicts. Cleanest: keep the existing tests, the orchestrator converts internally.

Add a new test ahead of the existing `test_escalation_message_has_no_skip_button`:

```python
@pytest.mark.asyncio
async def test_escalation_message_renders_verdict():
    """The escalated TG message must include the one-word verdict."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=42)
    orch = _make_orch_for_send(notifier)

    cc = SimpleNamespace(
        comment_id=99, author="Copilot", file="x.kt", line=10,
        body="Suggestion", reason="Real issue.", verdict="Valid",
    )
    await orch._send_escalated_comment_tg(_fake_workspace_for_send(), cc, pr_number=1234)

    body = notifier.send_message.call_args.args[1]
    assert "Valid — Real issue." in body
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_pr_comment_decision_echo.py::test_escalation_message_renders_verdict -v`
Expected: FAIL — verdict not in message.

- [ ] **Step 3: Refactor `_send_escalated_comment_tg`**

In `orchestrator/orchestrator.py`, replace the body of `_send_escalated_comment_tg` (around line 1674-1706) with:

```python
async def _send_escalated_comment_tg(self, workspace: Workspace, cc: Any, pr_number: int) -> int:
    """Send a single escalated comment to TG. Returns the message ID."""
    from orchestrator.escalation_view import build_escalated_comment_message

    state = workspace.state
    title = self._get_ticket_title(workspace)

    cc_dict = {
        "comment_id": cc.comment_id if hasattr(cc, "comment_id") else cc["comment_id"],
        "author": getattr(cc, "author", None) if hasattr(cc, "author") else cc.get("author"),
        "file": getattr(cc, "file", None) if hasattr(cc, "file") else cc.get("file"),
        "line": getattr(cc, "line", None) if hasattr(cc, "line") else cc.get("line"),
        "body": getattr(cc, "body", "") if hasattr(cc, "body") else cc.get("body", ""),
        "reason": getattr(cc, "reason", "") if hasattr(cc, "reason") else cc.get("reason", ""),
        "verdict": getattr(cc, "verdict", "Unsure") if hasattr(cc, "verdict") else cc.get("verdict", "Unsure"),
    }
    text, buttons = build_escalated_comment_message(
        state, cc_dict, pr_number, ticket_title=title,
    )
    chat_id = self._get_chat_id(workspace)
    if chat_id and self._notifier:
        return await self._notifier.send_message(chat_id, text, buttons=buttons)
    return 0
```

The dict-or-attr conversion lets both `ClassifiedComment` and dict-shaped `pending_review_comments` entries flow through.

- [ ] **Step 4: Update `_action_fetch_pr_comments` to attach verdict to pending entries**

In `orchestrator/orchestrator.py` around line 1654, when building `pending_comments` from escalated `cc`, add `verdict`:

```python
pending_comments.append({
    "comment_id": cc.comment_id, "msg_ids": [msg_id], "decision": None,
    "author": cc.author, "file": cc.file, "line": cc.line,
    "body": cc.body, "reason": cc.reason,
    "verdict": cc.verdict,
    "hint_rounds": 0, "last_hint": None, "pending_reinvestigation": False,
})
```

(`msg_id` becomes `msg_ids: [msg_id]` here. Old key removed.)

- [ ] **Step 5: Run all PR-review-related tests — must pass**

Run: `pytest tests/unit/test_pr_comment_decision_echo.py tests/unit/test_escalation_view.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_pr_comment_decision_echo.py
git commit -m "refactor(pr-review): use shared renderer + attach verdict to pending entries"
```

---

## Task 5: Lazy migration `msg_id` → `msg_ids[]` in command_handler

**Files:**
- Modify: `integrations/telegram/command_handler.py`
- Modify: `tests/unit/test_pr_comment_decision_echo.py`

- [ ] **Step 1: Write a test for matching against `msg_ids` list**

Add to `tests/unit/test_pr_comment_decision_echo.py`:

```python
@pytest.mark.asyncio
async def test_reply_matches_against_msg_ids_list():
    """Reply matching must work whether comment has 'msg_id' (old) or 'msg_ids' (new)."""
    handler = CommandHandler.__new__(CommandHandler)
    handler._allowed_chat_ids = None
    handler._notifier = MagicMock()
    handler._notifier.send_message = AsyncMock()
    handler._events = None

    ws = MagicMock()
    ws.state = SimpleNamespace(
        current_state="PR_REVIEW", ticket_id="T-1",
        pending_review_comments=[
            {"comment_id": 1, "msg_ids": [100, 200], "decision": None,
             "author": "C", "file": "x.kt", "line": 1, "body": "b", "reason": "r",
             "verdict": "Valid", "hint_rounds": 0, "last_hint": None,
             "pending_reinvestigation": False},
        ],
    )
    handler._active_workspaces_fn = lambda: [ws]

    matched = await handler.handle_reply(reply_to_msg_id=200, text="fix", chat_id="c-1")
    assert matched is True
    assert ws.state.pending_review_comments[0]["decision"] == "fix"
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_pr_comment_decision_echo.py::test_reply_matches_against_msg_ids_list -v`
Expected: FAIL — current code looks up `msg_id`, not `msg_ids`.

- [ ] **Step 3: Add migration helper at top of command_handler.py**

In `integrations/telegram/command_handler.py`, after the existing `_write_human_input` function (around line 30), add:

```python
def _ensure_msg_ids(c: dict) -> list[int]:
    """Lazy migration: old entries had 'msg_id', new ones have 'msg_ids'.

    Mutates c in place and returns the list.
    """
    if "msg_ids" in c:
        return c["msg_ids"]
    if "msg_id" in c:
        c["msg_ids"] = [c.pop("msg_id")]
        return c["msg_ids"]
    c["msg_ids"] = []
    return c["msg_ids"]
```

- [ ] **Step 4: Update `handle_reply` lookup to use `msg_ids`**

In `handle_reply` (around line 461), change:

```python
for c in pending:
    if c.get("msg_id") == reply_to_msg_id:
```

to:

```python
for c in pending:
    if reply_to_msg_id in _ensure_msg_ids(c):
```

- [ ] **Step 5: Run — must pass**

Run: `pytest tests/unit/test_pr_comment_decision_echo.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add integrations/telegram/command_handler.py tests/unit/test_pr_comment_decision_echo.py
git commit -m "refactor(pr-review): match replies against msg_ids list with lazy migration"
```

---

## Task 6: `_classify_reply` helper with synonym sets

**Files:**
- Modify: `integrations/telegram/command_handler.py`
- Modify: `tests/unit/test_pr_comment_decision_echo.py`

- [ ] **Step 1: Write failing tests for synonym matching**

Add to `tests/unit/test_pr_comment_decision_echo.py`:

```python
from integrations.telegram.command_handler import _classify_reply


class TestClassifyReply:
    @pytest.mark.parametrize("text,expected_token", [
        ("fix", "fix"), ("FIX", "fix"), ("yes", "yes"), ("fxi", "fxi"),
        ("fixx", "fixx"), ("Fix it", "fix it"),
    ])
    def test_fix_synonyms(self, text, expected_token):
        decision, token, _ = _classify_reply(text)
        assert decision == "fix"
        assert token == expected_token

    @pytest.mark.parametrize("text,expected_token", [
        ("won't fix", "won't fix"),
        ("wont fix", "wont fix"),
        ("don't fix", "don't fix"),
        ("dont fix", "dont fix"),
        ("do not fix", "do not fix"),
        ("not fix", "not fix"),
        ("no fix", "no fix"),
        ("WON'T FIX: out of scope", "won't fix"),
    ])
    def test_wont_fix_synonyms(self, text, expected_token):
        decision, token, _reason = _classify_reply(text)
        assert decision == "wont_fix"
        assert token == expected_token

    def test_wont_fix_extracts_reason(self):
        _, _, reason = _classify_reply("don't fix: this is intentional")
        assert reason == "this is intentional"

    def test_wont_fix_no_reason(self):
        _, _, reason = _classify_reply("won't fix")
        assert reason == ""

    def test_free_text_is_reinvestigate(self):
        decision, token, _ = _classify_reply("check other repos, we use this pattern")
        assert decision == "reinvestigate"
        assert token == ""

    def test_skip_is_no_longer_recognized(self):
        """Skip semantic was removed — should now be free-text."""
        decision, _, _ = _classify_reply("skip")
        assert decision == "reinvestigate"
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_pr_comment_decision_echo.py::TestClassifyReply -v`
Expected: FAIL — `_classify_reply` does not exist.

- [ ] **Step 3: Implement `_classify_reply`**

In `integrations/telegram/command_handler.py`, add at module level (after `_ensure_msg_ids`):

```python
_FIX_TOKENS = ("fix it", "fix", "fxi", "fifx", "fixx", "fx", "fi", "yes")
_WONT_FIX_TOKENS = (
    "won't fix", "wont fix", "do not fix", "don't fix", "dont fix",
    "not fix", "no fix",
)


def _classify_reply(text: str) -> tuple[str, str, str]:
    """Classify an operator reply into (decision, matched_token, wf_reason).

    decision ∈ {'fix', 'wont_fix', 'reinvestigate'}
    matched_token: empty for reinvestigate, else the canonical token used.
    wf_reason: only meaningful for 'wont_fix'; the text after ':' or whitespace.
    """
    raw = text.strip()
    lower = raw.lower()
    if not lower:
        return "reinvestigate", "", ""

    # Fix synonyms — must be exact match (no trailing reason)
    for tok in _FIX_TOKENS:
        if lower == tok:
            return "fix", tok, ""

    # Won't-fix synonyms — token at start, optional ':' or whitespace + reason
    for tok in _WONT_FIX_TOKENS:
        if lower == tok:
            return "wont_fix", tok, ""
        if lower.startswith(tok):
            rest = lower[len(tok):].lstrip(": ").strip()
            # Must be separated by punctuation or whitespace; reject e.g. "fixed"
            sep_char = lower[len(tok)] if len(lower) > len(tok) else ""
            if sep_char in (":", " ", "\t"):
                return "wont_fix", tok, rest

    return "reinvestigate", "", ""
```

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/unit/test_pr_comment_decision_echo.py::TestClassifyReply -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add integrations/telegram/command_handler.py tests/unit/test_pr_comment_decision_echo.py
git commit -m "feat(pr-review): _classify_reply helper with fix/won't-fix synonym sets"
```

---

## Task 7: Re-wire `handle_reply` to use `_classify_reply` + recognition echoes

**Files:**
- Modify: `integrations/telegram/command_handler.py:449-516`
- Modify: `tests/unit/test_pr_comment_decision_echo.py`

- [ ] **Step 1: Write failing tests for recognition echoes**

Add to `tests/unit/test_pr_comment_decision_echo.py`:

```python
@pytest.mark.asyncio
async def test_echo_includes_matched_token_for_fix():
    handler = CommandHandler.__new__(CommandHandler)
    handler._allowed_chat_ids = None
    handler._notifier = MagicMock()
    handler._notifier.send_message = AsyncMock()
    handler._events = None
    handler._wake_fn = None
    ws = MagicMock()
    ws.state = SimpleNamespace(
        current_state="PR_REVIEW", ticket_id="T-1",
        pending_review_comments=[
            {"comment_id": 1, "msg_ids": [100], "decision": None,
             "author": "C", "file": "x.kt", "line": 1, "body": "b", "reason": "r",
             "verdict": "Valid", "hint_rounds": 0, "last_hint": None,
             "pending_reinvestigation": False},
        ],
    )
    handler._active_workspaces_fn = lambda: [ws]

    await handler.handle_reply(reply_to_msg_id=100, text="yes", chat_id="c-1")

    sent = handler._notifier.send_message.call_args.args[1]
    assert "Recognized as FIX" in sent
    assert "matched: 'yes'" in sent


@pytest.mark.asyncio
async def test_echo_includes_matched_token_for_wont_fix():
    handler = CommandHandler.__new__(CommandHandler)
    handler._allowed_chat_ids = None
    handler._notifier = MagicMock()
    handler._notifier.send_message = AsyncMock()
    handler._events = None
    handler._wake_fn = None
    ws = MagicMock()
    ws.state = SimpleNamespace(
        current_state="PR_REVIEW", ticket_id="T-1",
        pending_review_comments=[
            {"comment_id": 1, "msg_ids": [100], "decision": None,
             "author": "C", "file": "x.kt", "line": 1, "body": "b", "reason": "r",
             "verdict": "Valid", "hint_rounds": 0, "last_hint": None,
             "pending_reinvestigation": False},
        ],
    )
    handler._active_workspaces_fn = lambda: [ws]

    await handler.handle_reply(reply_to_msg_id=100, text="don't fix this is intentional", chat_id="c-1")

    sent = handler._notifier.send_message.call_args.args[1]
    assert "Recognized as WON'T FIX" in sent
    assert "matched: 'don\\'t fix'" in sent or "matched: \"don't fix\"" in sent
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_pr_comment_decision_echo.py::test_echo_includes_matched_token_for_fix tests/unit/test_pr_comment_decision_echo.py::test_echo_includes_matched_token_for_wont_fix -v`
Expected: FAIL.

- [ ] **Step 3: Replace `handle_reply` PR_REVIEW branch**

Replace the body of the `for c in pending:` loop in `handle_reply` (currently around lines 460-516) with:

```python
for c in pending:
    if reply_to_msg_id not in _ensure_msg_ids(c):
        continue
    decision, matched_token, wf_reason = _classify_reply(text)

    if decision == "reinvestigate":
        return await self._stage_reinvestigation(c, ws, text.strip(), chat_id)

    file_line = f"{c.get('file','?')}:{c.get('line','?')}"
    if decision == "fix":
        c["decision"] = "fix"
        decision_label = f"FIX (matched: {matched_token!r}). Dev-agent will re-engage on {file_line}."
        recorded_label = "FIX"
        stored_decision = "fix"
    else:  # wont_fix
        reason_text = wf_reason or "operator decision"
        c["decision"] = f"won't fix: {reason_text}"
        decision_label = (
            f"WON'T FIX (matched: {matched_token!r}). "
            f'Posting on GitHub: "{reason_text}".'
        )
        recorded_label = "WON'T FIX"
        stored_decision = c["decision"]

    ws.save_state()
    confirm = f"✓ Recognized as {recorded_label}. {decision_label}"
    if self._events is not None:
        self._events.emit(
            "pr_comment_decision_recorded",
            f"{ws.state.ticket_id}: {stored_decision} for comment {c.get('comment_id')}",
            ticket_id=ws.state.ticket_id,
            data={
                "comment_id": c.get("comment_id"),
                "decision": stored_decision,
                "via": "reply",
                "matched_token": matched_token,
                "raw_text": text.strip()[:200],
            },
        )
    undecided = [x for x in pending if x.get("decision") is None]
    if undecided:
        await self._notifier.send_message(
            chat_id, f"{confirm}\n{len(undecided)} comment(s) remaining.",
        )
    else:
        await self._notifier.send_message(
            chat_id, f"{confirm}\nAll decisions in for {ws.state.ticket_id}. Executing now.",
        )
        if hasattr(self, '_wake_fn') and self._wake_fn:
            self._wake_fn()
    return True
```

(`_stage_reinvestigation` is implemented in Task 8. For this task, add a temporary stub:)

```python
async def _stage_reinvestigation(self, c, ws, hint_text, chat_id) -> bool:
    return True  # Implemented in Task 8
```

- [ ] **Step 4: Run — must pass (stub for re-investigation is fine)**

Run: `pytest tests/unit/test_pr_comment_decision_echo.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add integrations/telegram/command_handler.py tests/unit/test_pr_comment_decision_echo.py
git commit -m "feat(pr-review): handle_reply uses _classify_reply with recognition echo"
```

---

## Task 8: Implement `_stage_reinvestigation` with 3-round cap

**Files:**
- Modify: `integrations/telegram/command_handler.py`
- Create: `tests/unit/test_reinvestigation.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_reinvestigation.py`:

```python
"""Tests for the free-text → re-investigation flow."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.telegram.command_handler import CommandHandler


def _handler():
    h = CommandHandler.__new__(CommandHandler)
    h._allowed_chat_ids = None
    h._notifier = MagicMock()
    h._notifier.send_message = AsyncMock()
    h._events = MagicMock()
    h._events.emit = MagicMock()
    h._wake_fn = MagicMock()
    return h


def _entry():
    return {
        "comment_id": 1, "msg_ids": [100], "decision": None,
        "author": "Copilot", "file": "x.kt", "line": 10,
        "body": "b", "reason": "r", "verdict": "Valid",
        "hint_rounds": 0, "last_hint": None, "pending_reinvestigation": False,
    }


def _ws():
    ws = MagicMock()
    ws.state = SimpleNamespace(
        current_state="PR_REVIEW", ticket_id="T-1",
        pending_review_comments=[_entry()],
    )
    return ws


class TestStageReinvestigation:
    @pytest.mark.asyncio
    async def test_first_hint_stages_flag(self):
        h = _handler()
        ws = _ws()
        c = ws.state.pending_review_comments[0]
        result = await h._stage_reinvestigation(c, ws, "look at other repos", "chat-1")
        assert result is True
        assert c["pending_reinvestigation"] is True
        assert c["last_hint"] == "look at other repos"
        ws.save_state.assert_called()
        h._wake_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_first_hint_sends_recognition_ack(self):
        h = _handler()
        ws = _ws()
        c = ws.state.pending_review_comments[0]
        await h._stage_reinvestigation(c, ws, "hint", "chat-1")
        sent = h._notifier.send_message.call_args.args[1]
        assert "Recognized as hint" in sent
        assert "round 1/3" in sent
        assert "Re-checking" in sent

    @pytest.mark.asyncio
    async def test_third_round_still_allowed(self):
        h = _handler()
        ws = _ws()
        c = ws.state.pending_review_comments[0]
        c["hint_rounds"] = 2
        await h._stage_reinvestigation(c, ws, "third hint", "chat-1")
        assert c["pending_reinvestigation"] is True
        sent = h._notifier.send_message.call_args.args[1]
        assert "round 3/3" in sent

    @pytest.mark.asyncio
    async def test_fourth_round_rejected(self):
        h = _handler()
        ws = _ws()
        c = ws.state.pending_review_comments[0]
        c["hint_rounds"] = 3
        await h._stage_reinvestigation(c, ws, "fourth hint", "chat-1")
        assert c["pending_reinvestigation"] is False
        assert c["last_hint"] is None  # not stored
        sent = h._notifier.send_message.call_args.args[1]
        assert "Hint loop exceeded" in sent
        h._wake_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_emits_staged_event(self):
        h = _handler()
        ws = _ws()
        c = ws.state.pending_review_comments[0]
        await h._stage_reinvestigation(c, ws, "hint", "chat-1")
        h._events.emit.assert_called()
        event_name = h._events.emit.call_args.args[0]
        assert event_name == "pr_comment_reinvestigation_staged"

    @pytest.mark.asyncio
    async def test_emits_exhausted_event_on_cap(self):
        h = _handler()
        ws = _ws()
        c = ws.state.pending_review_comments[0]
        c["hint_rounds"] = 3
        await h._stage_reinvestigation(c, ws, "fourth hint", "chat-1")
        h._events.emit.assert_called()
        event_name = h._events.emit.call_args.args[0]
        assert event_name == "pr_comment_hint_exhausted"
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_reinvestigation.py -v`
Expected: FAIL — stub returns True without doing anything.

- [ ] **Step 3: Replace the stub with the real implementation**

In `integrations/telegram/command_handler.py`, replace the stub:

```python
async def _stage_reinvestigation(self, c, ws, hint_text: str, chat_id: str) -> bool:
    """Stage a re-investigation request on the comment entry.

    Returns True (always handled, even when capped).
    """
    rounds = int(c.get("hint_rounds", 0) or 0)
    file_line = f"{c.get('file','?')}:{c.get('line','?')}"
    if rounds >= 3:
        msg = (
            f"⚠ Hint loop exceeded (3/3) for @{c.get('author','?')} on {file_line}. "
            f"Reply `fix` or `won't fix` to close this comment."
        )
        await self._notifier.send_message(chat_id, msg)
        if self._events is not None:
            self._events.emit(
                "pr_comment_hint_exhausted",
                f"{ws.state.ticket_id}: hint cap reached for comment {c.get('comment_id')}",
                ticket_id=ws.state.ticket_id,
                data={
                    "comment_id": c.get("comment_id"),
                    "attempted_hint_excerpt": hint_text[:120],
                },
            )
        return True

    c["last_hint"] = hint_text
    c["pending_reinvestigation"] = True
    ws.save_state()

    next_round = rounds + 1
    msg = (
        f"🔍 Recognized as hint (round {next_round}/3). "
        f"Re-checking @{c.get('author','?')}'s comment on {file_line} with your context…"
    )
    await self._notifier.send_message(chat_id, msg)
    if self._events is not None:
        self._events.emit(
            "pr_comment_reinvestigation_staged",
            f"{ws.state.ticket_id}: hint round {next_round} for comment {c.get('comment_id')}",
            ticket_id=ws.state.ticket_id,
            data={
                "comment_id": c.get("comment_id"),
                "hint_round": next_round,
                "hint_excerpt": hint_text[:200],
            },
        )
    if hasattr(self, "_wake_fn") and self._wake_fn:
        self._wake_fn()
    return True
```

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/unit/test_reinvestigation.py -v tests/unit/test_pr_comment_decision_echo.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add integrations/telegram/command_handler.py tests/unit/test_reinvestigation.py
git commit -m "feat(pr-review): _stage_reinvestigation with 3-round cap and recognition ack"
```

---

## Task 9: `classify_comments` accepts `operator_hint` kwarg

**Files:**
- Modify: `orchestrator/comment_classifier.py`
- Modify: `tests/unit/test_comment_classifier.py`

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_comment_classifier.py`:

```python
class TestOperatorHint:
    @pytest.mark.asyncio
    async def test_operator_hint_threaded_to_agent_runtime(self):
        from orchestrator.comment_classifier import classify_comments

        runtime = MagicMock()
        result = MagicMock()
        result.success = True
        result.output = '[{"comment_id": 1, "classification": "ESCALATE", "verdict": "Valid", "reason": "ok"}]'
        runtime.execute = AsyncMock(return_value=result)

        ws = MagicMock()
        comments = [SimpleNamespace(id=1, author="C", path="x.kt", line=1, body="b")]

        await classify_comments(comments, ws, runtime, operator_hint="check repo X")

        ctx = runtime.execute.call_args.kwargs["extra_context"]
        assert ctx["operator_hint"] == "check repo X"

    @pytest.mark.asyncio
    async def test_default_operator_hint_is_empty_string(self):
        from orchestrator.comment_classifier import classify_comments

        runtime = MagicMock()
        result = MagicMock()
        result.success = True
        result.output = "[]"
        runtime.execute = AsyncMock(return_value=result)
        ws = MagicMock()

        await classify_comments([], ws, runtime)

        ctx = runtime.execute.call_args.kwargs["extra_context"]
        assert ctx["operator_hint"] == ""
```

Make sure imports include `from types import SimpleNamespace` and `from unittest.mock import AsyncMock, MagicMock` and `import pytest` at the top.

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_comment_classifier.py::TestOperatorHint -v`
Expected: FAIL — kwarg not supported.

- [ ] **Step 3: Update `classify_comments`**

In `orchestrator/comment_classifier.py`:

```python
async def classify_comments(
    comments: list[Any],
    workspace: Any,
    agent_runtime: Any,
    *,
    operator_hint: str = "",
) -> list[ClassifiedComment]:
    ...
    result = await agent_runtime.execute(
        agent_id="pr-comment-responder-agent",
        workspace=workspace,
        extra_context={
            "pr_comments_json": json.dumps(comment_data, indent=2),
            "operator_hint": operator_hint,
        },
    )
    ...
```

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/unit/test_comment_classifier.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/comment_classifier.py tests/unit/test_comment_classifier.py
git commit -m "feat(pr-review): classify_comments accepts operator_hint kwarg"
```

---

## Task 10: Re-investigation phase in `_action_fetch_pr_comments`

**Files:**
- Modify: `orchestrator/orchestrator.py:1456-1672`
- Modify: `tests/unit/test_reinvestigation.py`

- [ ] **Step 1: Write failing tests for the orchestrator phase**

Add to `tests/unit/test_reinvestigation.py`:

```python
class TestOrchestratorReinvestigation:
    """Tests _action_fetch_pr_comments re-investigation phase."""

    @pytest.mark.asyncio
    async def test_pending_reinvestigation_calls_classifier_with_hint(self, tmp_path, monkeypatch):
        from orchestrator.orchestrator import Orchestrator
        from orchestrator.comment_classifier import ClassifiedComment

        orch = Orchestrator.__new__(Orchestrator)
        orch._notifier = MagicMock()
        orch._notifier.send_message = AsyncMock(return_value=999)
        orch._events = None
        orch._get_chat_id = MagicMock(return_value="chat-1")
        orch._get_ticket_title = MagicMock(return_value="Ticket")
        orch._tg_header = MagicMock(return_value="hdr")
        orch._agent_runtime = MagicMock()

        ws = MagicMock()
        ws.reports_dir = tmp_path
        ws.state = SimpleNamespace(
            current_state="PR_REVIEW", ticket_id="T-1",
            company_id="acme", repo_id="app",
            pr_number=42, review_cycle=1,
            human_input_reply=None, stage_iterations={},
            pending_review_comments=[
                {"comment_id": 1, "msg_ids": [100], "decision": None,
                 "author": "C", "file": "x.kt", "line": 1, "body": "b",
                 "reason": "old reason", "verdict": "Valid",
                 "hint_rounds": 0, "last_hint": "look at repo X",
                 "pending_reinvestigation": True},
            ],
        )
        ws.save_state = MagicMock()

        async def fake_classify(comments, workspace, runtime, *, operator_hint=""):
            assert operator_hint == "look at repo X"
            return [ClassifiedComment(
                comment_id=1, classification="ESCALATE", verdict="Not valid",
                reason="new reason after re-check",
                author="C", file="x.kt", line=1, body="b",
            )]

        monkeypatch.setattr(
            "orchestrator.orchestrator.classify_comments", fake_classify, raising=False,
        )
        # also patch via the import alias the orchestrator uses
        import orchestrator.orchestrator as orch_mod
        monkeypatch.setattr(orch_mod, "classify_comments", fake_classify, raising=False)

        await orch._reinvestigate_pending(ws)

        c = ws.state.pending_review_comments[0]
        assert c["verdict"] == "Not valid"
        assert c["reason"] == "new reason after re-check"
        assert c["hint_rounds"] == 1
        assert c["pending_reinvestigation"] is False
        # New escalation message sent → msg_id appended
        assert 999 in c["msg_ids"]
        assert len(c["msg_ids"]) == 2

    @pytest.mark.asyncio
    async def test_skips_decided_comments_during_reinvestigation(self, tmp_path):
        from orchestrator.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch._notifier = MagicMock()
        orch._events = None

        ws = MagicMock()
        ws.reports_dir = tmp_path
        ws.state = SimpleNamespace(
            pending_review_comments=[
                {"comment_id": 1, "msg_ids": [100], "decision": "fix",
                 "pending_reinvestigation": True, "hint_rounds": 0,
                 "last_hint": "x", "verdict": "Valid", "reason": "r",
                 "author": "C", "file": "x.kt", "line": 1, "body": "b"},
            ],
        )
        ws.save_state = MagicMock()
        # Should be a no-op — comment is already decided
        await orch._reinvestigate_pending(ws)
        c = ws.state.pending_review_comments[0]
        assert c["hint_rounds"] == 0
        assert c["pending_reinvestigation"] is True  # not cleared since decided
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_reinvestigation.py::TestOrchestratorReinvestigation -v`
Expected: FAIL — `_reinvestigate_pending` does not exist.

- [ ] **Step 3: Add the re-investigation phase method**

In `orchestrator/orchestrator.py`, add a new method (place near `_action_fetch_pr_comments`):

```python
async def _reinvestigate_pending(self, workspace: Workspace) -> None:
    """Process any pending re-investigation requests on this workspace.

    For each entry with pending_reinvestigation=True (and not decided), call
    the classifier with operator_hint, update the entry in place, and re-send
    a fresh escalated TG message. Always re-escalates — never acts silently
    after a hint.
    """
    state = workspace.state
    pending = state.pending_review_comments or []
    if not pending:
        return
    pr_number = state.pr_number
    if not pr_number:
        return

    for c in pending:
        if not c.get("pending_reinvestigation"):
            continue
        if c.get("decision") is not None:
            # Operator decided via button while this was queued — skip
            continue
        comment_stub = SimpleNamespace(
            id=c["comment_id"], author=c.get("author", ""),
            path=c.get("file", ""), line=c.get("line"),
            body=c.get("body", ""),
        )
        old_verdict = c.get("verdict")
        old_classification = "ESCALATE"
        try:
            classified = await classify_comments(
                [comment_stub], workspace, self._agent_runtime,
                operator_hint=c.get("last_hint") or "",
            )
        except Exception as e:
            logger.error("Re-investigation failed for comment %s: %s", c["comment_id"], e)
            # Surface, clear flag so we don't loop on the same exception
            if self._notifier:
                chat_id = self._get_chat_id(workspace)
                if chat_id:
                    await self._notifier.send_message(
                        chat_id,
                        f"⚠ Re-investigation failed for @{c.get('author','?')} "
                        f"on {c.get('file','?')}:{c.get('line','?')}. "
                        f"Reply `fix` or `won't fix` to close.",
                    )
            c["pending_reinvestigation"] = False
            workspace.save_state()
            continue

        if not classified:
            logger.warning("Re-investigation returned no result for comment %s", c["comment_id"])
            c["pending_reinvestigation"] = False
            workspace.save_state()
            continue

        cc = classified[0]
        c["verdict"] = cc.verdict
        c["reason"] = cc.reason
        # Note: classification can change (e.g., ESCALATE → AUTO_FIX), but we
        # always re-escalate for human confirmation. We don't auto-act.
        c["hint_rounds"] = int(c.get("hint_rounds", 0) or 0) + 1
        c["pending_reinvestigation"] = False
        workspace.save_state()

        new_msg_id = await self._send_escalated_comment_tg(workspace, cc, pr_number)
        if new_msg_id:
            c.setdefault("msg_ids", []).append(new_msg_id)
            workspace.save_state()

        if self._events is not None:
            self._emit(
                "pr_comment_reinvestigation_completed",
                f"{state.ticket_id}: re-checked comment {c['comment_id']}",
                project_id=state.company_id, ticket_id=state.ticket_id,
                data={
                    "comment_id": c["comment_id"],
                    "hint_round": c["hint_rounds"],
                    "old_verdict": old_verdict,
                    "new_verdict": cc.verdict,
                    "old_classification": old_classification,
                    "new_classification": cc.classification,
                },
            )
```

Add `from types import SimpleNamespace` to the orchestrator imports if not already present.

- [ ] **Step 4: Wire `_reinvestigate_pending` into `_action_fetch_pr_comments`**

In `_action_fetch_pr_comments`, near the top of Phase 2 (right before `pending = state.pending_review_comments or []` on line 1515), insert:

```python
# Phase 1.5: Process any pending re-investigations from operator hints
await self._reinvestigate_pending(workspace)
```

Re-read `pending` after this call (since `pending_review_comments` may have been mutated):

```python
# Phase 2: Check pending escalated decisions
pending = state.pending_review_comments or []
```

(The existing line already reads it after; just confirm ordering.)

- [ ] **Step 5: Run — must pass**

Run: `pytest tests/unit/test_reinvestigation.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_reinvestigation.py
git commit -m "feat(pr-review): orchestrator re-investigates comments on operator hints"
```

---

## Task 11: Add `unanswered` intent to intent_parser

**Files:**
- Modify: `integrations/telegram/intent_parser.py`
- Modify: `tests/unit/test_command_handler.py`

- [ ] **Step 1: Read the existing intent_parser prompt**

Run: `cat integrations/telegram/intent_parser.py | head -80`

- [ ] **Step 2: Write failing intent test**

Add to `tests/unit/test_command_handler.py` (or wherever intent tests live — check first with `grep -l "ParsedIntent\|intent_parser" tests/unit/`).

```python
def test_intent_parser_recognizes_unanswered():
    from integrations.telegram.intent_parser import IntentParser, ParsedIntent

    # Simulated LLM output for the test — actual integration uses real LLM
    raw = '{"intent": "unanswered", "params": {"ticket_id": ""}, "reply": "Showing pending comments"}'
    parsed = ParsedIntent.from_json(raw)
    assert parsed.intent == "unanswered"
    assert parsed.params.get("ticket_id") == ""


def test_intent_parser_recognizes_unanswered_with_ticket():
    from integrations.telegram.intent_parser import ParsedIntent

    raw = '{"intent": "unanswered", "params": {"ticket_id": "T-1"}, "reply": "ok"}'
    parsed = ParsedIntent.from_json(raw)
    assert parsed.intent == "unanswered"
    assert parsed.params.get("ticket_id") == "T-1"
```

(`ParsedIntent.from_json` is the existing factory at line 47-66 of `intent_parser.py`.)

- [ ] **Step 3: Run — likely passes already (parser is permissive)**

Run: `pytest tests/unit/test_command_handler.py -v -k unanswered`
The `from_json` factory accepts arbitrary intent strings. If it passes, that's fine — the real change is in the prompt.

- [ ] **Step 4: Update the LLM prompt**

In `integrations/telegram/intent_parser.py`, locate the intent classification prompt (around line 21). Add `unanswered` to the intent list:

```
Classify the user message into one of these intents:
  status, analyze, approve, reject, set_mode, retry, provide_input, reviewed, unanswered, unknown
```

Add a rule line near the existing `provide_input` rule:

```
- unanswered: params.ticket_id (optional string). Triggered when the user is asking what's still waiting on them in PR review (e.g., `/unanswered`, `/repeat`, `what's pending`, `which comments are open`, `unanswered`).
```

- [ ] **Step 5: Update the unknown-fallback message to advertise the command**

Find the `reply="I didn't understand that..."` line (around line 65) and add `unanswered comment recall` to the list.

- [ ] **Step 6: Commit**

```bash
git add integrations/telegram/intent_parser.py tests/unit/test_command_handler.py
git commit -m "feat(pr-review): add unanswered intent to LLM prompt"
```

---

## Task 12: `_handle_unanswered` method + dispatch

**Files:**
- Modify: `integrations/telegram/command_handler.py`
- Create: `tests/unit/test_unanswered_recall.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_unanswered_recall.py`:

```python
"""Tests for /unanswered recall + 'Show unanswered' button + reply matching."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from integrations.telegram.command_handler import CommandHandler
from integrations.telegram.intent_parser import ParsedIntent


def _handler():
    h = CommandHandler.__new__(CommandHandler)
    h._allowed_chat_ids = None
    h._notifier = MagicMock()
    h._notifier.send_message = AsyncMock(return_value=200)
    h._events = MagicMock()
    h._events.emit = MagicMock()
    h._wake_fn = MagicMock()
    return h


def _ws_with_pending(comments):
    ws = MagicMock()
    ws.state = SimpleNamespace(
        current_state="PR_REVIEW", ticket_id="T-1",
        company_id="acme", repo_id="app", pr_number=42,
        pending_review_comments=comments,
    )
    return ws


def _entry(comment_id, decision=None, msg_ids=None):
    return {
        "comment_id": comment_id,
        "msg_ids": msg_ids or [100 + comment_id],
        "decision": decision,
        "author": "Copilot", "file": f"x{comment_id}.kt", "line": comment_id,
        "body": "b", "reason": "r", "verdict": "Valid",
        "hint_rounds": 0, "last_hint": None, "pending_reinvestigation": False,
    }


class TestHandleUnanswered:
    @pytest.mark.asyncio
    async def test_no_pending_comments_replies_with_empty_message(self):
        h = _handler()
        h._active_workspaces_fn = lambda: []
        intent = ParsedIntent(intent="unanswered", params={"ticket_id": ""}, reply="")
        await h._handle_unanswered(intent, "chat-1", processing_msg_id=None)
        sent = h._notifier.send_message.call_args.args[1]
        assert "No tickets" in sent

    @pytest.mark.asyncio
    async def test_resends_only_undecided_comments(self):
        h = _handler()
        ws = _ws_with_pending([
            _entry(1),
            _entry(2, decision="fix"),
            _entry(3),
        ])
        h._active_workspaces_fn = lambda: [ws]
        intent = ParsedIntent(intent="unanswered", params={"ticket_id": ""}, reply="")
        await h._handle_unanswered(intent, "chat-1", processing_msg_id=None)
        # 2 undecided comments + 1 summary = 3 sends
        assert h._notifier.send_message.call_count == 3
        # Comment ids 1 and 3 should have new msg_ids appended
        c1 = ws.state.pending_review_comments[0]
        c3 = ws.state.pending_review_comments[2]
        assert len(c1["msg_ids"]) == 2
        assert len(c3["msg_ids"]) == 2

    @pytest.mark.asyncio
    async def test_filters_to_specified_ticket(self):
        h = _handler()
        ws1 = _ws_with_pending([_entry(1)])
        ws1.state.ticket_id = "T-1"
        ws2 = _ws_with_pending([_entry(2)])
        ws2.state.ticket_id = "T-2"
        h._active_workspaces_fn = lambda: [ws1, ws2]

        intent = ParsedIntent(intent="unanswered", params={"ticket_id": "T-1"}, reply="")
        await h._handle_unanswered(intent, "chat-1", processing_msg_id=None)

        # Only T-1 entry should get a new msg_id
        assert len(ws1.state.pending_review_comments[0]["msg_ids"]) == 2
        assert len(ws2.state.pending_review_comments[0]["msg_ids"]) == 1

    @pytest.mark.asyncio
    async def test_emits_recall_event(self):
        h = _handler()
        ws = _ws_with_pending([_entry(1)])
        h._active_workspaces_fn = lambda: [ws]
        intent = ParsedIntent(intent="unanswered", params={"ticket_id": ""}, reply="")
        await h._handle_unanswered(intent, "chat-1", processing_msg_id=None)
        h._events.emit.assert_called()
        names = [call.args[0] for call in h._events.emit.call_args_list]
        assert "pr_comments_unanswered_recalled" in names

    @pytest.mark.asyncio
    async def test_reply_to_recall_message_resolves_comment(self):
        """Reply to either original or recall msg_id matches."""
        h = _handler()
        c = _entry(1, msg_ids=[100, 200])  # 100 = original, 200 = recall
        ws = _ws_with_pending([c])
        ws.save_state = MagicMock()
        h._active_workspaces_fn = lambda: [ws]

        # Reply to the recall message
        await h.handle_reply(reply_to_msg_id=200, text="fix", chat_id="chat-1")
        assert ws.state.pending_review_comments[0]["decision"] == "fix"
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_unanswered_recall.py -v`
Expected: FAIL — `_handle_unanswered` does not exist.

- [ ] **Step 3: Implement `_handle_unanswered`**

In `integrations/telegram/command_handler.py`, add:

```python
async def _handle_unanswered(self, intent, chat_id: str, processing_msg_id: int | None) -> None:
    """Re-send all undecided PR comments. Triggered by /unanswered intent."""
    from orchestrator.escalation_view import build_escalated_comment_message

    workspaces = self._active_workspaces_fn()
    target_id = (intent.params.get("ticket_id") or "").strip()
    matches = [
        w for w in workspaces
        if w.state.current_state == "PR_REVIEW"
        and w.state.pending_review_comments
        and (not target_id or w.state.ticket_id == target_id)
    ]
    if not matches:
        await self._reply(chat_id, "No tickets have unanswered PR comments.", processing_msg_id)
        return

    total = 0
    for ws in matches:
        for c in ws.state.pending_review_comments:
            if c.get("decision") is not None:
                continue
            text, buttons = build_escalated_comment_message(
                ws.state, c, ws.state.pr_number, ticket_title="", recall=True,
            )
            new_msg_id = await self._notifier.send_message(chat_id, text, buttons=buttons)
            if new_msg_id:
                _ensure_msg_ids(c).append(new_msg_id)
                total += 1
        ws.save_state()
        if self._events is not None:
            self._events.emit(
                "pr_comments_unanswered_recalled",
                f"{ws.state.ticket_id}: recalled {total} unanswered comment(s)",
                ticket_id=ws.state.ticket_id,
                data={"ticket_id": ws.state.ticket_id, "count": total, "via": "command"},
            )
    await self._reply(
        chat_id, f"Resent {total} unanswered comment(s).", processing_msg_id,
    )
```

- [ ] **Step 4: Wire intent dispatch in `handle_message`**

Find the intent dispatch in `handle_message` (search for `intent.intent ==` or `if intent.intent`). Add:

```python
if intent.intent == "unanswered":
    await self._handle_unanswered(intent, chat_id, processing_msg_id)
    return
```

near the other intent branches.

- [ ] **Step 5: Run — must pass**

Run: `pytest tests/unit/test_unanswered_recall.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add integrations/telegram/command_handler.py tests/unit/test_unanswered_recall.py
git commit -m "feat(pr-review): /unanswered command recalls undecided comments"
```

---

## Task 13: "Show N unanswered" button on summary + button action

**Files:**
- Modify: `orchestrator/orchestrator.py:1619-1633`
- Modify: `integrations/telegram/command_handler.py:505-513` and the `pr_fix`/`pr_wontfix` echo around line 749-756
- Modify: `integrations/telegram/command_handler.py:593` (handle_callback)
- Modify: `tests/unit/test_unanswered_recall.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_unanswered_recall.py`:

```python
class TestShowUnansweredButton:
    @pytest.mark.asyncio
    async def test_button_action_recalls_comments(self):
        h = _handler()
        ws = _ws_with_pending([_entry(1), _entry(2)])
        h._active_workspaces_fn = lambda: [ws]

        await h.handle_callback(action="unanswered", ticket_id="T-1", chat_id="chat-1", message_id=999)

        assert len(ws.state.pending_review_comments[0]["msg_ids"]) == 2
        assert len(ws.state.pending_review_comments[1]["msg_ids"]) == 2

    @pytest.mark.asyncio
    async def test_button_payload_via_button(self):
        h = _handler()
        ws = _ws_with_pending([_entry(1)])
        h._active_workspaces_fn = lambda: [ws]

        await h.handle_callback(action="unanswered", ticket_id="T-1", chat_id="chat-1", message_id=999)

        h._events.emit.assert_called()
        last_call_data = None
        for call in h._events.emit.call_args_list:
            if call.args[0] == "pr_comments_unanswered_recalled":
                last_call_data = call.kwargs.get("data") or {}
        assert last_call_data is not None
        assert last_call_data.get("via") == "button"
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_unanswered_recall.py::TestShowUnansweredButton -v`
Expected: FAIL — `unanswered` action not handled.

- [ ] **Step 3: Add `unanswered` button branch in `handle_callback`**

In `integrations/telegram/command_handler.py`, in `handle_callback` (around line 593), add a new branch (place before the `else: Unknown action` fallback):

```python
elif action == "unanswered":
    intent = ParsedIntent(intent="unanswered", params={"ticket_id": ticket_id}, reply="")
    # Reuse the same recall path; pass via=button via flag on the entry.
    self._unanswered_via = "button"
    try:
        await self._handle_unanswered(intent, chat_id, processing_msg_id=None)
    finally:
        self._unanswered_via = None
```

Add `from integrations.telegram.intent_parser import ParsedIntent` if not already imported (it is — at line 14).

In `_handle_unanswered`, change the event emission to use the `_unanswered_via` flag:

```python
via = getattr(self, "_unanswered_via", None) or "command"
...
data={"ticket_id": ws.state.ticket_id, "count": total, "via": via},
```

- [ ] **Step 4: Append "Show N unanswered" button to summary message in orchestrator**

In `orchestrator/orchestrator.py` around line 1619-1633 (the auto-handled summary), modify the send block:

```python
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
        summary_buttons = None
        if escalated:
            lines.append(f"Waiting for your decisions on {len(escalated)} escalated comment(s).")
            summary_buttons = [
                Button(label=f"Show {len(escalated)} unanswered", action=f"unanswered:{state.ticket_id}"),
            ]
        await self._notifier.send_message(chat_id, "\n".join(lines), buttons=summary_buttons)
```

- [ ] **Step 5: Append "Show N unanswered" button to "N remaining" confirmations in command_handler**

In `integrations/telegram/command_handler.py`:

Around the reply confirmation (after Task 7's edit, the `if undecided:` branch sending "N comment(s) remaining"), update to add the button. Replace:

```python
if undecided:
    await self._notifier.send_message(
        chat_id, f"{confirm}\n{len(undecided)} comment(s) remaining.",
    )
```

with:

```python
if undecided:
    from integrations.telegram.types import Button
    btn = [Button(
        label=f"Show {len(undecided)} unanswered",
        action=f"unanswered:{ws.state.ticket_id}",
    )]
    await self._notifier.send_message(
        chat_id, f"{confirm}\n{len(undecided)} comment(s) remaining.", buttons=btn,
    )
```

Apply the same change to the button-press confirmation path (around line 749-756 in `handle_callback`'s `pr_fix`/`pr_wontfix` branch):

```python
if undecided:
    btn = [Button(
        label=f"Show {len(undecided)} unanswered",
        action=f"unanswered:{tid}",
    )]
    msg_text = f"{confirm}\n{len(undecided)} comment(s) remaining."
    await self._notifier.send_message(chat_id, msg_text, buttons=btn, reply_to_message_id=message_id)
else:
    msg_text = f"{confirm}\nAll decisions in for {tid}. Executing now."
    if hasattr(self, '_wake_fn') and self._wake_fn:
        self._wake_fn()
    await self._notifier.send_message(chat_id, msg_text, reply_to_message_id=message_id)
```

(`Button` is already imported in `handle_callback`'s imports — verify with grep, otherwise add.)

- [ ] **Step 6: Run — must pass**

Run: `pytest tests/unit/test_unanswered_recall.py tests/unit/test_pr_comment_decision_echo.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/orchestrator.py integrations/telegram/command_handler.py tests/unit/test_unanswered_recall.py
git commit -m "feat(pr-review): Show N unanswered button on summary + button recall path"
```

---

## Task 14: Update button decision echo with matched_token

**Files:**
- Modify: `integrations/telegram/command_handler.py:703-756`
- Modify: `tests/unit/test_pr_comment_decision_echo.py`

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_pr_comment_decision_echo.py`:

```python
@pytest.mark.asyncio
async def test_button_press_echo_includes_matched_token():
    handler = CommandHandler.__new__(CommandHandler)
    handler._allowed_chat_ids = None
    handler._notifier = MagicMock()
    handler._notifier.send_message = AsyncMock()
    handler._events = MagicMock()
    handler._events.emit = MagicMock()
    handler._wake_fn = None

    ws = MagicMock()
    ws.state = SimpleNamespace(
        ticket_id="T-1",
        pending_review_comments=[
            {"comment_id": 7, "msg_ids": [10], "decision": None,
             "author": "C", "file": "x.kt", "line": 1, "body": "b",
             "reason": "r", "verdict": "Valid",
             "hint_rounds": 0, "last_hint": None,
             "pending_reinvestigation": False},
        ],
    )
    handler._active_workspaces_fn = lambda: [ws]

    await handler.handle_callback(
        action="pr_fix", ticket_id="T-1:7", chat_id="chat-1", message_id=10,
    )

    sent = handler._notifier.send_message.call_args.args[1]
    assert "Recognized as FIX" in sent
    assert "matched: 'Fix' button" in sent

    # Event payload includes matched_token + via=button
    event_data = handler._events.emit.call_args.kwargs.get("data") or {}
    assert event_data.get("matched_token") == "button:fix"
    assert event_data.get("via") == "button"
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_pr_comment_decision_echo.py::test_button_press_echo_includes_matched_token -v`
Expected: FAIL — current echo doesn't say "Recognized as".

- [ ] **Step 3: Update the `pr_fix`/`pr_wontfix` button branch**

In `integrations/telegram/command_handler.py` (around line 703-756), replace the body of the `elif action in ("pr_fix", "pr_skip", "pr_wontfix"):` branch with:

```python
elif action in ("pr_fix", "pr_wontfix"):
    parts = ticket_id.split(":", 1)
    tid = parts[0]
    comment_id_str = parts[1] if len(parts) > 1 else ""
    ws = next((w for w in workspaces if w.state.ticket_id == tid), None)
    if not ws or not ws.state.pending_review_comments:
        await self._notifier.send_message(chat_id, f"No pending comments for {tid}.", reply_to_message_id=message_id)
        return

    matched_comment = None
    for c in ws.state.pending_review_comments:
        if str(c.get("comment_id")) == comment_id_str:
            if action == "pr_fix":
                c["decision"] = "fix"
                stored_decision = "fix"
                recorded_label = "FIX"
                matched_token = "button:fix"
                btn_label = "'Fix' button"
                action_tail = "Dev-agent will re-engage"
            else:
                c["decision"] = "won't fix: operator decision"
                stored_decision = c["decision"]
                recorded_label = "WON'T FIX"
                matched_token = "button:wontfix"
                btn_label = "'Won't Fix' button"
                action_tail = 'Posting on GitHub: "operator decision"'
            matched_comment = c
            break

    if matched_comment is None:
        await self._notifier.send_message(chat_id, "Comment not found.", reply_to_message_id=message_id)
        return

    ws.save_state()
    file_line = f"{matched_comment.get('file','?')}:{matched_comment.get('line','?')}"
    confirm = (
        f"✓ Recognized as {recorded_label} (matched: {btn_label}). "
        f"{action_tail} on {file_line}."
    )
    if self._events is not None:
        self._events.emit(
            "pr_comment_decision_recorded",
            f"{tid}: {stored_decision} for comment {comment_id_str}",
            ticket_id=tid,
            data={
                "comment_id": comment_id_str,
                "decision": stored_decision,
                "via": "button",
                "matched_token": matched_token,
            },
        )

    undecided = [x for x in ws.state.pending_review_comments if x.get("decision") is None]
    if undecided:
        from integrations.telegram.types import Button
        btn = [Button(
            label=f"Show {len(undecided)} unanswered",
            action=f"unanswered:{tid}",
        )]
        msg_text = f"{confirm}\n{len(undecided)} comment(s) remaining."
        await self._notifier.send_message(chat_id, msg_text, buttons=btn, reply_to_message_id=message_id)
    else:
        msg_text = f"{confirm}\nAll decisions in for {tid}. Executing now."
        if hasattr(self, '_wake_fn') and self._wake_fn:
            self._wake_fn()
        await self._notifier.send_message(chat_id, msg_text, reply_to_message_id=message_id)
```

Note: dropped `pr_skip` from the action tuple — the button is gone, the branch is dead.

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/unit/test_pr_comment_decision_echo.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add integrations/telegram/command_handler.py tests/unit/test_pr_comment_decision_echo.py
git commit -m "refactor(pr-review): button echo says 'Recognized as X (matched: Y button)'"
```

---

## Task 15: Post "Will fix" reply on GitHub at decision time

Every PR comment must receive a reply on GitHub stating fix-or-don't-fix and the reason. AUTO_REJECT and ESCALATE→WON'T_FIX already do this; the gaps are AUTO_FIX (currently silent until the fix lands) and ESCALATE→FIX (silent until the fix lands).

**Files:**
- Modify: `orchestrator/orchestrator.py` (AUTO_FIX classification block ~line 1581-1594, `_execute_review_decisions` FIX branch ~line 1735-1742)
- Create: `tests/unit/test_github_reply_at_decision.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_github_reply_at_decision.py`:

```python
"""Every PR comment must receive a reply on GitHub at decision time —
not silently waiting for the post-fix verification reply."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
    o._git_diff_files = MagicMock(return_value=set())
    o._git_head_sha = MagicMock(return_value="abcdef0000000000")
    return o


class TestAutoFixReply:
    @pytest.mark.asyncio
    async def test_auto_fix_posts_will_fix_reply_at_classification_time(self, tmp_path, monkeypatch):
        from orchestrator.comment_classifier import ClassifiedComment
        from orchestrator.orchestrator import Stage

        orch = _orch()

        vcs = MagicMock()
        vcs.get_pr_comments = AsyncMock(return_value=[])
        vcs.reply_to_comment = AsyncMock()
        vcs.resolve_comment = AsyncMock()
        orch._get_vcs_for_workspace = MagicMock(return_value=(vcs, None))

        # Force classify_comments to return AUTO_FIX
        async def fake_classify(comments, ws, runtime, *, operator_hint=""):
            return [ClassifiedComment(
                comment_id=42, classification="AUTO_FIX", verdict="Valid",
                reason="Annotation missing — repo convention requires it.",
                suggested_fix="Add @Inject", author="C", file="x.kt", line=10, body="b",
            )]
        import orchestrator.orchestrator as omod
        monkeypatch.setattr(omod, "classify_comments", fake_classify, raising=False)

        # Set up workspace state and stub out comment fetching
        ws = MagicMock()
        ws.reports_dir = tmp_path
        ws.state = SimpleNamespace(
            ticket_id="T-1", company_id="acme", repo_id="app",
            pr_number=42, current_state="PR_REVIEW",
            pending_review_comments=[], review_cycle=0,
            stage_iterations={}, human_input_reply="reviewed",
        )
        ws.save_state = MagicMock()

        # Inject a fake comment so classify gets called
        async def fake_get_comments(_pr):
            return [SimpleNamespace(
                id=42, in_reply_to_id=None, body="please fix this",
                author="C", path="x.kt", line=10,
            )]
        vcs.get_pr_comments = fake_get_comments

        await orch._action_fetch_pr_comments(ws, stage_def=None)

        # AUTO_FIX must post 'Will fix: ...' on GitHub at classification time
        vcs.reply_to_comment.assert_awaited()
        called_with = vcs.reply_to_comment.call_args.args
        assert called_with[0] == 42  # pr_number
        assert called_with[1] == 42  # comment_id
        assert called_with[2].startswith("Will fix: ")
        assert "Annotation missing" in called_with[2]

        # AUTO_FIX must NOT resolve at classification time — resolve happens
        # post-fix verification when the diff is checked
        vcs.resolve_comment.assert_not_awaited()


class TestEscalateFixDecisionReply:
    @pytest.mark.asyncio
    async def test_operator_fix_decision_posts_will_fix_reply(self, tmp_path):
        from orchestrator.orchestrator import Orchestrator, Stage

        orch = _orch()

        vcs = MagicMock()
        vcs.reply_to_comment = AsyncMock()
        vcs.resolve_comment = AsyncMock()
        orch._get_vcs_for_workspace = MagicMock(return_value=(vcs, None))

        ws = MagicMock()
        ws.reports_dir = tmp_path
        ws.state = SimpleNamespace(
            ticket_id="T-1", pr_number=42, review_cycle=1, stage_iterations={},
            pending_review_comments=[
                {"comment_id": 7, "msg_ids": [100], "decision": "fix",
                 "author": "C", "file": "x.kt", "line": 10, "body": "b",
                 "reason": "Reviewer is right — annotation needed.", "verdict": "Valid",
                 "hint_rounds": 0, "last_hint": None,
                 "pending_reinvestigation": False},
            ],
        )
        ws.save_state = MagicMock()

        await orch._execute_review_decisions(ws)

        vcs.reply_to_comment.assert_awaited()
        called_with = vcs.reply_to_comment.call_args.args
        assert called_with[0] == 42
        assert called_with[1] == 7
        assert called_with[2].startswith("Will fix: ")
        assert "annotation needed" in called_with[2].lower()

        # Don't resolve until the fix is verified post-push
        vcs.resolve_comment.assert_not_awaited()
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/unit/test_github_reply_at_decision.py -v`
Expected: FAIL — no "Will fix" reply is posted today.

- [ ] **Step 3: Add "Will fix" reply in AUTO_FIX classification block**

In `orchestrator/orchestrator.py`, locate the AUTO_FIX branch in `_action_fetch_pr_comments` (around line 1583-1594). Replace:

```python
if cc.classification == "AUTO_FIX":
    add_entry(report_path, state.ticket_id, pr_number, cc.comment_id, {
        "classification": "AUTO_FIX",
        "file": cc.file or "",
        "line": str(cc.line or "?"),
        "author": cc.author or "",
        "reason": cc.reason or "",
        "verified": "PENDING",
        "fail_count": "0",
        "cycle": str(state.review_cycle),
    })
    auto_fixed.append(cc)
```

with:

```python
if cc.classification == "AUTO_FIX":
    if vcs:
        try:
            await vcs.reply_to_comment(
                pr_number, cc.comment_id, f"Will fix: {cc.reason}",
            )
        except Exception as e:
            logger.warning("Failed to post 'Will fix' on comment %d: %s", cc.comment_id, e)
    add_entry(report_path, state.ticket_id, pr_number, cc.comment_id, {
        "classification": "AUTO_FIX",
        "file": cc.file or "",
        "line": str(cc.line or "?"),
        "author": cc.author or "",
        "reason": cc.reason or "",
        "verified": "PENDING",
        "github_reply": "Posted (will fix)",
        "fail_count": "0",
        "cycle": str(state.review_cycle),
    })
    auto_fixed.append(cc)
```

- [ ] **Step 4: Add "Will fix" reply in `_execute_review_decisions` FIX branch**

In `orchestrator/orchestrator.py` `_execute_review_decisions` (around line 1735-1742), replace the FIX branch:

```python
if _is_fix(decision):
    fixes_needed.append(c)
    update_entry(report_path, cid, {
        "decision": "FIX",
        "verified": "PENDING",
        "fail_count": "0",
        "decided_at": self._now(),
    })
```

with:

```python
if _is_fix(decision):
    fixes_needed.append(c)
    if vcs and pr_number:
        try:
            await vcs.reply_to_comment(
                pr_number, cid, f"Will fix: {c.get('reason','operator decision')}",
            )
        except Exception as e:
            logger.warning("Failed to post 'Will fix' on comment %d: %s", cid, e)
    update_entry(report_path, cid, {
        "decision": "FIX",
        "verified": "PENDING",
        "github_reply": "Posted (will fix)",
        "fail_count": "0",
        "decided_at": self._now(),
    })
```

- [ ] **Step 5: Run — must pass**

Run: `pytest tests/unit/test_github_reply_at_decision.py -v`
Expected: PASS.

Run regression check on existing PR-review tests:

`pytest tests/unit/test_pr_comment_decision_echo.py tests/unit/test_reinvestigation.py tests/unit/test_unanswered_recall.py tests/unit/test_escalation_view.py tests/unit/test_comment_classifier.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_github_reply_at_decision.py
git commit -m "feat(pr-review): post 'Will fix' GitHub reply at decision time"
```

---

## Task 16: Rename and update old SKIP test file

**Files:**
- Rename: `tests/unit/test_review_decisions_skip.py` → `tests/unit/test_review_decisions_freetext.py`
- Modify: the renamed file's contents

- [ ] **Step 1: Read the existing test file**

Run: `cat tests/unit/test_review_decisions_skip.py`

Identify which assertions reference SKIP semantics — they need to become assertions about the re-investigation staging path.

- [ ] **Step 2: Rename the file**

```bash
git mv tests/unit/test_review_decisions_skip.py tests/unit/test_review_decisions_freetext.py
```

- [ ] **Step 3: Replace SKIP assertions**

For any test that previously asserted "free-text reply records SKIP", rewrite it to assert "free-text reply stages re-investigation":

```python
@pytest.mark.asyncio
async def test_free_text_reply_stages_reinvestigation():
    """Free-text replies that aren't fix/won't-fix must stage re-investigation,
    not silently SKIP. Regression for the operator-confused-by-SKIP bug."""
    # ... setup similar to existing tests ...
    handler = CommandHandler.__new__(CommandHandler)
    # ... fields ...
    c = {"comment_id": 1, "msg_ids": [100], "decision": None,
         "author": "C", "file": "x.kt", "line": 1, "body": "b", "reason": "r",
         "verdict": "Valid", "hint_rounds": 0, "last_hint": None,
         "pending_reinvestigation": False}
    ws = MagicMock()
    ws.state = SimpleNamespace(
        current_state="PR_REVIEW", ticket_id="T-1",
        pending_review_comments=[c],
    )
    handler._active_workspaces_fn = lambda: [ws]
    handler._notifier = MagicMock()
    handler._notifier.send_message = AsyncMock()
    handler._events = None
    handler._wake_fn = MagicMock()
    handler._allowed_chat_ids = None

    await handler.handle_reply(reply_to_msg_id=100, text="check repo X first", chat_id="c-1")

    assert c["decision"] is None  # NOT skip
    assert c["pending_reinvestigation"] is True
    assert c["last_hint"] == "check repo X first"
```

Drop any test that asserted "SKIP routes to AWAITING_APPROVAL" — that flow remains valid for the case when all escalated comments are eventually given non-fix/non-won't-fix outcomes that fall through to skip in `_execute_review_decisions`. **However:** with this change, free-text never reaches `_execute_review_decisions` as a "skip" — it loops through re-investigation. The only path to the AWAITING_APPROVAL skip-pile-up flow is operator-side cap exhaustion followed by buttons. Keep the existing AWAITING_APPROVAL test if it asserts the orchestrator behavior on real `decision="skip"` entries; check `_execute_review_decisions` to confirm the path is still reachable. If not, delete that test.

Actually — `_execute_review_decisions` skip branch still triggers when `decision` doesn't match fix or won't-fix. With Task 7's changes, command_handler never writes `decision="skip"` anymore. The branch could still be reached via stale state files. **Decision: keep the SKIP branch in `_execute_review_decisions` as a defensive fallback for old state files, but remove any test that creates new SKIP decisions through the reply path.**

- [ ] **Step 4: Run all tests**

Run: `pytest tests/unit/test_review_decisions_freetext.py -v`
Expected: PASS.

Run full PR-review test sweep:

`pytest tests/unit/test_review_decisions_freetext.py tests/unit/test_pr_comment_decision_echo.py tests/unit/test_unanswered_recall.py tests/unit/test_reinvestigation.py tests/unit/test_escalation_view.py tests/unit/test_comment_classifier.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_review_decisions_freetext.py
git commit -m "test(pr-review): rename skip-flow test to free-text + stage re-investigation"
```

---

## Task 17: Documentation

**Files:**
- Modify: `docs/features/agent-system.md`
- Modify: `docs/superpowers/specs/2026-04-21-pr-review-comment-resolution-design.md`

- [ ] **Step 1: Add a paragraph in agent-system.md under the PR review section**

Find the existing PR-review description in `docs/features/agent-system.md` (search with `grep -n "PR.review\|pr-comment\|escalat" docs/features/agent-system.md`).

Insert this paragraph at the appropriate place:

```markdown
### PR Comment Verdict and Hints

Each escalated comment carries a one-word verdict — **Valid** (the reviewer is correct) or **Not valid** (the reviewer is mistaken) — alongside the agent's reasoning. The verdict is the agent's own lean; the operator decides what to do.

If the operator replies with neither `fix` nor `won't fix`, the free text is treated as a hint: the comment is re-classified by the responder agent with the operator's hint as context. Capped at 3 rounds per comment.

Operators can recall pending comments via the `/unanswered` command (or `/unanswered <TICKET>` for one ticket) — both paths re-send each undecided comment with fresh Fix / Won't Fix buttons. Replies match against the original message OR any recall.
```

- [ ] **Step 2: Add a "see also" line to the predecessor spec**

In `docs/superpowers/specs/2026-04-21-pr-review-comment-resolution-design.md`, near the top after the "Status" line, add:

```markdown
**See also:** [2026-04-30 PR Review Flow Improvements](2026-04-30-pr-review-flow-improvements-design.md) — adds verdict, re-investigation, and recall on top of this base flow.
```

- [ ] **Step 3: Commit**

```bash
git add docs/features/agent-system.md docs/superpowers/specs/2026-04-21-pr-review-comment-resolution-design.md
git commit -m "docs(pr-review): document verdict, hints, and unanswered recall"
```

---

## Final Verification

- [ ] **Run the full test suite**

Run: `pytest tests/ -v`
Expected: PASS — no regressions in unrelated tests.

- [ ] **Inspect the diff**

Run: `git log --oneline master..HEAD`
Expected: ~17 atomic commits, one per task.

- [ ] **Manual smoke test (optional, requires running pipeline)**

If a workspace is in PR_REVIEW with an escalated comment:

1. Reply with `yes` → confirm "Recognized as FIX (matched: 'yes')".
2. Reply with `don't fix this is intentional` → confirm "Recognized as WON'T FIX (matched: 'don't fix')".
3. Reply with `check repo Y for the pattern` → confirm "🔍 Recognized as hint (round 1/3). Re-checking…" then a new escalation message ~30-60s later with updated verdict/reason.
4. After 3 hints on the same comment, the 4th gets "Hint loop exceeded".
5. Type `/unanswered` → all undecided comments re-sent with fresh buttons.
6. Reply to a recall message → resolves the same comment as the original.
