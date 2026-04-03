#!/usr/bin/env bash
set -euo pipefail

# Sickle deployment setup script for Ubuntu VPS.
# Run as root or with sudo.

APP_DIR="/opt/sickle"
CONFIG_DIR="/etc/sickle"
LOG_DIR="/var/log/sickle"
WORKSPACE_DIR="/workspaces"
SERVICE_USER="pipeline"

echo "=== Sickle Pipeline Setup ==="

# 1. Create service user
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Creating service user: $SERVICE_USER"
    useradd --system --shell /usr/sbin/nologin --home-dir "$APP_DIR" "$SERVICE_USER"
fi

# 2. Create directories
echo "Creating directories..."
mkdir -p "$APP_DIR" "$CONFIG_DIR" "$LOG_DIR" "$WORKSPACE_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$LOG_DIR" "$WORKSPACE_DIR"

# 3. Copy application code
echo "Copying application to $APP_DIR..."
cp -r . "$APP_DIR/"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

# 4. Set up Python virtual environment
echo "Setting up Python environment..."
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .

# 5. Copy config template
if [ ! -f "$CONFIG_DIR/environment" ]; then
    echo "Copying environment template..."
    cp deploy/environment.template "$CONFIG_DIR/environment"
    chmod 600 "$CONFIG_DIR/environment"
    chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR/environment"
    echo "  !! Edit $CONFIG_DIR/environment with your API keys !!"
fi

# 6. Install systemd service
echo "Installing systemd service..."
cp deploy/sickle.service /etc/systemd/system/sickle.service
systemctl daemon-reload
systemctl enable sickle

# 7. Validate config
echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Copy your config files to $CONFIG_DIR/config/"
echo "     (global.yaml, projects/ directory)"
echo "  2. Edit $CONFIG_DIR/environment with API keys"
echo "  3. Start the service: systemctl start sickle"
echo "  4. Check status: systemctl status sickle"
echo "  5. View logs: journalctl -u sickle -f"
