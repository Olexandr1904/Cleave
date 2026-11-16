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

## Running on macOS (launchd)

On macOS the daemon runs as a launchd agent instead of the systemd unit. On the host the checkout is `~/Cleave`.

**Nothing under `~/Documents`, `~/Desktop`, `~/Downloads` or iCloud Drive (`~/Library/Mobile Documents`).** macOS privacy protection (TCC) blocks a launchd agent from those folders, so the checkout, `~/CleaveData` and every path in the config lie elsewhere. The renderer refuses a plist with such a path, also through a symlink or in a `PATH` entry.

**Required** in `config-live/global.yaml`. Paths use `${HOME}`: the config expands `${VAR}`, but nothing expands `~`.
```yaml
os: mac
workspaces:
  base_dir: "${HOME}/CleaveData/workspaces"
logging:
  dir: "${HOME}/Library/Logs/cleave"
```

**Install:** `deploy/com.cleave.daemon.plist` is a template; render it into `~/Library/LaunchAgents/`:
```bash
mkdir -p ~/Library/Logs/cleave ~/Library/LaunchAgents
cd ~/Cleave
.venv/bin/python scripts/render_launchd_plist.py \
  --out ~/Library/LaunchAgents/com.cleave.daemon.plist \
  --tmpdir "$(getconf DARWIN_USER_TEMP_DIR)"
plutil -lint ~/Library/LaunchAgents/com.cleave.daemon.plist
```
The defaults are your home, your user, this checkout and a `PATH` with Homebrew's Python 3.12 first; `--home`, `--user`, `--cleave-dir` and `--path` override them. The plist runs `/bin/bash ~/Cleave/run.sh` with `RunAtLoad` and `KeepAlive`, `ProcessType` `Interactive` (launchd throttles the CPU and I/O of a background job, which would slow reviews, fetches and builds) and `ExitTimeOut` 60 (a stop gets 60 s to shut down cleanly before launchd kills the daemon; its default is about 20 s), and logs to `~/Library/Logs/cleave/`.

**Tools on the plist's `PATH`.** `claude`, `gh`, `git` and Python 3.12 (`python3`) must resolve on the plist's `PATH`, not your shell's. This prints four paths and Python 3.12:
```bash
P="$(plutil -extract EnvironmentVariables.PATH raw -o - ~/Library/LaunchAgents/com.cleave.daemon.plist)"
env -i PATH="$P" /bin/sh -c 'for t in claude gh git python3; do command -v "$t" || echo "missing: $t"; done; python3 --version'
```
When one lives elsewhere, for example `claude` from its native installer in `~/.local/bin`, render again with that directory written out as an absolute path in `--path`: `--path "$HOME/.local/bin:$P"`.

