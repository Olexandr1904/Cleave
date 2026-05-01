# Cleave Deployment

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
/home/admin0/cleave-prod/      Production (separate clone, pinned to tag)
/var/log/cleave/               Daemon logs
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
nano /home/admin0/cleave-prod/.env

# Start the service
sudo systemctl start cleave
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
sudo systemctl start cleave
sudo systemctl stop cleave
sudo systemctl restart cleave
sudo systemctl status cleave
journalctl -u cleave -f
```

## Log Files

- Daemon log: `/var/log/cleave/cleave-daemon.log` (rotating, 10 MB × 5 backups). Path is `<logging.dir>/cleave-daemon.log`; the daemon falls back to `./data/` if the configured dir isn't writable.
- systemd journal: `journalctl -u cleave -f` for stdout/stderr captured by the unit
- Agent logs: per-workspace in `{workspace}/logs/`
- Agent reports: per-workspace in `{workspace}/reports/`

## Dry-Run Testing (Development)

```bash
cd /home/admin0/tot
source .venv/bin/activate
source .env
python main.py --config config-live --dry-run
```
