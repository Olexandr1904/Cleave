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

## Dashboard Access

The dashboard answers only the host itself and Tailscale (`127.0.0.0/8`, `::1/128`, `100.64.0.0/10`, `fd7a:115c:a1e0::/48`); any other peer gets 403. Override with `dashboard.allowed_networks` (a list of CIDR networks) in `global.yaml`. A request must also name the dashboard by an IP address, `localhost`, a `*.ts.net` name or the machine's hostname; to reach it by another DNS name, add that name to `dashboard.allowed_hosts`.

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

## Skill flow: host prerequisites

The [skill flow](../docs/skill-flow.md) runs a repo's own skill in one `claude` session per ticket, in a long-lived checkout of the repo. Do these steps once per repo, in a Terminal on the host's desktop (Screen Sharing is fine), before you set `ticket_flow: both` on the repo and restart. None of them prints a token or asks you to type one.

The checkout's path is `<the directory of reviewer.db>/skill/repos/<project_id>/<repo_id>`; with the default database that is:
```bash
C=~/CleaveData/reviewer/skill/repos/<project_id>/<repo_id>
```
With `reviewer.db_path` set, put that file's directory in place of `~/CleaveData/reviewer`. The steps below use `$C`.

**1. Make the checkout by hand, as a full clone.** The daemon never clones it. A shallow clone is refused by name (`the skill checkout is a shallow clone`), so give no `--depth`. This clones through the daemon's own git environment: the token is read from `.env` inside the script and goes only into a request header, never into the clone's `.git/config` or the keychain.
```bash
mkdir -p "$(dirname "$C")"
cd ~/Cleave
(set -a; source .env; set +a; C="$C" .venv/bin/python - <<'EOF'
import os, subprocess
from orchestrator.reviewer.clone import git_env, github_clone_url
subprocess.run(["git", "clone", github_clone_url("<owner>", "<repo>"), os.environ["C"]],
               env=git_env(os.environ["<TOKEN_VAR>"]), check=True)
EOF
)
git -C "$C" rev-parse --is-shallow-repository
```
`<TOKEN_VAR>` is the `.env` variable the repo's GitHub token names. A good result: git's `Cloning into …` and no error, then `false`.

Then place the files `skill_flow.required_files` names (a local build properties file, say) in the checkout, and check that each is there:
```bash
ls -l "$C/local.properties"
```
Leave `submodule.recurse` unset in the checkout: Cleave fetches no submodules, and with `submodule.recurse=true` in the checkout's own config the move to the base branch can fail. A good result for this prints nothing (or `false`):
```bash
git -C "$C" config --local --get submodule.recurse
```
Last, trust the directory, as for the review clones: `cd "$C" && claude`, accept the workspace-trust prompt and type `/exit`.