**Your shell's variables are not there.** Under launchd the daemon gets only the plist's variables and `.env`, which `run.sh` exports whole. Put what your shell profile supplies and the builds need in `.env`: `JAVA_HOME` (Homebrew's openjdk is keg-only, so nothing finds it by itself), and `ANDROID_HOME` if `run.sh` does not find the SDK.

**Job clones.** Each running review gets a `git clone --local` of its repo's review clone under `~/CleaveData/reviewer/work/` (hard-linked objects: keep `work/` on the same volume as `repos/`), removed when the job ends and at start-up. Up to `reviewer.max_parallel_jobs` (default 6) run at once; none starts while the volume has less free space than `workspaces.min_free_disk_gb`.

**Review clones: make the first clone by hand.** The daemon keeps one clone per reviewed repo in `repos/<project_id>/<repo_id>` next to `reviewer.db` (by default `~/CleaveData/reviewer/`), and gives each git call there 300 s. That covers a fetch, but a large repo's first clone can take longer, and then every poll fails. So make the first clone (or a re-clone, after removing a broken one) by hand, with the daemon stopped, through the daemon's own code and a longer timeout:
```bash
cd ~/Cleave
(set -a; source .env; set +a; .venv/bin/python - <<'EOF'
import asyncio, os
from pathlib import Path
from orchestrator.reviewer.clone import ReviewClone, github_clone_url
clone = ReviewClone(Path.home() / "CleaveData/reviewer/repos/<project_id>/<repo_id>",
                    github_clone_url("<owner>", "<repo>"), "<base>", os.environ["<TOKEN_VAR>"], timeout=1800)
print(asyncio.run(clone.refresh()))
EOF
)
```
`<TOKEN_VAR>` is the `.env` variable the repo's GitHub token names. The token goes only into a request header, never into the clone's `.git/config` or the keychain. The clone's path is `<the directory of reviewer.db>/repos/<project_id>/<repo_id>`, the one shown by default; with `reviewer.db_path` set, put that file's directory in place of `CleaveData/reviewer`.

**Workspace trust.** `claude` asks once per directory whether to trust it, and a launchd run cannot answer. In a Terminal on the host's desktop (Screen Sharing is fine), run `cd <dir> && claude`, accept the workspace-trust prompt and type `/exit`. Do this in `~/Cleave`, in each review clone and in `~/CleaveData/reviewer/command` (create it first with `mkdir -m 700 -p`), where `claude` reads the replies the daemon does not recognise.

**Start, stop, restart:**
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cleave.daemon.plist   # start
launchctl bootout gui/$(id -u)/com.cleave.daemon                                  # stop
launchctl kickstart -k gui/$(id -u)/com.cleave.daemon                             # restart
```
If the daemon dies, `KeepAlive` starts it again within 60 s (`ThrottleInterval`), so a crash at start costs one start a minute, not a tight loop. `bootout` stops it until the next `bootstrap` or login. After a new render, `bootout` and `bootstrap` again: `kickstart` does not reread the plist. To uninstall, `bootout` and delete the plist from `~/Library/LaunchAgents/`.

**Stop or restart only while nothing is running:** no job (review, re-check, edit or draft) `running` and no post being posted. Then this read-only count from the reviewer database prints `0` (`sqlite3` ships with macOS; with `reviewer.db_path` set, use that path):
```bash
sqlite3 -readonly ~/CleaveData/reviewer/reviewer.db \
  "SELECT (SELECT count(*) FROM jobs WHERE state = 'running') + (SELECT count(*) FROM posts WHERE state = 'posting')"
```
A restart fails a running job with `interrupted (daemon restarted)` (a review or re-check is retried, but that run is lost) and marks a post being posted `interrupted`.

**Only one process anywhere may poll the bot token:** never `./run.sh` by hand while the agent is loaded, and never run Cleave with this token on another machine. A hand-run `run.sh` kills the agent's daemon, cutting a running review job short; launchd starts the agent again, and its `run.sh` kills the hand-started daemon in turn. While both run, Telegram answers `409 Conflict` and replies are lost.

**A new token in `.env` needs a restart** (`kickstart -k`): `run.sh` reads `.env` once at start, and the daemon keeps the old token until it restarts.

**GUI login.** The agent runs only while you are logged in to the host's GUI, and that login is also what unlocks the keychain `claude` needs; over ssh alone the keychain stays locked. A logout stops the agent; it starts again at the next GUI login, so with automatic login a reboot brings it back by itself.

**Logs**, in `~/Library/Logs/cleave/`:
- `cleave-daemon.log`: the daemon's log (`<logging.dir>/cleave-daemon.log`, the `Daemon logs:` path it prints at start), rotating at 10 MB × 5 backups.
- `cleave.log`: stdout, the startup lines. The plist sets `PYTHONUNBUFFERED=1`, so they appear as they happen.
- `cleave-error.log`: stderr: warnings, errors and crashes only (the plist sets `CLEAVE_STDERR_LEVEL=WARNING`; `cleave-daemon.log` has every line). launchd never rotates it; should it grow, empty it with the agent stopped (`: > ~/Library/Logs/cleave/cleave-error.log`).

---

## Dry-Run Testing (Development)

```bash
cd /home/admin0/tot
source .venv/bin/activate
source .env
python main.py --config config-live --dry-run
```
