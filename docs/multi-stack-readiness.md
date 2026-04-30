# Multi-Stack Readiness — Can Sickle Onboard Web / iOS / Backend Projects?

**Audit date:** 2026-04-30
**Question:** How easy is it to add a web (React/TS), iOS (Swift), or backend (Python/Node/Go) project to the pipeline? What needs to decouple first?

## TL;DR

**Sickle is ~85% project-agnostic by design and ~15% Android/Gradle-coupled by accretion.** The hard parts (state machine, agent dispatch, VCS/tracker/notifier) are well-abstracted. The leak is concentrated in **one module** ([`orchestrator/gradle_remediation.py`](orchestrator/gradle_remediation.py)) and **two call sites** in [`orchestrator/orchestrator.py`](orchestrator/orchestrator.py) and [`orchestrator/pr_creation.py`](orchestrator/pr_creation.py).

**You could onboard a web/iOS/backend project today** by setting `linting.run_command`, `testing.run_command`, `build.check_command` in repo YAML — agents read those and run them blindly. The Android-specific code would just sit dead. The right thing to do before scaling: extract one `FailureRecoveryPlugin` interface so per-stack remediation hooks plug in cleanly. **Estimated work: 1 focused day.**

---

## What's already generic (no work needed)

### State machine and workflow
[`workflows/default-workflow.yaml`](workflows/default-workflow.yaml) defines stages purely by ID and agent reference (`analysis → dev → scope_check → qa → push → pr_review → done`). No stack assumptions. Adding a new project type doesn't touch this file.

[`orchestrator/workflow_router.py`](orchestrator/workflow_router.py) and [`orchestrator/stage_verifier.py`](orchestrator/stage_verifier.py) drive transitions on outcome strings (`pass`/`fail`) and file-existence checks (e.g., `qa-agent-output.md` exists), not on build artifacts. Generic.

