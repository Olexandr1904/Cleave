# Plan-review gate + pipeline telemetry — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a cold-context `plan_review` stage that reviews `ba.md` before any code exists, and record enough per-ticket data to decide the ticket-tiering question on evidence.

**Architecture:** A new pipeline stage between `analysis` and `dev`, wired the same way `code_review` was: a `Stage` enum member, a row in the transition matrix, a stage→state map entry, a YAML stage, a read-only agent prompt, and entries in every place that enumerates stages. Its verdict is parsed by its own strict, fail-closed matcher rather than the loose shared one. Telemetry is a one-line fix plus a read-only reporting script over the existing SQLite event store.

**Tech Stack:** Python 3.10+, pytest 8.3.4 (`asyncio_mode = "auto"`), PyYAML, aiosqlite, vanilla ES modules for the dashboard.

**Spec:** [docs/superpowers/specs/2026-09-11-plan-review-stage-design.md](../specs/2026-09-11-plan-review-stage-design.md)

## Global Constraints

- **Transition-matrix edits are additive only.** Never remove an existing entry from `VALID_TRANSITIONS` — Telegram can force arbitrary stage transitions and narrowing the matrix breaks those paths.
- **The judge is read-only.** `plan-review-agent` gets `read_file`, `list_directory`, `search_code` and nothing else. No `write_file` (it must not rewrite the plan), no `git_operation` (no diff exists yet).
- **The judge is cold.** `plan_review` never sets `resume_from_previous`.
- **Fail closed.** Any ambiguity in the plan-review verdict resolves to `fail`, never to `pass`.
- **Stack-agnostic checklist.** No mobile/Android/web-specific checks — Cleave's stages carry no stack assumptions.
- Run the suite with `python -m pytest` from the repo root. Lint with `ruff check .` (line-length 100).
- Do not fix the pre-existing issues listed in the spec's last section (dead `STAGE_REPORT_FILE`, scope-guard's missing `write_file`, the open tooling P0).

## Task Order

Tasks 1–6 are inert: they add code that nothing calls yet. **Task 7 is the flip** — it is the single commit that puts the stage into the running pipeline, and it must not land before 1–6. Tasks 8–9 are independent of all of the above and may be done at any point.

---

### Task 1: The plan-review agent and its checklist

**Files:**
- Create: `agents/plan-review-agent.md`
- Create: `checklists/plan-review-checklist.md`
- Modify: `agents/README.md` (the "Existing Agents" table)

**Interfaces:**
- Consumes: nothing.
- Produces: agent id `plan-review-agent`, referenced by `workflows/default-workflow.yaml` in Task 7. Checklist id `plan-review-checklist`, resolved by the resource registry from the `id` field in its frontmatter.

Agents are discovered by the resource registry at startup from `agents/*.md`; checklists are resolved by `id`, not by path. Nothing references this agent until Task 7, so this task is safe to land alone.

- [ ] **Step 1: Write the checklist**

Create `checklists/plan-review-checklist.md`:

```markdown
---
checklist:
  id: "plan-review-checklist"
  name: "Plan Review Checklist"
  description: "Validates the implementation plan itself, before any code is written"
---

# Plan Review Checklist

## Form

- [ ] Every acceptance criterion has a pass/fail condition — you can say what
      observation would prove it met
- [ ] No unverifiable language: "works correctly", "is fast", "handles errors
      gracefully", "is robust"
- [ ] The Files to Create / Files to Modify lists are present, concrete (real
      paths, not "the relevant service"), and complete — scope-guard uses this
      list as its allowlist, so a vague or empty list is an automatic FAIL
- [ ] Every task in the plan references an acceptance criterion
- [ ] Assumptions are labelled as assumptions, not stated as established fact
- [ ] Nothing in the plan contradicts rules/arch-rules.md

## Verification against the repository

- [ ] Every file under "Files to Modify" exists in the repo
- [ ] Every file under "Files to Create" does NOT already exist
- [ ] Every symbol, module, class or endpoint the plan names is findable by
      search in the repo, or is explicitly introduced by this plan
```

- [ ] **Step 2: Write the agent prompt**

Create `agents/plan-review-agent.md`. Match the frontmatter shape used by the other agents (`agents/README.md` documents it):

