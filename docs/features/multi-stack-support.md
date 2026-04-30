# Feature: Multi-Stack Support (Pluggable Failure Recovery)

**Status:** Planned
**Created:** 2026-04-30
**Updated:** 2026-04-30
**Author:** Oleksandr Brazhenko

## Description

Decouple the orchestrator from Android/Gradle-specific failure recovery so the pipeline can cleanly serve non-Android repos (web, iOS, backend) without dead code, false-firing warnings, or misleading recovery buttons. Extract a `FailureRecoveryPlugin` interface, wrap existing Gradle remediation as the first plugin, and tag each repo with a `project_type` so plugins dispatch correctly.

See [`docs/multi-stack-readiness.md`](../multi-stack-readiness.md) for the full audit that motivated this feature.

## Motivation

Current state (audited 2026-04-30): Sickle is ~85% project-agnostic by design. The remaining 15% is concentrated in one module ([`orchestrator/gradle_remediation.py`](../../orchestrator/gradle_remediation.py)) wired into the orchestrator unconditionally:

- [`orchestrator/orchestrator.py:29-34`](../../orchestrator/orchestrator.py#L29-L34) — hard import of Gradle helpers.
- [`orchestrator/orchestrator.py:854-857`](../../orchestrator/orchestrator.py#L854-L857) — hardcoded `"Android SDK not installed"` / `"JDK not installed"` warning strings appended to QA-pass Telegram messages on substring matches.
- [`orchestrator/orchestrator.py:999-1016`](../../orchestrator/orchestrator.py#L999-L1016) — every failure inspected for AAPT2 arch mismatch and Gradle cache corruption; offers a `🧹 Clear cache & retry` Telegram button.
- [`orchestrator/pr_creation.py:41-47`](../../orchestrator/pr_creation.py#L41-L47) — push retry decision uses Gradle-only environmental-failure classifier.

For a web/Python/Node repo configured today, this code would either run as dead weight or produce false positives in user-facing Telegram messages.

## Requirements

- FR1: New module `orchestrator/failure_recovery.py` defines a `FailureRecoveryPlugin` Protocol (or ABC) with at minimum: `project_type: str`, `detect(error_output: str) -> RecoveryAction | None`, `pre_push_environmental(error_output: str) -> bool`.
- FR2: `RecoveryAction` is a small dataclass: `severity`, `user_message`, optional `button_label`, optional `button_action`.
- FR3: Existing Gradle code in [`orchestrator/gradle_remediation.py`](../../orchestrator/gradle_remediation.py) is wrapped as `GradleRecoveryPlugin(FailureRecoveryPlugin)` with `project_type = "android-kmp"`. **No logic changes** — the regex patterns, `clear_gradle_transforms()`, and arch-mismatch help text move behind the class boundary unchanged.
- FR4: `RepoInfo` in [`config/schemas.py:252-255`](../../config/schemas.py#L252-L255) gains a `project_type: str = "android-kmp"` field. Default preserves current behavior for existing repos. Allowed values documented (initially: `"android-kmp"`; `"web"`, `"ios"`, `"backend-python"`, `"backend-node"`, `"backend-go"` reserved for later).
- FR5: Orchestrator constructor ([`orchestrator/orchestrator.py:100-110`](../../orchestrator/orchestrator.py#L100-L110)) accepts `recovery_plugins: list[FailureRecoveryPlugin]` parameter, defaulting to `[GradleRecoveryPlugin()]`. Hard imports at lines 29-34 are replaced with iteration over `self._recovery_plugins`.
- FR6: At dispatch time, the orchestrator filters plugins by `repo_config.repo.project_type`, so a `web` repo never sees the Gradle classifier and vice versa.
- FR7: The hardcoded warning strings at [`orchestrator/orchestrator.py:854-857`](../../orchestrator/orchestrator.py#L854-L857) are replaced with a single generic message: `"Build toolchain not found locally — CI on GitHub will be the authoritative gate."` The `"sdk"` / `"java"` substring detection is removed (CI is already authoritative — see [line 872](../../orchestrator/orchestrator.py#L872)).
- FR8: [`orchestrator/pr_creation.py:41-47`](../../orchestrator/pr_creation.py#L41-L47) routes through the plugin's `pre_push_environmental()` method instead of importing `looks_like_pre_push_hook_environmental_failure` directly.
- FR9: Existing Android repos behave **identically** before and after — same Telegram messages, same Clear-cache button, same push-retry behavior. Validated by existing tests in [`tests/unit/test_orchestrator_notify_failed.py`](../../tests/unit/test_orchestrator_notify_failed.py) passing unchanged.

## Non-Requirements (explicitly out of scope)

- ❌ New stack-typed workflow YAMLs. The state machine stays generic.
- ❌ Per-stack agent prompts (`web-dev-agent.md`, etc.). Existing agents already read commands from repo config.
- ❌ A "build adapter" or "language plugin" abstraction layer. Variance lives only in failure recovery and host-OS — solve those, not a generic plugin framework.
- ❌ Onboarding any actual web/iOS/backend repo. This feature only unblocks them; first real consumer drives any further refactor.
- ❌ macOS runner support for iOS `xcodebuild`. Tracked separately if/when iOS becomes real.
- ❌ Stub plugins for stacks with no real consumer yet. Add them when needed.

## Technical Approach

1. Create `orchestrator/failure_recovery.py` with the Protocol and `RecoveryAction` dataclass.
2. Refactor [`gradle_remediation.py`](../../orchestrator/gradle_remediation.py) — keep all functions; add a thin `GradleRecoveryPlugin` class that delegates to them. No regex or remediation logic changes.
3. Modify [`config/schemas.py`](../../config/schemas.py) `RepoInfo` to add `project_type` with sensible default.
4. Modify [`orchestrator/orchestrator.py`](../../orchestrator/orchestrator.py): replace hard imports with plugin list, replace the two failure-inspection sites (lines 854-857, 999-1016) with iteration over filtered plugins, replace hardcoded warning strings with the generic message.
5. Modify [`orchestrator/pr_creation.py`](../../orchestrator/pr_creation.py) to call through the plugin interface.
6. Add a unit test that registering zero plugins still produces a working orchestrator (no AttributeErrors, no false warnings, no `🧹 Clear cache` button in Telegram messages).

Estimated scope: ~6 files touched, ~150 LOC moved/added. One focused day of work.

## Dependencies

- Existing Gradle remediation module ([`orchestrator/gradle_remediation.py`](../../orchestrator/gradle_remediation.py)) — wrapped, not rewritten.
- Existing config schema ([`config/schemas.py`](../../config/schemas.py)) — additive change to `RepoInfo`.
- Configuration Cascade — new field flows through the existing 3-level merge unchanged.

## Acceptance Criteria

- [ ] `FailureRecoveryPlugin` Protocol defined in `orchestrator/failure_recovery.py`.
- [ ] `GradleRecoveryPlugin` exists and wraps all current Gradle logic without behavior change.
- [ ] `RepoInfo.project_type` field added; defaults to `"android-kmp"`.
- [ ] Orchestrator core has zero direct imports from `gradle_remediation`; all references go through the plugin list.
- [ ] Hardcoded `"Android SDK"` / `"JDK"` strings removed from `orchestrator.py:854-857`; replaced with single generic message.
- [ ] All existing tests in `tests/unit/test_orchestrator_notify_failed.py` pass unchanged.
- [ ] New test: orchestrator constructed with `recovery_plugins=[]` produces no Android-specific user-facing strings on a simulated build failure.
- [ ] A repo configured with `project_type: "web"` (no plugin registered for it yet) does not see the Gradle Clear-cache button on failures.

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-30 | Initial draft — seeded from `docs/multi-stack-readiness.md` audit |