### VCS / Tracker / Notifier integrations
[`integrations/base/`](integrations/base/) defines three ABCs — [`VCSInterface`](integrations/base/vcs.py), [`TrackerInterface`](integrations/base/tracker.py), [`NotifierInterface`](integrations/base/notifier.py). The orchestrator depends only on the bases ([`orchestrator/orchestrator.py:21-23`](orchestrator/orchestrator.py#L21-L23)) and accepts concrete impls via DI. Web/iOS/backend projects use the same GitHub + Jira + Telegram plumbing with zero changes.

### Config schema
[`config/schemas.py:193-208`](config/schemas.py#L193-L208) exposes `LintConfig`, `TestConfig`, `BuildConfig` as **per-repo run commands**:
```python
@dataclass
class TestConfig:
    run_command: str = ""
    hard_gate: bool = True
```
This is the load-bearing abstraction. `./gradlew test`, `npm test`, `swift test`, `pytest`, `go test` — all valid values. No code changes needed.

### Agent prompts
All agents in [`agents/`](agents/) are language-agnostic. They consume `linting.run_command` etc. from repo config and shell out. Concretely verified — only **one** Android string lives in any agent prompt:

- [`agents/project-setup-agent.md:139`](agents/project-setup-agent.md#L139): `(optional, comma-separated, e.g. \`.github/, build.gradle.kts\`)` — illustrative example, not enforcement.

### Tasks directory
[`tasks/implement-code.md`](tasks/implement-code.md) is the only file. Generic.

---

## What's coupled (needs decoupling before scaling)

### 1. Gradle remediation module — Android-only, ~235 lines

[`orchestrator/gradle_remediation.py`](orchestrator/gradle_remediation.py) is **100% Android/Gradle-specific**. It detects:
- AAPT2 daemon-startup failures ([line 51](orchestrator/gradle_remediation.py#L51))
- AAPT2 binary syntax errors ([line 52](orchestrator/gradle_remediation.py#L52))
- `AarResourcesCompilerTransform` failures with `transforms/` cache paths ([lines 55-66](orchestrator/gradle_remediation.py#L55-L66))
- aarch64 vs x86-64 aapt2 architecture mismatch ([line 76-79](orchestrator/gradle_remediation.py#L76-L79))

It also acts: `clear_gradle_transforms()` wipes `~/.gradle/caches/*/transforms/` and kills running Gradle daemons. None of this generalizes to npm/yarn/cargo/swift/pip.

The module **itself** is fine — it's a quality piece of stack-specific code. The problem is **how it's wired in**.

### 2. Hard import of Gradle remediation in core orchestrator

[`orchestrator/orchestrator.py:29-34`](orchestrator/orchestrator.py#L29-L34):
```python
from orchestrator.gradle_remediation import (
    ARCH_MISMATCH_HELP,
    clear_gradle_transforms,
    looks_like_aapt2_arch_mismatch,
    looks_like_gradle_cache_corruption,
)
```
Unconditional, top-of-file. The orchestrator core **knows about Gradle**. For a web-only or iOS-only deployment this is dead weight; for a mixed-fleet daemon serving all four stack types from one process, it's a smell.

### 3. Hardcoded Android/JDK warning strings

[`orchestrator/orchestrator.py:854-857`](orchestrator/orchestrator.py#L854-L857) (in the QA-passed-with-warnings path):
```python
if "sdk" in output_lower and "not found" in output_lower:
    warnings.append("Android SDK not installed — build not verified")
if "java" in output_lower and ("not found" in output_lower or "command not found" in output_lower):
    warnings.append("JDK not installed — tests not run")
```
For a web project, `"sdk not found"` substring matching could false-fire on unrelated text and mention "Android SDK" in a Telegram message about a React PR. This is the most user-visible leak.

### 4. Failure-classification call sites

[`orchestrator/orchestrator.py:999-1016`](orchestrator/orchestrator.py#L999-L1016) inspects every failure for `looks_like_aapt2_arch_mismatch()` and `looks_like_gradle_cache_corruption()` and offers a "🧹 Clear cache & retry" Telegram button. Wrong stack = noise.

[`orchestrator/pr_creation.py:41-47`](orchestrator/pr_creation.py#L41-L47) imports `looks_like_pre_push_hook_environmental_failure` to decide whether to retry pushes with `--no-verify`. The classifier currently only knows about Gradle/AAPT2 toolchain failures. Web/iOS pre-push hooks (husky, swiftlint pre-commit) would never trip it.

### 5. Tests reflect Android-only history

[`tests/unit/test_orchestrator_notify_failed.py`](tests/unit/test_orchestrator_notify_failed.py) tests Gradle cache paths and aarch64 hints. Not blocking — adding a web fixture wouldn't break anything, but the test surface for new stacks is empty.

### 6. Schema has no `project_type` field

[`config/schemas.py:231-256`](config/schemas.py#L231-L256) — `ProjectInfo` and `RepoInfo` have `id`, `name`, `enabled`, nothing else. The stack is **inferred implicitly** from the lint/test/build commands. That works (it's how the Android repos work today), but a new operator can't tell what a repo is without reading its commands. Once you have multiple stacks under one daemon, you'll want to dispatch failure-recovery plugins by `project_type`.

---

## Onboarding cost per stack — concretely

Assuming **no** decoupling work first (just use the existing levers):

| Stack | Required config changes | Will the pipeline run? | Will failure UX be clean? |
|---|---|---|---|
| **Web (React/TS)** | `linting.run_command: "npm run lint"`, `testing.run_command: "npm test"`, `build.check_command: "npm run build"` | Yes | No — Gradle button + "Android SDK not installed" warnings will misfire on certain errors |
| **iOS (Swift)** | `linting.run_command: "swiftlint"`, `testing.run_command: "xcodebuild test ..."`, `build.check_command: "xcodebuild build ..."` | Yes, but `xcodebuild` requires macOS host — Sickle daemon is Linux-only today | Same warning leakage |
| **Backend (Python)** | `linting.run_command: "ruff check ."`, `testing.run_command: "pytest"`, `build.check_command: "python -m build"` | Yes | Same warning leakage |
| **Backend (Node)** | `linting.run_command: "eslint ."`, `testing.run_command: "npm test"`, `build.check_command: "tsc --noEmit"` | Yes | Same |
| **Backend (Go)** | `linting.run_command: "golangci-lint run"`, `testing.run_command: "go test ./..."`, `build.check_command: "go build ./..."` | Yes | Same |

**iOS has an extra blocker:** `xcodebuild` needs macOS. The Sickle daemon currently assumes Linux ([`CLAUDE.md`](CLAUDE.md) lists `gradlew`, no Xcode tooling). A separate macOS runner or a build-on-CI-only mode is needed. Punt: have iOS QA delegate compilation entirely to GitHub Actions runners and treat local lint-only as the gate.

---

## Recommended decoupling — minimal, surgical

This is the smallest change that closes the leak without over-engineering:

### Step 1 — Define a recovery-plugin interface
New file: `orchestrator/failure_recovery.py`.
```python
class FailureRecoveryPlugin(Protocol):
    project_type: str  # "android-kmp", "web-react", etc.
    def detect(self, error_output: str) -> RecoveryAction | None: ...
    # RecoveryAction := (severity, user_message, button_label?, button_action?)
```

### Step 2 — Wrap existing Gradle code as a plugin
Move the four `looks_like_*` functions and `clear_gradle_transforms` behind a `GradleRecoveryPlugin(FailureRecoveryPlugin)` class in [`gradle_remediation.py`](orchestrator/gradle_remediation.py). No logic changes — just a class wrapper.

### Step 3 — Register plugins on orchestrator init
[`orchestrator/orchestrator.py:100-110`](orchestrator/orchestrator.py#L100-L110) (constructor) gains a `recovery_plugins: list[FailureRecoveryPlugin] = [GradleRecoveryPlugin()]` parameter. Replace the hard imports at lines 29-34 with iteration over `self._recovery_plugins`.

### Step 4 — Add `project_type` to RepoConfig
Add `project_type: str = "android-kmp"` to [`RepoInfo`](config/schemas.py#L252-L255) (default preserves existing behavior). At dispatch time, filter plugins by `project_type` so a web repo never sees the Gradle classifier.

### Step 5 — Replace hardcoded Android/JDK warning strings
Replace [`orchestrator/orchestrator.py:854-857`](orchestrator/orchestrator.py#L854-L857) with generic messaging like `"Build toolchain not found — CI on GitHub will be the authoritative gate."` Lose the false-positive risk; lose nothing real (CI is already authoritative — see [line 872](orchestrator/orchestrator.py#L872)).

**Total surface: ~6 files touched, ~150 LOC moved/added, no behavioral change for existing Android repos. Gradle-specific tests keep working unchanged.**

---

## What NOT to do

- **Don't** rewrite the agents per stack. They already read commands from config and shell out. Adding `web-dev-agent.md`, `ios-dev-agent.md` would duplicate prompts that should stay shared.
- **Don't** introduce a stack-typed workflow. The state machine is generic on purpose.
- **Don't** abstract VCS/tracker/notifier further — they're already at the right level.
- **Don't** add per-stack config schemas. The current `LintConfig`/`TestConfig`/`BuildConfig` shape covers everything.
- **Don't** invent a "build adapter" or "language plugin" layer. There's no behavior that varies by language *except* failure recovery and host-OS requirements. Solve those two; ignore the rest.

---

## Bottom line

Architecture is sound. The state machine, agent dispatch, integration ABCs, and config schema were designed to be stack-agnostic, and they are. The single concrete leak — Gradle remediation hardwired into the orchestrator — is small, isolated, and refactorable in a day without touching agents, workflows, or integrations. **Onboard the next non-Android repo first, then refactor when the second one shows up** — not before. The `project_type` field and `FailureRecoveryPlugin` interface are the right abstractions, but premature without a real second consumer.
