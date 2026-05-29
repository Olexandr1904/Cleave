#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Kill any existing instances. Match on "main.py --config" rather than
# "python main.py" — on macOS the Homebrew interpreter shows as "Python"
# (capital P), so a lowercase "python" pattern silently matches nothing and
# stale daemons pile up, with the oldest holding port 8080.
pkill -f "main.py --config" 2>/dev/null && echo "Stopped old instance" && sleep 1 || true

set -a; source .env; set +a

# Android build toolchain — exported so QA-agent subprocesses (claude -p)
# inherit it and gradlew can locate the SDK.
# macOS installs the SDK under ~/Library/Android/sdk; Linux under ~/Android/Sdk.
if [ "$(uname)" = "Darwin" ]; then
  export ANDROID_HOME="$HOME/Library/Android/sdk"
else
  export ANDROID_HOME="$HOME/Android/Sdk"
fi
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export PATH="$ANDROID_HOME/platform-tools:$ANDROID_HOME/cmdline-tools/latest/bin:$PATH"

exec .venv/bin/python main.py --config config-live
