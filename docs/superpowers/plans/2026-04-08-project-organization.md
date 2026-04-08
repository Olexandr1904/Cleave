# Project Organization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Set up dev/prod separation with two directories, semantic versioning via git tags, and a deploy script for managing releases.

**Architecture:** Development stays in `/home/admin0/tot/`, production runs from `/home/admin0/sickle-prod/` (a separate git clone pinned to a tag). A `scripts/deploy.sh` script handles first-time init and subsequent deploys. The systemd service is updated to point at the prod directory.

**Tech Stack:** Bash (deploy script), Python (version logging), systemd

---

### Task 1: Add version logging to main.py

**Files:**
- Modify: `main.py:53-58`
- Test: `tests/unit/test_main.py`

- [ ] **Step 1: Write failing test for version at startup**

Add a test to the `TestMain` class in `tests/unit/test_main.py` that verifies version is printed at startup:

```python
def test_version_printed_at_startup(self, capsys, monkeypatch):
    """main() prints the version string on startup."""
    self._set_all_env(monkeypatch)
    main(["--config", FIXTURES_DIR, "--project", "nonexistent"])
    captured = capsys.readouterr()
    assert "Sickle v" in captured.out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/admin0/tot && source .venv/bin/activate && python -m pytest tests/unit/test_main.py::TestMain::test_version_printed_at_startup -v`

Expected: FAIL — "Sickle v" not found in output (current startup line is "Sickle starting with config:")

- [ ] **Step 3: Implement version logging**

In `main.py`, add version reading at the top of `main()`:

```python
def get_version() -> str:
    """Read version from package metadata, falling back to pyproject.toml."""
    try:
        from importlib.metadata import version
        return version("sickle")
    except Exception:
        from pathlib import Path
        import re
        pyproject = Path(__file__).parent / "pyproject.toml"
        match = re.search(r'version\s*=\s*"([^"]+)"', pyproject.read_text())
        return match.group(1) if match else "unknown"
```

Then change the startup print in `main()` from:

```python
print(f"Sickle starting with config: {args.config}")
```

to:

```python
version = get_version()
print(f"Sickle v{version} starting with config: {args.config}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/admin0/tot && source .venv/bin/activate && python -m pytest tests/unit/test_main.py::TestMain::test_version_printed_at_startup -v`

Expected: PASS

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `cd /home/admin0/tot && source .venv/bin/activate && python -m pytest tests/unit/test_main.py -v`

Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add main.py tests/unit/test_main.py
git commit -m "Add version logging at startup"
```

---

### Task 2: Create deploy script

**Files:**
- Create: `scripts/deploy.sh`

- [ ] **Step 1: Write the deploy script**

Create `scripts/deploy.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Sickle deploy script
# Usage:
#   ./scripts/deploy.sh --init v0.1.0   # First-time setup
#   ./scripts/deploy.sh v0.2.0          # Deploy a tagged version

PROD_DIR="/home/admin0/sickle-prod"
REPO_URL="$(git -C "$(dirname "$0")/.." remote get-url origin)"
SERVICE_NAME="sickle"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}==>${NC} $*"; }
warn()  { echo -e "${YELLOW}WARNING:${NC} $*"; }
error() { echo -e "${RED}ERROR:${NC} $*" >&2; exit 1; }

usage() {
    echo "Usage:"
    echo "  $0 --init <tag>    First-time setup (clone, venv, systemd)"
    echo "  $0 <tag>           Deploy a tagged version"
    echo ""
    echo "Examples:"
    echo "  $0 --init v0.1.0"
    echo "  $0 v0.2.0"
    exit 1
}

# --- Validation helpers ---

validate_tag() {
    local tag="$1"
    if ! git tag --list "$tag" | grep -q "^${tag}$"; then
        error "Tag '$tag' does not exist. Create it first: git tag $tag"
    fi
}

check_prod_clean() {
    if [ -d "$PROD_DIR" ]; then
        if [ -n "$(git -C "$PROD_DIR" status --porcelain 2>/dev/null)" ]; then
            error "Prod directory has uncommitted changes. Resolve them first."
        fi
    fi
}

