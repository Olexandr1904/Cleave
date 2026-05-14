#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Kill any existing instances
pkill -f "python main.py" 2>/dev/null && echo "Stopped old instance" && sleep 1 || true

source .env

# Android build toolchain — exported so QA-agent subprocesses (claude -p)
# inherit it and gradlew can locate the SDK.
export ANDROID_HOME="$HOME/Android/Sdk"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export PATH="$ANDROID_HOME/platform-tools:$ANDROID_HOME/cmdline-tools/latest/bin:$PATH"

exec .venv/bin/python main.py --config config-live
