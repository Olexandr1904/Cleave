# Rename Sickle/Tot → Cleave Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the project from "Sickle" (brand) / "tot" (repo) to "Cleave" across all source files, docs, config, deploy artifacts, and GitHub.

**Architecture:** Pure find/replace across 17 change categories. No logic changes. Order matters: package name first (runtime critical), then deploy artifacts, then config defaults, then docs. Existing test suite validates nothing broke.

**Spec:** `docs/superpowers/specs/2026-05-01-project-rename-to-cleave-design.md`

---

### Task 1: Python Package Core

**Files:**
- Modify: `pyproject.toml`
- Modify: `main.py`

- [ ] **Step 1: Update pyproject.toml package name**

In `pyproject.toml` line 6, change:
```toml
name = "sickle"
```
to:
```toml
name = "cleave"
```

- [ ] **Step 2: Update main.py — all Sickle references**

Apply these changes in `main.py`:

| Line | Old | New |
|---|---|---|
| 5 | `"""Sickle — Autonomous AI Development Pipeline.` | `"""Cleave — Autonomous AI Development Pipeline.` |
| 19 | `return version("sickle")` | `return version("cleave")` |
| 35 | `prog="sickle",` | `prog="cleave",` |
| 36 | `description="Sickle — Autonomous AI Development Pipeline",` | `description="Cleave — Autonomous AI Development Pipeline",` |
| 76 | `print(f"Sickle v{version} starting` | `print(f"Cleave v{version} starting` |
| 157 | `"sickle-daemon.log"` | `"cleave-daemon.log"` |
| 211 | `event_bus.emit("daemon_started", f"Sickle v{version}` | `event_bus.emit("daemon_started", f"Cleave v{version}` |

Run:
```bash
grep -n "sickle\|Sickle" main.py
```
Expected: no output.

- [ ] **Step 3: Reinstall package under new name**

```bash
pip install -e ".[dev]"
```
Expected: `Successfully installed cleave-0.1.0`

- [ ] **Step 4: Delete stale egg-info**

```bash
rm -rf sickle.egg-info
ls *.egg-info 2>/dev/null || echo "clean"
```
Expected: `cleave.egg-info/` exists (created by pip), `sickle.egg-info/` gone.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml main.py
git rm -r --cached sickle.egg-info 2>/dev/null || true
git add cleave.egg-info/ 2>/dev/null || true
git commit -m "chore: rename package sickle → cleave"
```

---

### Task 2: Deploy Artifacts

**Files:**
- Rename: `deploy/sickle.service` → `deploy/cleave.service`
- Modify: `deploy/cleave.service`
- Modify: `scripts/deploy.sh`

- [ ] **Step 1: Rename and rewrite service file**

```bash
git mv deploy/sickle.service deploy/cleave.service
```

Replace the full contents of `deploy/cleave.service` with:
```ini
[Unit]
Description=Cleave — Autonomous AI Development Pipeline
After=network.target

[Service]
Type=simple
User=admin0
Group=admin0
WorkingDirectory=/home/admin0/cleave-prod
ExecStart=/home/admin0/cleave-prod/.venv/bin/python3 main.py --config config-live
EnvironmentFile=/home/admin0/cleave-prod/.env
Restart=always
RestartSec=10
StandardOutput=append:/var/log/cleave/cleave.log
StandardError=append:/var/log/cleave/cleave-error.log
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Update deploy.sh**

In `scripts/deploy.sh`, apply these changes:

| Line | Old | New |
|---|---|---|
| 4 | `# Sickle deploy script` | `# Cleave deploy script` |
| 9 | `PROD_DIR="/home/admin0/sickle-prod"` | `PROD_DIR="/home/admin0/cleave-prod"` |
| 11 | `SERVICE_NAME="sickle"` | `SERVICE_NAME="cleave"` |
| 94 | `sudo cp deploy/sickle.service /etc/systemd/system/sickle.service` | `sudo cp deploy/cleave.service /etc/systemd/system/cleave.service` |
| 99 | `sudo mkdir -p /var/log/sickle` | `sudo mkdir -p /var/log/cleave` |
| 100 | `sudo chown "$USER:$USER" /var/log/sickle` | `sudo chown "$USER:$USER" /var/log/cleave` |
| 105 | `echo "/data/sickle")` | `echo "/data/cleave")` |
| 151 | `sudo cp deploy/sickle.service /etc/systemd/system/sickle.service` | `sudo cp deploy/cleave.service /etc/systemd/system/cleave.service` |