```markdown
---
agent:
  id: "plan-review-agent"
  name: "Surveyor"
  title: "Plan Reviewer"

persona:
  role: "Senior Engineer reviewing a plan before anyone writes code"
  style: "Concrete, evidence-based, brief"
  identity: "Catches the defect that every later gate would faithfully enforce"

core_principles:
  - "The plan is what scope-guard, code-review and QA all measure against — a defect here is invisible to all of them"
  - "Never rewrite the plan. Name the defect and where it is; the BA fixes it"
  - "If the plan is sound, say PASS — never invent findings to look useful"
  - "A short plan for a bug ticket is correct, not a defect. The BA's bar for bugs is deliberately low"
  - "Every finding names a plan section and a concrete reason"

tools:
  - read_file
  - list_directory
  - search_code

inputs:
  - ai_pipeline/{ticket_id}/ba.md
  - meta/ticket.md
  - meta/parent.md
  - meta/comments.md
  - rules/arch-rules.md

outputs:
  - ai_pipeline/{ticket_id}/plan-review-agent-output.md

decision_policy:
  when_to_run: "State is PLAN_REVIEW"
  when_to_skip: "Never"
  success_outcome: "Status: PASS → State → DEV"
  failure_outcome: "Status: FAIL → State → ANALYSIS with a defect list"
  max_iterations: 2

dependencies:
  tasks: []
  checklists:
    - "plan-review-checklist"
---

# Plan Review Agent — Reviewing the Plan Before the Code

## Activation

You are Surveyor. You review an implementation plan before a single line of
code has been written. You are the only gate on this plan: scope-guard will
enforce its file list, the dev will build what it says, code-review will judge
the result against it, and QA will test what it specified. If the plan is
wrong, all four of them will faithfully deliver the wrong thing with every
check green.

You are deliberately given no write access. You do not fix the plan. You
describe what is wrong with it precisely enough that the BA can.

## Input

- `ai_pipeline/{ticket_id}/ba.md` — the plan under review
- `meta/ticket.md` — what was actually asked for
- `meta/parent.md`, `meta/comments.md` — context, if present
- `rules/arch-rules.md` — architecture constraints (READ ONLY)
- The repository itself — read it

## Process

### Step 1: Read the ticket, then the plan

Read the ticket first. You are judging whether the plan serves *this* request,
not whether it is a nice plan in the abstract.

### Step 2: Run the form checks

Work through the Form section of `plan-review-checklist`. The file list matters
most: scope-guard takes its allowlist straight from it, so a plan with a vague
or missing file list disarms the next gate as well as this one.

### Step 3: Verify the plan against the real repository

Work through the Verification section. Read and search the repo. A plan that
names files, modules or symbols that do not exist was written against an
imagined codebase, and no later gate will catch it — the dev will simply
improvise.

### Step 4: Decide

If the plan is sound, PASS. Do not manufacture findings to look useful; a gate
that always finds something gets ignored, and then it protects nothing.

Bug tickets usually carry a short plan. That is correct — the BA is instructed
to keep the bar low for bugs. Judge whether the plan is *sufficient for this
ticket*, not whether it is long.

## Output

Write your report as your response. The first line must be exactly one of:

```
Status: PASS
```

```
Status: FAIL
```

This line is parsed by the orchestrator. Anything else — a missing line,
"Status: mostly pass", a verdict buried mid-paragraph — is read as FAIL and
the plan is returned to the BA.

On FAIL, follow the verdict line with the defects, one per block:

```
### <plan section>
<what is wrong, and why it matters downstream>
```

## Constraints

- NEVER edit `ba.md` or any other file. You have no write tools; do not attempt
  workarounds.
- NEVER comment on code style, naming or formatting — there is no code yet.
- NEVER flag a plan as defective solely for being short.
- NEVER put anything before the `Status:` line.
```

- [ ] **Step 3: Add the agent to the README table**

In `agents/README.md`, add a row to the "Existing Agents" table, after the `scope-guard-agent` row:

```markdown
| Surveyor | `plan-review-agent` | Reviews the BA's implementation plan before any code exists; bounces defective plans back to analysis |
```

- [ ] **Step 4: Verify the resource registry loads both files**

Run: `python -c "from config.resource_registry import *; print('import ok')"`
Expected: `import ok` (this only proves the module imports; the registry's own discovery is exercised by the daemon at startup and by Task 7's smoke run).

Run: `python -c "
import yaml, pathlib
for p in ['agents/plan-review-agent.md', 'checklists/plan-review-checklist.md']:
    text = pathlib.Path(p).read_text()
    fm = text.split('---')[1]
    print(p, '->', yaml.safe_load(fm))
"`
Expected: both files print parsed frontmatter with no YAML error, `plan-review-agent` and `plan-review-checklist` visible as ids.

- [ ] **Step 5: Commit**

```bash
git add agents/plan-review-agent.md checklists/plan-review-checklist.md agents/README.md
git commit -m "feat(agents): add plan-review-agent and its checklist

Read-only judge for the implementation plan. Not yet referenced by the
workflow; wiring lands separately."
```

---

### Task 2: `Stage.PLAN_REVIEW` in the state machine

**Files:**
- Modify: `workspace/workspace.py:15-32` (the `Stage` enum), `workspace/workspace.py:40-54` (`VALID_TRANSITIONS`)
- Test: `tests/unit/test_workspace.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Stage.PLAN_REVIEW` (value `"PLAN_REVIEW"`), used by every later task.

`tests/unit/test_workspace.py:634-640` already contains the equivalent tests written when `CODE_REVIEW` was added — mirror that style.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_workspace.py`:

```python
class TestPlanReviewTransitions:
    """PLAN_REVIEW sits between ANALYSIS and DEV; it bounces back to ANALYSIS."""

    def test_analysis_can_reach_plan_review(self):
        from workspace.workspace import VALID_TRANSITIONS, Stage
        assert Stage.PLAN_REVIEW in VALID_TRANSITIONS[Stage.ANALYSIS]

    def test_plan_review_can_pass_to_dev_and_bounce_to_analysis(self):
        from workspace.workspace import VALID_TRANSITIONS, Stage
        assert Stage.DEV in VALID_TRANSITIONS[Stage.PLAN_REVIEW]
        assert Stage.ANALYSIS in VALID_TRANSITIONS[Stage.PLAN_REVIEW]

    def test_plan_review_can_await_approval(self):
        """Manual mode gates here, so the state must be reachable."""
        from workspace.workspace import VALID_TRANSITIONS, Stage
        assert Stage.AWAITING_APPROVAL in VALID_TRANSITIONS[Stage.PLAN_REVIEW]

    def test_paused_states_can_resume_into_plan_review(self):
        from workspace.workspace import VALID_TRANSITIONS, Stage
        for paused in (Stage.BLOCKED, Stage.FAILED, Stage.AWAITING_APPROVAL,
                       Stage.DEFERRED, Stage.PAUSED):
            assert Stage.PLAN_REVIEW in VALID_TRANSITIONS[paused], (
                f"{paused} cannot resume into PLAN_REVIEW"
            )

    def test_analysis_to_dev_still_allowed(self):
        """Additive change: the old edge stays for forced Telegram transitions."""
        from workspace.workspace import VALID_TRANSITIONS, Stage
        assert Stage.DEV in VALID_TRANSITIONS[Stage.ANALYSIS]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_workspace.py::TestPlanReviewTransitions -v`
Expected: FAIL with `AttributeError: PLAN_REVIEW` (the enum member does not exist yet).

- [ ] **Step 3: Add the enum member**

In `workspace/workspace.py`, inside `class Stage(StrEnum)`, add `PLAN_REVIEW` directly after `ANALYSIS` so the enum reads in pipeline order:

```python
    ANALYSIS = "ANALYSIS"
    PLAN_REVIEW = "PLAN_REVIEW"
    DEV = "DEV"