get_current_version() {
    if [ -d "$PROD_DIR" ] && [ -d "$PROD_DIR/.git" ]; then
        git -C "$PROD_DIR" describe --tags --exact-match 2>/dev/null || \
        git -C "$PROD_DIR" rev-parse --short HEAD 2>/dev/null || \
        echo "(unknown)"
    else
        echo "(not installed)"
    fi
}

# --- Init mode ---

do_init() {
    local tag="$1"

    if [ -d "$PROD_DIR" ]; then
        error "Prod directory $PROD_DIR already exists. Remove it first or use deploy mode."
    fi

    validate_tag "$tag"

    info "Initializing production at $PROD_DIR (tag: $tag)"

    # 1. Clone
    info "Cloning repository..."
    git clone "$REPO_URL" "$PROD_DIR"
    cd "$PROD_DIR"
    git checkout "$tag"

    # 2. Virtualenv + deps
    info "Creating virtual environment..."
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip --quiet
    .venv/bin/pip install -e . --quiet

    # 3. Environment file
    if [ ! -f "$PROD_DIR/.env" ]; then
        cp environment.template .env
        chmod 600 .env
        warn "Fill in API keys in $PROD_DIR/.env"
    fi

    # 4. Systemd service
    info "Installing systemd service..."
    sudo cp deploy/sickle.service /etc/systemd/system/sickle.service
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"

    # 5. Log directory
    sudo mkdir -p /var/log/sickle
    sudo chown "$USER:$USER" /var/log/sickle

    echo ""
    info "Init complete! Next steps:"
    echo "  1. Edit $PROD_DIR/.env with your API keys"
    echo "  2. Start: sudo systemctl start $SERVICE_NAME"
    echo "  3. Logs:  journalctl -u $SERVICE_NAME -f"
}

# --- Deploy mode ---

do_deploy() {
    local tag="$1"

    if [ ! -d "$PROD_DIR/.git" ]; then
        error "Prod directory not initialized. Run: $0 --init $tag"
    fi

    validate_tag "$tag"
    check_prod_clean

    local prev_version
    prev_version="$(get_current_version)"

    info "Deploying $tag (current: $prev_version)"

    # 1. Stop service
    info "Stopping $SERVICE_NAME..."
    sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true

    # 2. Fetch and checkout
    info "Fetching and checking out $tag..."
    cd "$PROD_DIR"
    git fetch origin
    git checkout "$tag"

    # 3. Update dependencies
    info "Updating dependencies..."
    .venv/bin/pip install -e . --quiet

    # 4. Update systemd unit (in case it changed)
    sudo cp deploy/sickle.service /etc/systemd/system/sickle.service
    sudo systemctl daemon-reload

    # 5. Start service
    info "Starting $SERVICE_NAME..."
    sudo systemctl start "$SERVICE_NAME"

    echo ""
    info "Deploy complete: $prev_version → $tag"
    echo "  Status: sudo systemctl status $SERVICE_NAME"
    echo "  Logs:   journalctl -u $SERVICE_NAME -f"
}

# --- Main ---