Verify:
```bash
grep -n "sickle\|Sickle" scripts/deploy.sh
```
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add deploy/cleave.service scripts/deploy.sh
git commit -m "chore: rename deploy artifacts sickle → cleave"
```

---

### Task 3: Config Defaults and Examples

**Files:**
- Modify: `config/schemas.py`
- Modify: `config-live.example/global.yaml`
- Modify: `config-live.example/projects/example-project/repos/example-repo.yaml`
- Modify: `tests/fixtures/config/global.yaml`

- [ ] **Step 1: Update config/schemas.py**

In `config/schemas.py`, apply:

| Line | Old | New |
|---|---|---|
| 1 | `"""Configuration schema dataclasses for Sickle (v2)."""` | `"""Configuration schema dataclasses for Cleave (v2)."""` |
| 75 | `dir: str = "/var/log/sickle"` | `dir: str = "/var/log/cleave"` |
| 186 | `# redundant with Sickle's own QA gate.` | `# redundant with Cleave's own QA gate.` |
| 211 | `commit_author_name: str = "Sickle Bot"` | `commit_author_name: str = "Cleave Bot"` |
| 212 | `commit_author_email: str = "sickle@pipeline.local"` | `commit_author_email: str = "cleave@pipeline.local"` |

- [ ] **Step 2: Update config-live.example/global.yaml**

```bash
sed -i 's|/var/log/sickle|/var/log/cleave|g; s|/data/sickle|/data/cleave|g; s|Sickle|Cleave|g; s|sickle|cleave|g' config-live.example/global.yaml
```

Verify:
```bash
grep -i "sickle" config-live.example/global.yaml
```
Expected: no output.

- [ ] **Step 3: Update config-live.example repo yaml**

```bash
sed -i 's|Sickle|Cleave|g; s|sickle|cleave|g' config-live.example/projects/example-project/repos/example-repo.yaml
```

Verify:
```bash
grep -i "sickle" config-live.example/projects/example-project/repos/example-repo.yaml
```
Expected: no output.

- [ ] **Step 4: Update test fixture**

In `tests/fixtures/config/global.yaml` line 26, change:
```yaml
  dir: "/var/log/sickle"
```
to:
```yaml
  dir: "/var/log/cleave"
```

- [ ] **Step 5: Commit**

```bash
git add config/schemas.py config-live.example/ tests/fixtures/config/global.yaml
git commit -m "chore: update config defaults and examples sickle → cleave"
```

---

### Task 4: Agent Files and Claude Commands

**Files:**
- Modify: `agents/project-setup-agent.md`
- Modify: `.claude/commands/add-project.md`
- Modify: `.claude/commands/list-projects.md`
- Modify: `.claude/commands/remove-project.md`

- [ ] **Step 1: Update project-setup-agent.md**

```bash
sed -i 's|Sickle|Cleave|g; s|sickle|cleave|g' agents/project-setup-agent.md
```

Verify:
```bash
grep -i "sickle" agents/project-setup-agent.md
```
Expected: no output.

- [ ] **Step 2: Update Claude commands**

```bash
sed -i 's|Sickle|Cleave|g; s|sickle|cleave|g' .claude/commands/add-project.md .claude/commands/list-projects.md .claude/commands/remove-project.md
```

