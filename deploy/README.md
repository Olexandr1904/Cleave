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
