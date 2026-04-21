#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Kill any existing instances
pkill -f "python main.py" 2>/dev/null && echo "Stopped old instance" && sleep 1 || true

source .env
exec .venv/bin/python main.py --config config-live