Verify:
```bash
grep -i "sickle" .claude/commands/*.md
```
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add agents/project-setup-agent.md .claude/commands/
git commit -m "chore: update agent identity and claude commands sickle → cleave"
```

---

### Task 5: Python Source Files

**Files:**
- Modify: `orchestrator/pr_creation.py`
- Modify: `orchestrator/ticket_prioritizer.py`
- Modify: `orchestrator/tool_sandbox.py`
- Modify: `orchestrator/constants.py`
- Modify: `integrations/telegram/intent_parser.py`
- Modify: `integrations/telegram/handlers/status.py`
- Modify: `integrations/config/config_tools.py`
- Modify: `config/config_loader.py`
- Modify: `config/resource_registry.py`
- Modify: `dashboard/web.py`
- Modify: `dashboard/event_store.py`
- Modify: `dashboard/events.py`
- Modify: `workspace/workspace.py`

- [ ] **Step 1: Bulk replace across all Python source files**

```bash
find orchestrator integrations config dashboard workspace -name "*.py" | xargs sed -i 's|Sickle pipeline|Cleave pipeline|g; s|Sickle Pipeline|Cleave Pipeline|g; s|Sickle Bot|Cleave Bot|g; s|Sickle Status|Cleave Status|g; s|Sickle,|Cleave,|g; s|Sickle —|Cleave —|g; s|for Sickle\.|for Cleave.|g; s|for Sickle$|for Cleave|g; s|of Sickle |of Cleave |g; s|sickle@pipeline\.local|cleave@pipeline.local|g; s|Sickle|Cleave|g; s|sickle|cleave|g'
```

- [ ] **Step 2: Verify no sickle references remain in Python source**

```bash
grep -rn "sickle\|Sickle" orchestrator/ integrations/ config/ dashboard/ workspace/ --include="*.py"
```
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/ integrations/ config/ dashboard/ workspace/
git commit -m "chore: update Python source docstrings and strings sickle → cleave"
```

---

### Task 6: Root Documentation

**Files:**
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update README.md**

```bash
sed -i 's|sickle@pipeline\.local|cleave@pipeline.local|g; s|/var/log/sickle|/var/log/cleave|g; s|/data/sickle|/data/cleave|g; s|sickle-prod|cleave-prod|g; s|sickle\.service|cleave.service|g; s|cd sickle|cd cleave|g; s|Sickle|Cleave|g; s|sickle|cleave|g' README.md
```

Verify:
```bash
grep -i "sickle" README.md
```
Expected: no output.

- [ ] **Step 2: Update CONTRIBUTING.md and CLAUDE.md**

```bash
sed -i 's|Sickle|Cleave|g; s|sickle|cleave|g' CONTRIBUTING.md CLAUDE.md
```

Verify:
```bash
grep -i "sickle" CONTRIBUTING.md CLAUDE.md
```
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add README.md CONTRIBUTING.md CLAUDE.md
git commit -m "chore: rename project in root docs sickle → cleave"
```

---

### Task 7: Documentation Directory (Bulk)

**Files:**
- Modify: all `docs/**/*.md` and `agents/README.md` and `deploy/README.md`

- [ ] **Step 1: Bulk replace across all docs**

```bash
find docs agents deploy -name "*.md" | xargs sed -i 's|sickle@pipeline\.local|cleave@pipeline.local|g; s|/var/log/sickle|/var/log/cleave|g; s|/data/sickle|/data/cleave|g; s|sickle-prod|cleave-prod|g; s|sickle\.service|cleave.service|g; s|cd sickle|cd cleave|g; s|Sickle|Cleave|g; s|sickle|cleave|g'
```

- [ ] **Step 2: Verify no sickle references remain in docs**

```bash
grep -rl "sickle\|Sickle" docs/ agents/ deploy/ --include="*.md"
```
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add docs/ agents/ deploy/
git commit -m "chore: rename project in all docs sickle → cleave"
```

---

### Task 8: Test Files

**Files:**
- Modify: `tests/unit/test_main.py`
- Modify: `tests/unit/test_config_loader.py`
- Modify: `tests/unit/test_orchestrator_squash.py`
- Modify: `tests/unit/test_ensure_branch_has_commits.py`
- Modify: `tests/unit/test_gradle_remediation.py`
- Modify: `tests/unit/test_workspace_manager.py`
- Modify: `tests/e2e/test_deferred_recovery.py`
- Modify: `tests/e2e/conftest.py`

- [ ] **Step 1: Bulk replace across all test files**