```

- [ ] **Step 4: Extend the transition matrix**

In `VALID_TRANSITIONS`, add `Stage.PLAN_REVIEW` to the `Stage.ANALYSIS` target set, insert the new row after it, and add `Stage.PLAN_REVIEW` to each paused-state row. Add only — remove nothing:

```python
    Stage.ANALYSIS:           {Stage.PLAN_REVIEW, Stage.DEV, Stage.BLOCKED, Stage.FAILED, Stage.DEFERRED, Stage.AWAITING_APPROVAL, Stage.MANUAL_CONTROL, Stage.PAUSED},
    Stage.PLAN_REVIEW:        {Stage.DEV, Stage.ANALYSIS, Stage.BLOCKED, Stage.FAILED, Stage.DEFERRED, Stage.AWAITING_APPROVAL, Stage.MANUAL_CONTROL, Stage.PAUSED},
```

and add `Stage.PLAN_REVIEW` to the target sets of `Stage.BLOCKED`, `Stage.FAILED`, `Stage.AWAITING_APPROVAL`, `Stage.DEFERRED` and `Stage.PAUSED` (insert it after `Stage.ANALYSIS` in each set).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_workspace.py -v`
Expected: PASS, including the pre-existing `test_all_valid_transitions`.

- [ ] **Step 6: Commit**

```bash
git add workspace/workspace.py tests/unit/test_workspace.py
git commit -m "feat(workspace): add Stage.PLAN_REVIEW and its transitions

Additive only — the ANALYSIS -> DEV edge stays so forced Telegram
transitions keep working."
```

---

### Task 3: Constants, stage verifier, and the stage→state map

**Files:**
- Modify: `orchestrator/constants.py`, `orchestrator/stage_verifier.py:110-115`, `orchestrator/pipeline/driver.py:29-39`
- Test: `tests/unit/test_orchestrator_stage_verify.py`

**Interfaces:**
- Consumes: `Stage.PLAN_REVIEW` (Task 2).
- Produces: `RUNTIME_OUTPUT_PLAN_REVIEW = "plan-review-agent-output.md"`, registered in `STAGE_RUNTIME_OUTPUT["plan_review"]`; `_STAGE_TO_STATE["plan_review"] = Stage.PLAN_REVIEW`. Task 4 imports the constant.

Do **not** add an entry to `STAGE_REPORT_FILE` — that map has no callers and the agent has no write tools.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_orchestrator_stage_verify.py`:

```python
def test_plan_review_verify_requires_report(tmp_path: Path) -> None:
    """A plan_review run that produced no output must not be treated as verified."""
    from orchestrator import stage_verifier
    from orchestrator.constants import RUNTIME_OUTPUT_PLAN_REVIEW

    ws = MagicMock()
    ws.reports_dir = tmp_path
    ws.state = SimpleNamespace(ticket_id="T-1")

    assert stage_verifier.verify("plan_review", ws, None).ok is False

    (tmp_path / RUNTIME_OUTPUT_PLAN_REVIEW).write_text("Status: PASS", encoding="utf-8")
    assert stage_verifier.verify("plan_review", ws, None).ok is True
```

Add `from pathlib import Path`, `from types import SimpleNamespace` and `from unittest.mock import MagicMock` at the top of the file if they are not already imported.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_orchestrator_stage_verify.py::test_plan_review_verify_requires_report -v`
Expected: FAIL with `ImportError: cannot import name 'RUNTIME_OUTPUT_PLAN_REVIEW'`.

- [ ] **Step 3: Add the constants**

In `orchestrator/constants.py`, add the runtime-output constant next to the others and register it in `STAGE_RUNTIME_OUTPUT`:

```python
RUNTIME_OUTPUT_PLAN_REVIEW = "plan-review-agent-output.md"
```

```python
STAGE_RUNTIME_OUTPUT: dict[str, str] = {
    "analysis": RUNTIME_OUTPUT_BA,
    "plan_review": RUNTIME_OUTPUT_PLAN_REVIEW,
    "dev": RUNTIME_OUTPUT_DEV,
    ...
}
```

- [ ] **Step 4: Add the verifier branch**

In `orchestrator/stage_verifier.py`, import `RUNTIME_OUTPUT_PLAN_REVIEW` alongside the existing constants and add the branch next to the `code_review` one:

```python
    if stage_id == "plan_review":
        return _verify_report_exists("plan_review", workspace, RUNTIME_OUTPUT_PLAN_REVIEW)
```

- [ ] **Step 5: Add the stage→state map entry**

In `orchestrator/pipeline/driver.py`, in `_STAGE_TO_STATE`, insert after `"analysis"`:

```python
    "plan_review": Stage.PLAN_REVIEW,
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_orchestrator_stage_verify.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/constants.py orchestrator/stage_verifier.py orchestrator/pipeline/driver.py tests/unit/test_orchestrator_stage_verify.py
git commit -m "feat(pipeline): register plan_review report, verifier and state map"
```

---

### Task 4: Strict, fail-closed verdict parsing for `plan_review`

**Files:**
- Modify: `orchestrator/pipeline/agent_stage.py:1-30` (imports), `orchestrator/pipeline/agent_stage.py:81-100` (matchers), `orchestrator/pipeline/agent_stage.py:114-165` (`parse_agent_outcome`)
- Test: `tests/unit/test_orchestrator_outcome_parser.py`

**Interfaces:**
- Consumes: `RUNTIME_OUTPUT_PLAN_REVIEW` (Task 3).
- Produces: `parse_agent_outcome("plan_review", output, workspace)` returning `"pass"` only on an exact `Status: PASS` verdict line, and `"fail"` for everything else.