if [ $# -lt 1 ]; then
    usage
fi

if [ "$1" = "--init" ]; then
    [ $# -lt 2 ] && usage
    do_init "$2"
elif [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    usage
else
    do_deploy "$1"
fi
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x /home/admin0/tot/scripts/deploy.sh`

- [ ] **Step 3: Verify script parses without errors**

Run: `bash -n /home/admin0/tot/scripts/deploy.sh`

Expected: No output (syntax OK)

- [ ] **Step 4: Verify help output**

Run: `cd /home/admin0/tot && ./scripts/deploy.sh --help`

Expected: Usage text showing `--init` and deploy modes

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy.sh
git commit -m "Add deploy script for prod init and tag-based deploys"
```

---

### Task 3: Update systemd service for new paths

**Files:**
- Modify: `deploy/sickle.service`

The current service file uses `/opt/sickle` and `User=pipeline`. Per the spec, production is at `/home/admin0/sickle-prod` and runs as the current user.

- [ ] **Step 1: Update sickle.service**

Replace the full content of `deploy/sickle.service` with:

```ini
[Unit]
Description=Sickle — Autonomous AI Development Pipeline
After=network.target

[Service]
Type=simple
User=admin0
Group=admin0
WorkingDirectory=/home/admin0/sickle-prod
ExecStart=/home/admin0/sickle-prod/.venv/bin/python3 main.py --config config-live
EnvironmentFile=/home/admin0/sickle-prod/.env
Restart=always
RestartSec=10
StandardOutput=append:/var/log/sickle/sickle.log
StandardError=append:/var/log/sickle/sickle-error.log
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Validate service file syntax**

Run: `systemd-analyze verify /home/admin0/tot/deploy/sickle.service 2>&1 || true`

Expected: No critical errors (warnings about unit not being loaded are OK)

- [ ] **Step 3: Commit**

```bash
git add deploy/sickle.service
git commit -m "Update systemd service paths for prod directory layout"
```

---

### Task 4: Update deploy documentation

**Files:**
- Modify: `deploy/README.md`

- [ ] **Step 1: Rewrite deploy/README.md**

Replace the full content of `deploy/README.md` with:

```markdown
# Sickle Deployment

## VPS Requirements

- Ubuntu 22.04+ (or any systemd-based Linux)
- Python 3.10+
- Git
- 2+ GB RAM
- 20+ GB disk (for workspaces)
- Outbound HTTPS access (Jira, GitHub, Anthropic, Telegram APIs)

## Directory Layout

```
/home/admin0/tot/              Development (git repo, edit here)
/home/admin0/sickle-prod/      Production (separate clone, pinned to tag)
/var/log/sickle/               Daemon logs
```

## First-Time Setup

From your dev directory:

```bash
cd /home/admin0/tot

# Tag a release
git tag v0.1.0
git push origin master --tags

# Initialize production
./scripts/deploy.sh --init v0.1.0

# Fill in API keys
nano /home/admin0/sickle-prod/.env

# Start the service
sudo systemctl start sickle
```

## Deploying a New Version

```bash
cd /home/admin0/tot

# Bump version in pyproject.toml, commit, tag
git tag v0.2.0
git push origin master --tags

# Deploy
./scripts/deploy.sh v0.2.0
```

## Rollback

```bash
./scripts/deploy.sh v0.1.0
```

## Service Management

```bash
sudo systemctl start sickle
sudo systemctl stop sickle
sudo systemctl restart sickle
sudo systemctl status sickle
journalctl -u sickle -f
```

## Log Files

- Service log: `/var/log/sickle/sickle.log`
- Error log: `/var/log/sickle/sickle-error.log`
- Agent logs: per-workspace in `{workspace}/logs/`

## Dry-Run Testing (Development)

```bash
cd /home/admin0/tot
source .venv/bin/activate
source .env
python main.py --config config-live --dry-run
```
```

- [ ] **Step 2: Commit**

```bash
git add deploy/README.md
git commit -m "Update deployment docs for two-directory layout"
```

---

### Task 5: Remove legacy setup.sh

**Files:**
- Delete: `deploy/setup.sh`

The `deploy.sh --init` replaces `setup.sh`. The old script used `/opt/sickle`, a `pipeline` user, and `cp -r` instead of git — none of which match the new design.

- [ ] **Step 1: Remove setup.sh**

Run: `git rm /home/admin0/tot/deploy/setup.sh`

- [ ] **Step 2: Commit**

```bash
git commit -m "Remove legacy setup.sh, replaced by scripts/deploy.sh --init"
```

---

### Task 6: Tag initial release

- [ ] **Step 1: Verify current version in pyproject.toml**

Run: `grep 'version' /home/admin0/tot/pyproject.toml`

Expected: `version = "0.1.0"`

- [ ] **Step 2: Tag the release**

```bash
git tag v0.1.0
```

- [ ] **Step 3: Verify tag**

Run: `git tag --list 'v*'`

Expected: `v0.1.0`

- [ ] **Step 4: Push tag to remote**

```bash
git push origin master --tags
```
