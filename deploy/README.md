# Sickle Deployment

## VPS Requirements

- Ubuntu 22.04+ (or any systemd-based Linux)
- Python 3.10+
- Git (for cloning repos)
- 2+ GB RAM
- 20+ GB disk (for workspaces)
- Outbound HTTPS access (Jira, GitHub, Anthropic, Telegram APIs)

## Setup

1. **Clone the repo** to the VPS:
   ```bash
   git clone <repo-url> /tmp/sickle && cd /tmp/sickle
   ```

2. **Run the setup script** as root:
   ```bash
   sudo bash deploy/setup.sh
   ```

3. **Configure environment variables**:
   ```bash
   sudo nano /etc/sickle/environment
   ```
   Fill in all API keys (Anthropic, Jira, GitHub, Telegram).

4. **Copy config files**:
   ```bash
   sudo mkdir -p /etc/sickle/config/projects
   sudo cp your-global.yaml /etc/sickle/config/global.yaml
   sudo cp -r your-projects/ /etc/sickle/config/projects/
   ```

5. **Start the service**:
   ```bash
   sudo systemctl start sickle
   ```

## First-Run Validation

```bash
# Check service status
systemctl status sickle

# Follow logs
journalctl -u sickle -f

# Dry-run test (manual)
cd /opt/sickle
sudo -u pipeline .venv/bin/python3 main.py --config /etc/sickle/config --dry-run
```

## Log Files

- Service log: `/var/log/sickle/sickle.log`
- Error log: `/var/log/sickle/sickle-error.log`
- Agent logs: per-workspace in `{workspace}/logs/`

## Service Management

```bash
# Start / stop / restart
sudo systemctl start sickle
sudo systemctl stop sickle
sudo systemctl restart sickle

# View status
sudo systemctl status sickle

# Disable auto-start
sudo systemctl disable sickle
```

## File Layout

```
/opt/sickle/            # Application code
/etc/sickle/
  ├── environment       # API keys (chmod 600)
  └── config/
      ├── global.yaml
      └── projects/
/var/log/sickle/        # Daemon logs
/workspaces/            # Ticket workspaces (auto-managed)
```