`plan_review` gets its own branch rather than joining the `("scope_check", "code_review", "qa")` tuple. The shared `_looks_like_pass` accepts the substring `"advances to"` and treats `"verdict: hold"` as a pass, and `parse_agent_outcome` checks pass **before** fail — so a report reading "the plan advances to dev only if…" would be read as PASS. That is the exact inversion this gate exists to prevent. The existing matcher is left untouched for the stages that already depend on its quirks.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_orchestrator_outcome_parser.py`:

```python
def test_plan_review_pass_requires_exact_verdict_line(tmp_path: Path) -> None:
    from orchestrator.constants import RUNTIME_OUTPUT_PLAN_REVIEW
    (tmp_path / RUNTIME_OUTPUT_PLAN_REVIEW).write_text(
        "Status: PASS\n\nThe plan is sound.", encoding="utf-8",
    )
    assert parse_agent_outcome("plan_review", "", _ws(tmp_path)) == "pass"


def test_plan_review_fail_verdict(tmp_path: Path) -> None:
    from orchestrator.constants import RUNTIME_OUTPUT_PLAN_REVIEW
    (tmp_path / RUNTIME_OUTPUT_PLAN_REVIEW).write_text(
        "Status: FAIL\n\n### Files to Modify\nNo file list.", encoding="utf-8",
    )
    assert parse_agent_outcome("plan_review", "", _ws(tmp_path)) == "fail"


def test_plan_review_does_not_inherit_loose_pass_matcher(tmp_path: Path) -> None:
    """'advances to' is a pass keyword for the other stages. Not for this one."""
    from orchestrator.constants import RUNTIME_OUTPUT_PLAN_REVIEW
    (tmp_path / RUNTIME_OUTPUT_PLAN_REVIEW).write_text(
        "Status: FAIL\n\nThe plan advances to dev only if the file list is fixed.",
        encoding="utf-8",
    )
    assert parse_agent_outcome("plan_review", "", _ws(tmp_path)) == "fail"


def test_plan_review_fails_closed_on_missing_report(tmp_path: Path) -> None:
    assert parse_agent_outcome("plan_review", "looks good to me", _ws(tmp_path)) == "fail"


def test_plan_review_fails_closed_on_malformed_verdict(tmp_path: Path) -> None:
    from orchestrator.constants import RUNTIME_OUTPUT_PLAN_REVIEW
    (tmp_path / RUNTIME_OUTPUT_PLAN_REVIEW).write_text(
        "Status: mostly pass\n\nGood enough.", encoding="utf-8",
    )
    assert parse_agent_outcome("plan_review", "", _ws(tmp_path)) == "fail"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_orchestrator_outcome_parser.py -v -k plan_review`
Expected: FAIL — `parse_agent_outcome("plan_review", …)` currently falls through to the `return "default"` branch, so the assertions comparing to `"pass"`/`"fail"` fail.

- [ ] **Step 3: Add the strict matcher**

In `orchestrator/pipeline/agent_stage.py`, add `import re` to the stdlib imports and `RUNTIME_OUTPUT_PLAN_REVIEW` to the `orchestrator.constants` import line. Then add, next to `_looks_like_fail`:

```python
# plan_review deliberately does NOT reuse _looks_like_pass: that matcher accepts
# "advances to" and treats "verdict: hold" as a pass, and parse_agent_outcome
# checks pass before fail. A plan-review report reading "the plan advances to dev
# only if..." would be read as PASS — the exact inversion this gate exists to
# prevent. Strict line match, and anything unparseable fails closed.
_PLAN_REVIEW_VERDICT = re.compile(r"^status:[ \t]*(pass|fail)[ \t]*$", re.IGNORECASE | re.MULTILINE)


def _parse_plan_review_outcome(workspace: Workspace) -> str:
    """Return 'pass' only for an explicit `Status: PASS` line; else 'fail'."""
    report = workspace.reports_dir / RUNTIME_OUTPUT_PLAN_REVIEW
    if not report.exists():
        return "fail"
    match = _PLAN_REVIEW_VERDICT.search(report.read_text(encoding="utf-8"))
    if match is None:
        return "fail"
    return "pass" if match.group(1).lower() == "pass" else "fail"
```

- [ ] **Step 4: Dispatch to it from `parse_agent_outcome`**

In `parse_agent_outcome`, add the branch immediately before the `if stage_id in ("scope_check", "code_review", "qa"):` block:

```python
    if stage_id == "plan_review":
        return _parse_plan_review_outcome(workspace)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_orchestrator_outcome_parser.py -v`
Expected: PASS, including the five pre-existing tests.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/pipeline/agent_stage.py tests/unit/test_orchestrator_outcome_parser.py
git commit -m "feat(pipeline): strict fail-closed verdict parsing for plan_review

The shared pass matcher accepts 'advances to' and checks pass before
fail, which would invert this gate's verdict. plan_review gets its own
exact-line matcher instead."
```

---

### Task 5: Operator surface — dashboard and Telegram

**Files:**
- Modify: `dashboard/web.py:399-404`, `dashboard/actions.py:28`, `dashboard/actions.py:306-307`, `dashboard/static/js/helpers.js:58`, `dashboard/static/js/helpers.js:79-86`, `dashboard/static/js/board.js:72-76`, `dashboard/static/js/board.js:338`, `dashboard/static/js/detail.js:114`, `dashboard/static/style.css:295-308`, `integrations/telegram/handlers/status.py:10-17`, `integrations/telegram/command_handler.py:349-356`, `integrations/telegram/command_handler.py:628-633`
- Test: `tests/unit/test_dashboard_actions.py`

**Interfaces:**
- Consumes: `Stage.PLAN_REVIEW` (Task 2).
- Produces: no new symbols — every change adds `PLAN_REVIEW` to an existing collection.