**2. The skills' plugins are installed in the host's `claude` profile, and a skill can be called by name in `-p` mode.** Install the plugins that carry the repo's skills for the host user (`claude`, then `/plugin`), then ask a `-p` session in the checkout which skills it can call:
```bash
cd "$C"
claude -p --strict-mcp-config --allowedTools Skill \
  "Without running any skill, list the names of the skills you can call with the Skill tool, one per line."
```
A good result lists the names `skill_flow.bug_skill` and `skill_flow.task_skill` hold, written the same way (with the plugin's prefix, `plugin:skill`, when the config names them so). This list is a stand-in for calling a skill, not a call: calling one would start its work on a ticket. The first run is the first real call.

**3. The `build_env` variables are visible to the daemon under launchd.** The daemon gets only the plist's variables and `.env`, which `run.sh` exports whole; `run.sh` also sets `ANDROID_HOME` and `ANDROID_SDK_ROOT` when it finds the SDK. With the daemon running under launchd, print just those variables from its environment (put the names `build_env` lists in place of the three after `HOME`):
```bash
PID="$(pgrep -f 'main.py --config' | head -1)"
ps -wwE -o command= -p "$PID" | tr ' ' '\n' | grep -E '^(HOME|JAVA_HOME|ANDROID_HOME|ANDROID_SDK_ROOT)='
```
A good result: one `NAME=value` line for `HOME` (it shows the environment could be read) and one for each name. A missing one goes into `.env`, followed by a restart (`launchctl kickstart -k gui/$(id -u)/com.cleave.daemon`). Otherwise every run stops with `build variable <name> is not set in the daemon's environment`.

**4. A session resumed with `--resume` keeps its context, and the CLI words an unknown session as Cleave expects.** The skill flow continues one session over many turns (your replies, the commit, the gate fixes):
```bash
cd "$C"
ID="$(uuidgen | tr '[:upper:]' '[:lower:]')"
claude -p --session-id "$ID" "Remember the word copper. Answer with one word: noted."
claude -p --resume "$ID" "Which word did I ask you to remember? Answer with that word only."
```
A good result: the second answer is `copper`. Then resume a session id the CLI never created, with the output options the daemon uses:
```bash
claude -p --resume "$(uuidgen | tr '[:upper:]' '[:lower:]')" --output-format stream-json --verbose "hello" 2>&1 \
  | grep -i "no conversation found"
```
A good result: a line that holds `No conversation found`. Cleave starts a fresh session after a cut first turn only on those words; if this prints nothing, the CLI words it differently, and a run whose first turn was cut stops with `session failed` instead (Drop ends it).

**5. The deny rules apply.** Start `-p` probe sessions in an environment scrubbed like the session's, with a settings file made by the daemon's own code, as the session's is. First make the file and the environment:
```bash
cd ~/Cleave
S="$(mktemp -d)/settings.json"
.venv/bin/python -c 'import json; from pathlib import Path; from orchestrator.skillflow.session import skill_settings_json; print(json.dumps(skill_settings_json([Path(".env").absolute(), Path("config-live").absolute()]), indent=2))' > "$S"
cd "$C"
SCRUBBED=(env -i HOME="$HOME" PATH="$PATH" USER="$USER" LANG="$LANG" TMPDIR="$TMPDIR"
          GIT_TERMINAL_PROMPT=0 GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null)
```
The control, without Cleave's settings file: the same harmless command runs.
```bash
"${SCRUBBED[@]}" claude -p --strict-mcp-config --allowedTools Bash \
  "Run 'gh --version' with the Bash tool. Answer 'ran' or 'refused', then its first output line."
```
A good result: `ran` and a `gh version …` line. Then with Cleave's settings file:
```bash
"${SCRUBBED[@]}" claude -p --settings "$S" --strict-mcp-config --allowedTools Bash \
  "Run 'gh --version' with the Bash tool, then 'git ls-remote origin', one call each. For each, answer 'ran' or 'refused'."
```
A good result: both `refused`, and no `gh` version and no list of refs in the answer. Bash is allowed in both probes, as it is in the session, and the control ran the same `gh --version`: so the refusal comes from the deny rules, not from the model declining.

**6. The host user keeps no GitHub login: not for `gh`, not for git.** The session runs as the host user with its `HOME`, so it can reach a login stored for that user, and a push made with it would skip every gate Cleave runs. The reviewer's own `gh` calls get the token in their environment, one call at a time, and Cleave's own git calls likewise.

First `gh`:
```bash
env -u GH_TOKEN -u GITHUB_TOKEN gh auth status
```
A good result: `You are not logged into any GitHub hosts.`, with no keychain prompt. A listed host, or a keychain prompt, means a login is stored (`gh auth status` shows a stored token masked, never whole): deny the prompt, remove the login with `gh auth logout`, and run the check again until it prints the not-logged-in line with no prompt.

Then git's own credentials for github.com: no entry in a git credentials file, and no github.com item in the login keychain (where git's `osxkeychain` helper keeps one). These print only whether an entry exists, never its secret:
```bash
for f in ~/.git-credentials ~/.config/git/credentials; do
  [ -e "$f" ] && echo "$f: $(grep -c 'github\.com' "$f") github.com line(s)"
done
security find-internet-password -s github.com ~/Library/Keychains/login.keychain-db >/dev/null 2>&1 \
  && echo "a github.com item is in the login keychain" || echo "no github.com item in the login keychain"
```
A good result: no file line (or `0 github.com line(s)`), then `no github.com item in the login keychain`. Without `-g` or `-w`, `security find-internet-password` reads only the item's attributes, not its password, and asks for nothing. To remove what it found: delete the github.com lines from that file, and the github.com item in Keychain Access (search for github.com in the login keychain); then run the check again.

**7. The repo takes draft PRs, and the token can carry out a verdict.** With `both`, both flows open draft PRs, and a [comparison verdict](../docs/skill-flow.md#what-a-verdict-does) marks one ready, edits its title, comments on the other and closes it. Two things on GitHub's side:

- The repo's GitHub plan must accept draft PRs. A private repo on the Free plan does not: both flows then fail to open their PR (the skill run stops with `pull request not opened: …`, and the staged flow's push stage reports GitHub's refusal).
- The repo's token must be able to mark a PR ready for review (a GraphQL call, `markPullRequestReadyForReview`), edit a PR's title, comment on a PR and close one: write access to the repo's pull requests (for a fine-grained token, **Pull requests: Read and write**; for a classic one, the `repo` scope).

There is no dry check for either: the first compared ticket on the host is the check. A good result: both PRs open as drafts, the comparison message comes, and a tap gives a closing message that lists `Ready for review: PR #…` and `Closed: PR #…`, with no `not finished` message. A verdict stuck on `marking PR #… ready`, `retitling PR #…`, `commenting on PR #…` or `closing PR #…` names GitHub's answer: fix the token's rights, then tap Retry.

---

## Dry-Run Testing (Development)

```bash
cd /home/admin0/tot
source .venv/bin/activate
source .env
python main.py --config config-live --dry-run
```
