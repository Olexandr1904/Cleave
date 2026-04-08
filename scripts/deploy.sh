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