This must land **before** Task 7, or the first ticket to enter the stage renders as an unknown state and cannot be paused or retried.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_dashboard_actions.py`:

```python
def test_plan_review_is_a_counter_stage() -> None:
    """Retrying into plan_review must clear its iteration counter."""
    from dashboard.actions import _COUNTER_STAGE_KEYS
    assert "plan_review" in _COUNTER_STAGE_KEYS
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/unit/test_dashboard_actions.py::test_plan_review_is_a_counter_stage -v`
Expected: FAIL — `assert 'plan_review' in {...}`.

- [ ] **Step 3: Update the Python surface**

`dashboard/actions.py:28`:

```python
_COUNTER_STAGE_KEYS = {"analysis", "plan_review", "dev", "scope_check", "code_review", "qa"}
```

`dashboard/actions.py:306-307` — add to `PAUSEABLE`:

```python
        PAUSEABLE = {Stage.ANALYSIS, Stage.PLAN_REVIEW, Stage.DEV, Stage.SCOPE_CHECK,
                     Stage.CODE_REVIEW, Stage.QA, Stage.PUSHED, Stage.PR_REVIEW}
```

`dashboard/web.py:399-404` — insert `PLAN_REVIEW` into `state_order` between `ANALYSIS` and `SCOPE_CHECK`, renumbering the entries after it:

```python
        state_order = {
            Stage.BLOCKED: 0, Stage.AWAITING_APPROVAL: 1, Stage.DEV: 2, Stage.ANALYSIS: 3,
            Stage.PLAN_REVIEW: 4, Stage.SCOPE_CHECK: 5, Stage.CODE_REVIEW: 6, Stage.QA: 7,
            Stage.PR_REVIEW: 8, Stage.PUSHED: 9,
            Stage.NEW: 9, Stage.DONE: 10, Stage.FAILED: 11, Stage.ARCHIVED: 12,
        }
```

`integrations/telegram/handlers/status.py:10-17` — add to `_FRIENDLY`:

```python
    "PLAN_REVIEW": "Plan Review",
```

`integrations/telegram/command_handler.py:349-356` — add to `_VALID_RETRY_STATES`:

```python
        "plan_review": Stage.PLAN_REVIEW,
```

`integrations/telegram/command_handler.py:628-633` — the `skip` map must route `PLAN_REVIEW` forward to `DEV`, and `ANALYSIS` now skips into `PLAN_REVIEW`:

```python
                    _NEXT = {
                        Stage.ANALYSIS: Stage.PLAN_REVIEW,
                        Stage.PLAN_REVIEW: Stage.DEV,
                        Stage.DEV: Stage.SCOPE_CHECK,
                        Stage.SCOPE_CHECK: Stage.CODE_REVIEW,
                        Stage.CODE_REVIEW: Stage.QA, Stage.QA: Stage.PUSHED,
                        Stage.PUSHED: Stage.PR_REVIEW, Stage.PR_REVIEW: Stage.DONE,
                    }
```

- [ ] **Step 4: Update the JavaScript surface**

`dashboard/static/js/helpers.js:58`:

```javascript
export const PIPELINE_STAGES = ['NEW', 'ANALYSIS', 'PLAN_REVIEW', 'DEV', 'SCOPE_CHECK', 'CODE_REVIEW', 'QA', 'PUSHED', 'PR_REVIEW', 'DONE'];
```

`dashboard/static/js/helpers.js:79-86` — add to `STATE_LABELS`:

```javascript
  PLAN_REVIEW: 'PLAN REVIEW',
```

`dashboard/static/js/board.js:72-76` — insert into `stateOrder` after `ANALYSIS`, renumbering what follows:

```javascript
    const stateOrder = {
      BLOCKED: 0, AWAITING_APPROVAL: 1, DEFERRED: 2, MANUAL_CONTROL: 3, PAUSED: 4,
      DEV: 5, ANALYSIS: 6, PLAN_REVIEW: 7, SCOPE_CHECK: 8, CODE_REVIEW: 9, QA: 10,
      PR_REVIEW: 11, PUSHED: 12,
      NEW: 12, FAILED: 13, DONE: 14, SETUP_DONE: 14, ARCHIVED: 15,
    };
```

`dashboard/static/js/board.js:338` and `dashboard/static/js/detail.js:114` — add `'PLAN_REVIEW'` to both `PAUSEABLE_STATES` arrays:

```javascript
  const PAUSEABLE_STATES = ['ANALYSIS', 'PLAN_REVIEW', 'DEV', 'SCOPE_CHECK', 'CODE_REVIEW', 'QA', 'PUSHED', 'PR_REVIEW'];
