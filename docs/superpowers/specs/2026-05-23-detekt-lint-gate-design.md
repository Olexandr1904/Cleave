# Deterministic detekt lint gate (dev → qa)

**Date:** 2026-05-23
**Status:** Design — awaiting approval

## Problem

detekt is *supposed* to run in the pipeline today, but only because the dev-agent
([dev-agent.md:107](../../../agents/dev-agent.md)) and qa-agent
([qa-agent.md:98](../../../agents/qa-agent.md)) are told to run it in their prompts. That is
not deterministic — an agent can skip it — and inspection of real acme-mobile QA runs shows
the existing approach is fragile:

1. **It uses `./gradlew detektDebug` (type resolution).** On the ARM64 host this repeatedly
   hits `OutOfMemoryError` (host ~7.7 GB RAM vs ~9 GB requested heap) and the x86-64 aapt2
   architecture wall. The check often fails *environmentally*, not on real findings.
2. **It scans whole modules**, so pre-existing violations on `develop` (e.g.
   `TasksUnitsFragment.kt:333`) make the task exit non-zero even when the ticket's own code is
   clean.
3. Because of #2 the QA agent performs expensive "scope analysis" to decide a non-zero exit is
   actually *"PASS for ticket scope"*. A naive deterministic gate that just checked the exit
   code would bounce clean tickets back to dev forever.

Because of the host limits, acme-mobile already pushes with `--no-verify`
(`skip_pre_push_hook: true`), so the repo's own pre-push detekt hook does not run in the
pipeline either.

## Goal

Add a deterministic lint stage between dev and qa that runs detekt, bounces to dev on real
findings, and escalates (does **not** bounce) on environmental failures.

## Non-goals

- Auto-detecting which linter a project uses — tracked as a separate follow-up (enhance
  `project-setup-agent` to detect detekt/ktlint/spotless/eslint/etc. and emit the host-safe
  command). See "Follow-up".
- Removing detekt from the dev/qa agent prompts. The qa-agent keeps running lint as cheap
  insurance for projects with no lint command configured. May be slimmed later.
- Changing any acme-mobile **source** (no repo build.gradle / hook edits). Pipeline-side only.

## Approach (chosen: "B — whole-repo plain detekt + baseline")

Run the project's **plain** `./gradlew detekt` task (not `detektDebug`). Plain detekt skips
type resolution, which:

- avoids the aapt2 wall and the compile-time OOM, and
- relies on `config/detekt-baseline.xml` to suppress pre-existing issues.

### Verification already performed (real acme-mobile sandbox, ACME-6941 clone)

- `./gradlew detekt` on a clean checkout → **BUILD SUCCESSFUL, exit 0** in ~33s cold. No
  aapt2/OOM. Baseline suppressed all pre-existing issues.
- Same with a new violating file present → **BUILD FAILED, exit 1**, names the file/issue.

This confirms the gate passes clean code and catches new violations on the actual host.

### Residual risk

If the baseline drifts (a new un-baselined violation lands on `develop`), plain detekt could
fail on code the ticket did not touch, re-creating the bounce loop. Mitigated by the
`max_iterations: 2 → escalate` cap (a human is notified instead of looping forever). Accepted
for now; the more robust changed-files-only variant (Approach A) was considered and deferred.

## Design

### Flow

```
analysis → dev → scope_check → lint → qa → push → pr_review → done
                                  │
                          on findings → dev (fix loop, cap 2 → escalate)
```

### Components

1. **`workflows/default-workflow.yaml`**
   - Change `scope_check.on_pass` from `"qa"` to `"lint"`.
   - Add stage:
     ```yaml
     - id: "lint"
       action: "run_lint"
       description: "Run linter (detekt); bounce to dev on findings"
       on_pass: "qa"
       on_fail: "dev"
       max_iterations: 2
       on_max_iterations: "escalate"
     ```
   - Shared workflow → the `lint` stage exists for all projects, but is a clean pass-through
     where `linting.run_command` is unset (see action below).

2. **New action `orchestrator/pipeline/actions/run_lint.py`**
   Returns `ActionResult(success, next_state, error, metadata)`:
   - `linting.run_command` unset → `success=True, next_state=Stage.QA` (clean pass-through).
   - Runs the command in the workspace via subprocess (mirrors `push_and_open_pr`'s git calls).
   - exit 0 → `success=True, next_state=Stage.QA`.
   - exit non-zero AND output is **not** an environmental pattern → `success=True,
     next_state=Stage.DEV`; write detekt output to the dev-readable runtime output file so the
     dev-agent sees the findings on bounce.
   - exit non-zero AND output matches `gradle_remediation.looks_like_*` (aapt2 / cache
     corruption / OOM) → attempt `gradle_remediation` recovery + one retry; if still failing,
     return `success=False` so the action layer escalates. **Never** route an environmental
     failure to dev.
   - `on_pass`/`on_fail` targets are read from `stage_def` (passed into the action) and mapped
     to `Stage` via `driver._STAGE_TO_STATE`, rather than hard-coding QA/DEV.

3. **State machine wiring**
   - `workspace/workspace.py`: add `Stage.LINT = "LINT"`. Update `_TRANSITIONS`:
     - `Stage.SCOPE_CHECK` → add `Stage.LINT`.
     - add `Stage.LINT: {Stage.QA, Stage.DEV, Stage.BLOCKED, Stage.FAILED, Stage.DEFERRED, Stage.MANUAL_CONTROL, Stage.PAUSED}`.
     - add `Stage.LINT` to the recovery target sets (BLOCKED, FAILED, AWAITING_APPROVAL,
       DEFERRED, PAUSED).
   - `orchestrator/pipeline/driver.py`: add `"lint": Stage.LINT` to `_STAGE_TO_STATE`.
   - `orchestrator/pipeline/action_stage.py`: add `run_lint` to the action dispatch and pass
     `stage_def`; thread the new action callable through `orchestrator.py` like the existing
     `action_*` callables.
   - `orchestrator/constants.py`: add a `lint` runtime-output file mapping
     (e.g. `lint-output.md`) so bounced findings are surfaced consistently.

4. **Config (not source)** — `config-live/projects/acme/repos/acme-mobile.yaml`:
   ```yaml
   linting:
     run_command: "./gradlew detekt"
     hard_gate: true
   ```
   Plain `detekt`, deliberately — not `detektDebug`.

### Error handling

The one critical guard: environmental failures (OOM / aapt2 / gradle cache corruption) must
escalate, never bounce to dev. Detection reuses the existing
`orchestrator/gradle_remediation.py` helpers. Everything else is exit-code routing.

## Testing

- Unit-test `run_lint`'s output → `next_state` mapping:
  - clean (exit 0) → QA
  - real findings (exit 1, normal output) → DEV
  - environmental output (aapt2/OOM patterns) → escalate (success=False), not DEV
  - `linting.run_command` unset → QA pass-through
- detekt behavior on real acme-mobile already verified manually (see above); no integration test
  against the live repo.

## Follow-up (separate spec)

Enhance `project-setup-agent` to **auto-detect** the linter during project creation
(detekt → `./gradlew detekt`, ktlint → `ktlintCheck`, spotless → `spotlessCheck`,
eslint → `npm run lint`, swiftlint → `swiftlint`) and propose the **host-safe** command for
user confirmation, instead of only asking. This populates `linting.run_command` automatically
for future projects; acme-mobile is configured manually here because it already exists.