```bash
find tests -name "*.py" | xargs sed -i 's|sickle@pipeline\.local|cleave@pipeline.local|g; s|/var/log/sickle|/var/log/cleave|g; s|/data/sickle|/data/cleave|g; s|"sickle"|"cleave"|g; s|Sickle Bot|Cleave Bot|g; s|Sickle v|Cleave v|g; s|"Sickle"|"Cleave"|g; s|Sickle|Cleave|g; s|sickle|cleave|g'
```

- [ ] **Step 2: Verify no sickle references remain in tests**

```bash
grep -rn "sickle\|Sickle" tests/
```
Expected: no output.

- [ ] **Step 3: Run test suite**

```bash
pytest tests/ -x -q 2>&1 | tail -20
```
Expected: all tests pass (or same failures as before this rename — zero new failures introduced).

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "chore: update test fixtures and assertions sickle → cleave"
```

---

### Task 9: IDE Settings

**Files:**
- Modify: `.claude/settings.local.json`

- [ ] **Step 1: Replace sickle paths and service name**

```bash
sed -i 's|/data/sickle/|/data/cleave/|g; s|journalctl --user -u sickle|journalctl --user -u cleave|g; s|/home/admin0/tot/|/home/admin0/tot/|g' .claude/settings.local.json
```

Note: the `/home/admin0/tot/` paths in settings.local.json refer to the local working directory — leave those as-is unless the directory itself is renamed.

Verify:
```bash
grep -i "sickle" .claude/settings.local.json
```
Expected: no output (or only irrelevant matches if any historical entries remain).

- [ ] **Step 2: Commit**

```bash
git add .claude/settings.local.json
git commit -m "chore: update IDE settings paths sickle → cleave"
```

---

### Task 10: GitHub Repository Rename and Git Remote

This task requires manual browser action.

- [ ] **Step 1: Rename GitHub repo**

1. Open `https://github.com/Olexandr1904/tot`
2. Go to **Settings** → scroll to **Danger Zone** → **Rename repository**
3. Enter `cleave` → confirm

- [ ] **Step 2: Update git remote URL**

```bash
git remote set-url origin https://github.com/Olexandr1904/cleave.git
git remote -v
```
Expected:
```
origin  https://github.com/Olexandr1904/cleave.git (fetch)
origin  https://github.com/Olexandr1904/cleave.git (push)
```

- [ ] **Step 3: Update clone instruction in README.md**

In `README.md`, find the clone instruction and ensure it reads:
```bash
git clone https://github.com/Olexandr1904/cleave.git && cd cleave
```
(The `cd cleave` was already fixed in Task 6; just verify the URL if it was hardcoded.)

```bash
grep "github.com" README.md | head -5
```

If the clone URL is still `tot`, fix it:
```bash
sed -i 's|Olexandr1904/tot|Olexandr1904/cleave|g' README.md
git add README.md
```

- [ ] **Step 4: Final commit**

```bash
git add -p  # stage any remaining changes
git commit -m "chore: update GitHub remote URL tot → cleave"
```

---

### Task 11: Final Verification

- [ ] **Step 1: Global scan for any remaining sickle/tot references**

```bash
grep -rl "sickle\|Sickle\|SICKLE" . --include="*.py" --include="*.md" --include="*.yaml" --include="*.yml" --include="*.toml" --include="*.sh" --include="*.json" --include="*.service" 2>/dev/null | grep -v __pycache__ | grep -v ".pytest_cache" | grep -v ".git/"
```
Expected: no output (or only this plan file itself, which is expected).

- [ ] **Step 2: Check "tot" as repo reference specifically**

```bash
grep -rn "\"tot\"\|/tot\b\|tot\.git\|cd tot" . --include="*.md" --include="*.yaml" --include="*.sh" --include="*.json" 2>/dev/null | grep -v ".git/" | grep -v "admin0/tot"
```
Expected: no output.

- [ ] **Step 3: Run full test suite one final time**

```bash
pytest tests/ -q 2>&1 | tail -10
```
Expected: all tests pass.

- [ ] **Step 4: Verify CLI entry point**

```bash
python main.py --version
```
Expected: output contains `Cleave v0.1.0` (not `Sickle`).

- [ ] **Step 5: Push to GitHub**

```bash
git push origin master
```