```

`dashboard/static/style.css` — add a badge colour next to `.state-ANALYSIS`, reusing the analysis blue family since the stage is part of planning:

```css
  .state-PLAN_REVIEW{ background: #1a2d3d; color: #79c0ff; border: 1px solid #388bfd; }
```

Note: `.state-CODE_REVIEW` has no rule and falls back to unstyled. That is pre-existing — do not add one.

- [ ] **Step 5: Run the dashboard tests**

Run: `python -m pytest tests/unit/test_dashboard_actions.py tests/unit/test_dashboard_web.py tests/unit/test_command_handler.py tests/unit/test_handler_status.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add dashboard/ integrations/telegram/ tests/unit/test_dashboard_actions.py
git commit -m "feat(ui): surface PLAN_REVIEW in dashboard and Telegram

Lands before the stage is activated so the first ticket through it
renders, pauses and retries correctly."
```

---

### Task 6: The BA consumes the plan-review verdict

**Files:**
- Modify: `agents/ba-agent.md:24-28` (`inputs`), and its `## Input` and `## Process` sections

**Interfaces:**
- Consumes: `plan-review-agent-output.md`, written by the runtime in Task 1's agent run.
- Produces: nothing code-level.

`ba-agent` has no `resume_from_previous`, so on a bounce it starts cold and reads from disk. This mirrors how `dev-agent` already consumes `scope-guard.md` (`agents/dev-agent.md:26-33`). No new plumbing.

- [ ] **Step 1: Add the input**

In `agents/ba-agent.md`, extend the frontmatter `inputs` list:

```yaml
inputs:
  - meta/ticket.md
  - meta/parent.md
  - meta/comments.md
  - rules/arch-rules.md
  - ai_pipeline/{ticket_id}/plan-review-agent-output.md
```

- [ ] **Step 2: Document it in the `## Input` section**

Add to the bullet list under `## Input`:

```markdown
- `ai_pipeline/{ticket_id}/plan-review-agent-output.md` — present only when a
  previous plan of yours was rejected. Its defect list is what you must fix.
```

- [ ] **Step 3: Add the amend-don't-rewrite instruction to `## Process`**

Insert a new step at the very start of the `## Process` section, before `### Step 1: Validate Requirements`:

```markdown
### Step 0: Check for a rejected plan

If `ai_pipeline/{ticket_id}/plan-review-agent-output.md` exists and its verdict
line reads `Status: FAIL`, your previous plan was rejected. Read `ba.md` and the
defect list, and **correct the named defects in the existing plan**. Do not
start over — a rewritten plan loses the parts that were already accepted and
usually reintroduces defects the reviewer did not mention.
```

- [ ] **Step 4: Verify the frontmatter still parses**

Run: `python -c "
import yaml, pathlib
fm = pathlib.Path('agents/ba-agent.md').read_text().split('---')[1]
d = yaml.safe_load(fm)
assert 'ai_pipeline/{ticket_id}/plan-review-agent-output.md' in d['inputs'], d['inputs']
print('ok')
"`
Expected: `ok`.

- [ ] **Step 5: Commit**

```bash
git add agents/ba-agent.md
git commit -m "feat(ba): amend the plan from plan-review defects instead of rewriting"
```

---

### Task 7: The flip — workflow graph and approval gate

**Files:**
- Modify: `workflows/default-workflow.yaml`, `orchestrator/approval_gate.py:17-22`, `integrations/telegram/handlers/approval.py:10-14`
- Test: `tests/unit/test_workflow_router.py`, `tests/unit/test_orchestrator_modes.py`

**Interfaces:**
- Consumes: everything from Tasks 1–6.
- Produces: a live `analysis → plan_review → dev` pipeline.

The two changes ship together on purpose. Inserting the stage silently disables the existing manual-mode gate — it fires only when the happy-path next stage is exactly `"dev"` — and moving the gate before the stage exists would leave manual mode with no plan gate at all. Either alone is a regression; together they are the feature.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_workflow_router.py`, update `test_all_stages_present` and `test_happy_path`, and add the new cases:

```python
    def test_all_stages_present(self, workflow):
        expected = {"analysis", "plan_review", "dev", "scope_check", "code_review", "qa",
                    "push", "pr_review", "escalate", "done"}
        assert set(workflow.stages.keys()) == expected
```

```python
    def test_happy_path(self, workflow):
        """Default workflow progression — analysis now hands off to plan_review."""
        assert get_next_stage("analysis", workflow) == "plan_review"
        assert get_next_stage("plan_review", workflow, "pass") == "dev"
        assert get_next_stage("dev", workflow) == "scope_check"

    def test_plan_review_bounces_to_analysis(self, workflow):
        """A defective plan goes back to the BA, not forward to dev."""
        assert get_next_stage("plan_review", workflow, "fail") == "analysis"

    def test_plan_review_escalates_on_cap(self, workflow):
        assert workflow.stages["plan_review"].max_iterations == 2
        assert get_next_stage("plan_review", workflow, "max_iterations") == "escalate"

    def test_plan_review_is_a_cold_judge(self, workflow):
        """A judge that resumes the author's session inherits the author's excuses."""
        assert workflow.stages["plan_review"].resume_from_previous is False

    def test_plan_review_agent_mapping(self, workflow):
        assert workflow.stages["plan_review"].agent == "plan-review-agent"
```

In `tests/unit/test_orchestrator_modes.py`, replace the ANALYSIS gate cases and add the PLAN_REVIEW ones:

```python
    def test_should_gate_returns_true_for_plan_review_in_manual(self):
        assert self._gate(self._manual(), "PLAN_REVIEW") is True

    def test_analysis_no_longer_gated(self):
        """The plan gate moved to PLAN_REVIEW: the human reviews a vetted plan."""
        assert self._gate(self._manual(), "ANALYSIS") is False

    def test_gate_plan_review_to_dev_happy_path_fires(self):
        assert self._gate(self._manual(), "PLAN_REVIEW", "dev") is True

    def test_gate_plan_review_to_analysis_bypasses(self):
        """A bounced plan is not a happy path — the gate must NOT fire."""
        assert self._gate(self._manual(), "PLAN_REVIEW", "analysis") is False
```

Delete the now-contradicted `test_should_gate_returns_true_for_analysis_in_manual` and `test_gate_analysis_to_dev_happy_path_fires`. Leave `test_gate_analysis_to_escalate_bypasses` and `test_should_gate_returns_false_in_auto` — both still expect `False` and still pass.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_workflow_router.py tests/unit/test_orchestrator_modes.py -v`
Expected: FAIL — `KeyError: 'plan_review'` in the router tests, and `assert False is True` for the PLAN_REVIEW gate tests.

- [ ] **Step 3: Insert the stage into the workflow**

In `workflows/default-workflow.yaml`, change `analysis`'s `next` and add the stage after it:

```yaml
  - id: "analysis"
    agent: "ba-agent"
    description: "Validate requirements, create implementation plan"
    next: "plan_review"
    on_unclear: "escalate"

  # The plan is what scope_check, code_review and qa all measure against, and
  # scope_check takes its file allowlist straight out of it. A defect here is
  # invisible to every later gate: the diff matches its spec, tests go green,
  # and the wrong feature ships. Cold context on purpose — a judge that resumes
  # the author's session inherits the author's justifications.
  - id: "plan_review"
    agent: "plan-review-agent"
    description: "Review the plan itself, before any code exists"
    on_pass: "dev"
    on_fail: "analysis"
    max_iterations: 2
    on_max_iterations: "escalate"
```

- [ ] **Step 4: Move the approval gate**

`orchestrator/approval_gate.py:17-22`:

```python
APPROVAL_GATE_STATES = {Stage.PLAN_REVIEW, Stage.QA}

GATE_HAPPY_PATH_NEXT_STAGE = {
    Stage.PLAN_REVIEW: "dev",
    Stage.QA: "push",
}
```

`integrations/telegram/handlers/approval.py:10-14`:

```python
APPROVAL_NEXT_STATE = {
    "PLAN_REVIEW": "DEV",
    "QA": "PUSHED",
    "PR_REVIEW": "DONE",
}
```

Leave the module docstring's description of gates as-is; it is still accurate.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS. Any other failure is a stage enumeration this plan missed — fix it in this task rather than deferring.

- [ ] **Step 6: Lint**

Run: `ruff check .`
Expected: no findings.

- [ ] **Step 7: Commit**

```bash
git add workflows/default-workflow.yaml orchestrator/approval_gate.py integrations/telegram/handlers/approval.py tests/unit/test_workflow_router.py tests/unit/test_orchestrator_modes.py
git commit -m "feat(pipeline): activate plan_review between analysis and dev

Also moves the manual-mode plan gate from ANALYSIS to PLAN_REVIEW, so
the operator reviews a plan the machine has already vetted. The two
changes ship together: inserting the stage alone would silently disable
the gate, which only fires when the next stage is exactly 'dev'."
```

---

### Task 8: Attribute agent spend to a ticket

**Files:**
- Modify: `orchestrator/agent_runtime.py:416`
- Test: `tests/unit/test_agent_runtime.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `agent_execution_detail` events carrying `ticket_id` and `project_id`, which Task 9's script joins on.

Independent of Tasks 1–7.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_agent_runtime.py`:

```python
def test_agent_execution_detail_carries_ticket_id() -> None:
    """Without ticket_id the cost of a run cannot be attributed to a ticket."""
    import inspect
    from orchestrator import agent_runtime

    src = inspect.getsource(agent_runtime)
    emit_line = next(
        line for line in src.splitlines() if '"agent_execution_detail"' in line
    )
    assert "ticket_id=" in emit_line, emit_line
    assert "project_id=" in emit_line, emit_line
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/unit/test_agent_runtime.py::test_agent_execution_detail_carries_ticket_id -v`
Expected: FAIL — `assert 'ticket_id=' in '...emit("agent_execution_detail", ..., agent_id=agent_id, data={...})'`.

- [ ] **Step 3: Add the attribution**

In `orchestrator/agent_runtime.py:416`, add the two keyword arguments to the emit. `workspace` is already in scope:

```python
                self._events.emit("agent_execution_detail", f"Agent {agent_id}: model={model}, tokens={result.input_tokens}/{result.output_tokens}, duration={result.duration_seconds:.1f}s", agent_id=agent_id, ticket_id=workspace.state.ticket_id, project_id=workspace.state.company_id, data={"model": model, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens, "tool_calls": result.tool_calls, "tool_rounds": result.tool_rounds, "duration": result.duration_seconds})
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/unit/test_agent_runtime.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/agent_runtime.py tests/unit/test_agent_runtime.py
git commit -m "feat(telemetry): attribute agent_execution_detail to its ticket

Spend per ticket was unattributable, which is the one thing missing
before the tiering question can be answered from data."
```

---

### Task 9: `pipeline_stats.py` — the report

**Files:**
- Create: `scripts/pipeline_stats.py`
- Test: `tests/unit/test_pipeline_stats.py`

**Interfaces:**
- Consumes: the `events` table written by `dashboard/event_store.py` — columns `timestamp, event_type, message, project_id, ticket_id, agent_id, data_json`.
- Produces: `collect_ticket_stats(db_path: str) -> list[TicketStats]`, where `TicketStats` is a dataclass with fields `ticket_id: str`, `project_id: str`, `stage_entries: dict[str, int]`, `bounces: dict[str, int]`, `input_tokens: int`, `output_tokens: int`, `duration_seconds: float`, `terminal_state: str`.

Read-only. No schema change, no new event type. Bounce edges are keyed `"FROM→TO"`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_pipeline_stats.py`:

```python
"""Tests for scripts/pipeline_stats.py — read-only reporting over the event store."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.pipeline_stats import collect_ticket_stats


def _seed(db_path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL,
            project_id TEXT,
            ticket_id TEXT,
            agent_id TEXT,
            data_json TEXT DEFAULT '{}'
        )
    """)
    conn.executemany(
        "INSERT INTO events (timestamp, event_type, message, project_id, ticket_id,"
        " agent_id, data_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_counts_stage_entries_and_bounces(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    def transition(ts, frm, to):
        return (ts, "stage_transition", "", "proj", "T-1", None,
                json.dumps({"from_state": frm, "to_state": to}))
    _seed(db, [
        transition(1.0, "NEW", "ANALYSIS"),
        transition(2.0, "ANALYSIS", "PLAN_REVIEW"),
        transition(3.0, "PLAN_REVIEW", "ANALYSIS"),
        transition(4.0, "ANALYSIS", "PLAN_REVIEW"),
        transition(5.0, "PLAN_REVIEW", "DEV"),
        transition(6.0, "DEV", "SCOPE_CHECK"),
        transition(7.0, "SCOPE_CHECK", "DEV"),
        transition(8.0, "DEV", "SCOPE_CHECK"),
        transition(9.0, "SCOPE_CHECK", "CODE_REVIEW"),
    ])

    stats = collect_ticket_stats(str(db))
    assert len(stats) == 1
    s = stats[0]
    assert s.ticket_id == "T-1"
    assert s.project_id == "proj"
    assert s.stage_entries["DEV"] == 2
    assert s.stage_entries["PLAN_REVIEW"] == 2
    assert s.bounces["PLAN_REVIEW→ANALYSIS"] == 1
    assert s.bounces["SCOPE_CHECK→DEV"] == 1
    assert s.terminal_state == "CODE_REVIEW"


def test_sums_tokens_and_duration(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    def run(ts, inp, out, dur):
        return (ts, "agent_execution_detail", "", "proj", "T-1", "dev-agent",
                json.dumps({"input_tokens": inp, "output_tokens": out, "duration": dur}))
    _seed(db, [run(1.0, 100, 20, 5.0), run(2.0, 300, 40, 7.5)])

    s = collect_ticket_stats(str(db))[0]
    assert s.input_tokens == 400
    assert s.output_tokens == 60
    assert s.duration_seconds == 12.5


def test_ignores_events_with_no_ticket(tmp_path: Path) -> None:
    """poll_cycle and daemon_started carry no ticket_id and must not create rows."""
    db = tmp_path / "events.db"
    _seed(db, [(1.0, "poll_cycle", "", None, None, None, "{}")])
    assert collect_ticket_stats(str(db)) == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/unit/test_pipeline_stats.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.pipeline_stats'`.

- [ ] **Step 3: Create the module**

`scripts/` is not a package yet — create an empty `scripts/__init__.py` so the test can `import scripts.pipeline_stats`:

```bash
touch scripts/__init__.py
```

Create `scripts/pipeline_stats.py`:

```python
"""Per-ticket pipeline statistics from the dashboard event store.

Read-only. Answers: how often does each ticket bounce, where, and what does
it cost? Existing data — `stage_transition` and `agent_execution_detail`
events are already persisted with no retention, so this reports on history
already on disk.

Usage:
    python -m scripts.pipeline_stats [path/to/events.db]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field

# Bounces are backward edges: a stage handing work back to an earlier one.
BOUNCE_EDGES = {
    ("PLAN_REVIEW", "ANALYSIS"),
    ("SCOPE_CHECK", "DEV"),
    ("CODE_REVIEW", "DEV"),
    ("QA", "DEV"),
    ("PR_REVIEW", "DEV"),
}


@dataclass
class TicketStats:
    ticket_id: str
    project_id: str = ""
    stage_entries: dict[str, int] = field(default_factory=dict)
    bounces: dict[str, int] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    duration_seconds: float = 0.0
    terminal_state: str = ""


def collect_ticket_stats(db_path: str) -> list[TicketStats]:
    """Aggregate every ticket's stage entries, bounces and spend."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT timestamp, event_type, project_id, ticket_id, data_json"
            " FROM events WHERE ticket_id IS NOT NULL ORDER BY timestamp ASC"
        ).fetchall()
    finally:
        conn.close()

    by_ticket: dict[str, TicketStats] = {}
    entries: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    bounces: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for _ts, event_type, project_id, ticket_id, data_json in rows:
        stats = by_ticket.setdefault(ticket_id, TicketStats(ticket_id=ticket_id))
        if project_id and not stats.project_id:
            stats.project_id = project_id
        try:
            data = json.loads(data_json or "{}")
        except json.JSONDecodeError:
            continue

        if event_type == "stage_transition":
            frm, to = data.get("from_state", ""), data.get("to_state", "")
            if not to:
                continue
            entries[ticket_id][to] += 1
            stats.terminal_state = to
            if (frm, to) in BOUNCE_EDGES:
                bounces[ticket_id][f"{frm}→{to}"] += 1
        elif event_type == "agent_execution_detail":
            stats.input_tokens += int(data.get("input_tokens") or 0)
            stats.output_tokens += int(data.get("output_tokens") or 0)
            stats.duration_seconds += float(data.get("duration") or 0.0)

    for ticket_id, stats in by_ticket.items():
        stats.stage_entries = dict(entries[ticket_id])
        stats.bounces = dict(bounces[ticket_id])
    return list(by_ticket.values())


def format_table(stats: list[TicketStats]) -> str:
    """One line per ticket, widest-first columns."""
    header = f"{'TICKET':<16} {'STATE':<16} {'DEV':>4} {'BOUNCES':>8} {'TOK IN':>9} {'TOK OUT':>8} {'MINS':>6}"
    lines = [header, "-" * len(header)]
    for s in sorted(stats, key=lambda x: x.ticket_id):
        lines.append(
            f"{s.ticket_id:<16} {s.terminal_state:<16} "
            f"{s.stage_entries.get('DEV', 0):>4} {sum(s.bounces.values()):>8} "
            f"{s.input_tokens:>9} {s.output_tokens:>8} {s.duration_seconds / 60:>6.1f}"
        )
    lines.append("")
    edge_totals: dict[str, int] = defaultdict(int)
    for s in stats:
        for edge, count in s.bounces.items():
            edge_totals[edge] += count
    lines.append("Bounces by edge:")
    for edge, count in sorted(edge_totals.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {edge:<28} {count}")
    return "\n".join(lines)


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/events.db"
    print(format_table(collect_ticket_stats(db_path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_pipeline_stats.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run it against the real database**

The live path is `data/events.db` (`config/schemas.py:131`, overridable per install via `dashboard.db_path`).

Run: `python -m scripts.pipeline_stats data/events.db`
Expected: a table of real tickets with real bounce counts. Ticket rows predating Task 8 will show zero tokens — that is expected, not a bug.

- [ ] **Step 6: Commit**

```bash
git add scripts/__init__.py scripts/pipeline_stats.py tests/unit/test_pipeline_stats.py
git commit -m "feat(telemetry): per-ticket bounce and spend report

Read-only over the existing event store. This is what the ticket-tiering
question gets decided on: if bounces and spend are flat across tickets,
tiers buy nothing."
```

---

## Final verification

- [ ] **Full suite:** `python -m pytest -q` — all green.
- [ ] **Lint:** `ruff check .` — no findings.
- [ ] **Dry run:** start the daemon in dry-run mode and confirm a ticket advances `analysis → plan_review → dev` in the logs without dispatching agents. Check `run.sh` / `main.py --help` for the exact flag.
- [ ] **Live run:** one real ticket end to end. Confirm `plan-review-agent-output.md` appears in the workspace's reports directory, its first line is a `Status:` verdict, and the dashboard shows the `PLAN REVIEW` badge.
- [ ] **Manual-mode check:** with mode set to manual, confirm the pipeline pauses after `plan_review` (not after `analysis`) and that approving advances it to `dev`.
- [ ] **Report:** `python -m scripts.pipeline_stats data/events.db` produces a table, and after a plan bounce it lists the `PLAN_REVIEW→ANALYSIS` edge.
