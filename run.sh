#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Kill any existing instances. Match on "main.py --config" rather than
# "python main.py" — on macOS the Homebrew interpreter shows as "Python"
# (capital P), so a lowercase "python" pattern silently matches nothing and
# stale daemons pile up, with the oldest holding port 8080.
#
# Then WAIT for the old daemon to fully exit and release port 8080 before
# starting the new one. A bare `pkill; sleep 1` races the old daemon's
# graceful shutdown (Telegram polling + uvicorn take a couple seconds to
# release the socket), so the new daemon hits "address already in use" and
# comes up with no dashboard.
if pkill -f "main.py --config" 2>/dev/null; then
  echo "Stopping old instance..."
  for _ in $(seq 1 30); do
    pgrep -f "main.py --config" >/dev/null 2>&1 || break
    sleep 0.5
  done
  pkill -9 -f "main.py --config" 2>/dev/null || true   # hard-kill any straggler
  for _ in $(seq 1 20); do
    lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 0.5
  done
  echo "Old instance stopped"
fi

set -a; source .env; set +a

# Android build toolchain — exported so subprocesses that DON'T source the
# shell profile can still locate the SDK: the QA-agent's `claude -p`, and the
# git pre-push hook that runs detekt. A wrong ANDROID_HOME here makes
# `git push` fail with "SDK location not found", which blocks the PR.
#
# Respect an ANDROID_HOME pinned in .env (sourced above) for hosts where the
# SDK lives somewhere non-standard; otherwise probe the conventional macOS
# (Android Studio + the Homebrew android-commandlinetools cask) and Ubuntu
# locations and pick the first that exists. This keeps the same launcher
# working on both macOS and Ubuntu without hardcoding one host's layout.
if [ -z "${ANDROID_HOME:-}" ] || [ ! -d "${ANDROID_HOME}" ]; then
  for candidate in \
    "$HOME/Library/Android/sdk" \
    "/opt/homebrew/share/android-commandlinetools" \
    "/usr/local/share/android-commandlinetools" \
    "$HOME/Android/Sdk" \
    "$HOME/Android/sdk" \
    "/usr/lib/android-sdk" \
    "/opt/android-sdk"; do
    if [ -d "$candidate" ]; then
      export ANDROID_HOME="$candidate"
      break
    fi
  done
fi
if [ -z "${ANDROID_HOME:-}" ] || [ ! -d "${ANDROID_HOME}" ]; then
  echo "WARNING: Android SDK not found — gradlew and the detekt pre-push hook will fail." >&2
  echo "         Set ANDROID_HOME in .env to your SDK path." >&2
else
  echo "Android SDK: $ANDROID_HOME"
fi
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export PATH="$ANDROID_HOME/platform-tools:$ANDROID_HOME/cmdline-tools/latest/bin:$PATH"

exec .venv/bin/python main.py --config config-live
