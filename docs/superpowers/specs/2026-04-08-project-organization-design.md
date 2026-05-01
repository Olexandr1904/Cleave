# Project Organization: Dev/Prod Separation & Versioning

**Date:** 2026-04-08
**Status:** Approved

## Problem

Cleave is developed and will run in production on the same VPS. All code, prompts, configs, workflows, and runtime data currently live in a single directory. Editing any part of the system while the pipeline processes tickets risks breaking a running instance. There is no versioning or release process.

## Decisions

- **Approach:** Two separate directories — one for development, one for production
- **Versioning:** Semantic versioning via git tags; production pins to a tagged release
- **Test strategy:** `--dry-run` flag for now; `config-test/` can be added later
- **Deployment:** Manual via a deploy script; simple and scriptable
- **Secrets:** Separate `.env` per directory, not in git

## Filesystem Layout

```
/home/admin0/tot/                ← DEVELOPMENT (git repo, edit here)
  main.py
  pyproject.toml                 ← version = "X.Y.Z"
  .env                           ← dev secrets (not in git)
  .venv/                         ← dev virtualenv
  config-live/                   ← config (versioned with code)
  agents/                        ← agent prompts
  workflows/                     ← workflow definitions
  orchestrator/                  ← core logic
  integrations/                  ← adapters
  scripts/
    deploy.sh                    ← deploy script (NEW)
    pre-commit                   ← existing hook
    install-hooks.sh             ← existing hook installer
  deploy/
    cleave.service               ← systemd unit
    setup.sh                     ← VPS setup
    environment.template         ← .env reference
  ...

/home/admin0/cleave-prod/        ← PRODUCTION (separate git clone, pinned to tag)
  .env                           ← prod secrets (not in git)
  .venv/                         ← prod virtualenv
  config-live/                   ← config at the tagged version
  ...                            ← everything else from the tag
```

## Versioning & Release Workflow

### Version Source

`pyproject.toml` holds the version string. Already present as `version = "0.1.0"`.

### Release Process

1. Develop and commit in `/home/admin0/tot/`
2. When ready to release:
   - Bump `version` in `pyproject.toml`
   - Commit: `git commit -m "Release v0.2.0"`
   - Tag: `git tag v0.2.0`
   - Push: `git push origin master --tags`
3. Deploy: `./scripts/deploy.sh v0.2.0`

### Rollback

Deploy any previous tag: `./scripts/deploy.sh v0.1.0`

### Version Tracking at Runtime

`main.py` logs the current version on startup (read from `pyproject.toml` or `importlib.metadata`).

## Deploy Script (`scripts/deploy.sh`)

### Usage

```bash
# Deploy a specific tag
./scripts/deploy.sh v0.2.0

# First-time setup (initializes prod directory)
./scripts/deploy.sh --init v0.1.0
```

### First-Time Init (`--init`)

1. Clone the repo to `/home/admin0/cleave-prod/`
2. Checkout the specified tag
3. Create `.venv`, install dependencies
4. Copy `environment.template` to `.env`
5. Print reminder to fill in `.env` values
6. Install systemd service (`deploy/cleave.service`)
7. Print instructions to start

### Subsequent Deploys

1. `systemctl stop cleave`
2. `cd /home/admin0/cleave-prod`
3. `git fetch origin`
4. `git checkout <tag>`
5. `source .venv/bin/activate && pip install -e .`
6. `systemctl start cleave`

### Safety

- Refuses to deploy if there are uncommitted changes in the prod directory
- Validates that the tag exists before proceeding
- Prints the previous and new version for confirmation

## Test Environment Strategy

**Current:** Use `--dry-run` flag to skip real API calls during development.

```bash
# Dev testing
cd /home/admin0/tot
source .venv/bin/activate
source .env
python main.py --config config-live --dry-run
```

**Future (when needed):** Add `config-test/` with sandbox Jira project and test GitHub repo. Run dev against it with `python main.py --config config-test`.

## Production Runtime

### Systemd Service

The existing `deploy/cleave.service` manages the prod instance:
- Auto-restarts on crash
- Logs to journald (`journalctl -u cleave -f`)
- Starts on boot
- Working directory: `/home/admin0/cleave-prod`

### Secrets Management

Each directory maintains its own `.env`:

| File | Purpose |
|------|---------|
| `/home/admin0/tot/.env` | Dev secrets (can match prod or use test values) |
| `/home/admin0/cleave-prod/.env` | Prod secrets (real tokens) |
| `environment.template` | Reference template (in git) |

`.env` is in `.gitignore` — never committed.

### Monitoring

- Systemd ensures the process stays alive
- Telegram notifications (already built in) alert on errors
- `journalctl -u cleave` for log inspection

## What This Design Does NOT Cover (Future Work)

- **CI/CD pipeline** (GitHub Actions for tests on push)
- **Automated version bumping** (e.g., `bump2version`)
- **Separate test environment config** (`config-test/`)
- **Backup strategy** for runtime data (workspace reports, state files)
- **Multi-instance production** (running multiple Cleave daemons)
