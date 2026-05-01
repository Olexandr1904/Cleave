# Troubleshooting

What to do when Cleave isn't doing what you expect.

This doc is a user-facing reference for diagnosing common problems. For implementation/spec content, see [docs/features/](features/).

---

## First: ask an AI

Cleave's codebase is opinionated and the error messages are usually specific. Before diving into logs, try this:

1. Copy the relevant error log lines (from `/var/log/cleave/cleave-daemon.log` or `<workspace>/logs/`).
2. Copy the command you ran and any relevant `state.json`.
3. Paste it into Claude (or your AI assistant of choice) along with: "I'm running Cleave, an autonomous AI dev pipeline. Here's what happened: …"

AI assistants are particularly good at the following common categories of failure:

- Jira label / status / project-key mismatches
- GitHub PAT scope issues, or token expired
- Missing JDK on the host (Android/Kotlin projects fail QA silently otherwise)
- Claude Code CLI auth not done (browser flow never completed)
- Gradle / AAPT2 cache corruption
- Git identity not set on the host
- `git push` fails because SSH key / credential helper isn't set up
- YAML schema mismatches in `config-live/`
- Telegram chat-id format (negative numbers for groups, positive for personal)

If the AI can't help, the rest of this doc covers the structured diagnostics.

---

## Diagnostic order (most useful first)

### 1. Dashboard health strip

If the dashboard is reachable, the **Project Health** strip on the Board (per project) is the fastest signal — green / yellow / red, with click-to-expand fix hints. See [docs/dashboard.md](dashboard.md#project-health-strip) for what each check covers.

### 2. Pre-flight health check (CLI)

If the dashboard isn't reachable, run the same validators from the CLI without starting the daemon:

```bash
python -m health.runner --config config-live
```

It prints each project's check results. Use this in CI or when smoke-testing a new VPS.

### 3. Daemon log

```
/var/log/cleave/cleave-daemon.log
```

- Configurable via `global.yaml` → `logging.dir`
- If the configured dir isn't writable, the daemon falls back to `./data/`
- Rotating, 10 MB × 5 backups
- All `logger.warning` / `error` / `info` calls land here

Tail it during a poll cycle:

```bash
tail -f /var/log/cleave/cleave-daemon.log
```

### 4. Per-workspace logs and reports

Every workspace keeps its own logs and agent reports:

```
<workspaces.base_dir>/<project-id>/<repo-id>/tickets/<ticket-id>/
  state.json        — current pipeline state
  meta/             — ticket.md, plan.md, etc.
  logs/             — per-stage stdout/stderr from agents
  source/           — git clone
    reports/        — Markdown reports the agents wrote (BA plan, QA output, etc.)
```

`workspaces.base_dir` is set in `global.yaml` (schema default `/data`; the example config-live ships `/data/cleave`).

The dashboard's Ticket Detail view exposes the same files inline — you don't need to ssh in.

### 5. Dry-run the daemon

```bash
source .env && python main.py --config config-live --dry-run
```

Polls Jira and logs everything it *would* do without writing files, creating workspaces, or pushing. Good for "is the daemon seeing my tickets at all?" questions.

### 6. Single-project / single-repo run

If a daemon serving many projects is misbehaving, narrow the blast radius:

```bash
source .env && python main.py --config config-live --project <project-id> --repo <repo-id>
```

Only that combination is processed.

### 7. Event Log on the dashboard

The Event Log view is a complete record of orchestrator activity. Filter by `agent_failed`, `stage_transition`, or `poll_cycle` to spot patterns. Useful when something *looks* wrong but the workspace state alone isn't enough context.

---

## Common symptoms and where to start

| Symptom | First thing to check |
|---|---|
| Daemon won't start | `python -m health.runner --config config-live` then daemon log |
| Dashboard isn't reachable | Daemon log for "Dashboard:" line, then `dashboard.host`/`dashboard.port` in `global.yaml` |
| Jira tickets aren't being picked up | Jira labels (trigger AND ignore — see [docs/labels.md](labels.md)), Jira status, `project_key`, `status_to.todo` |
| Ticket goes to FAILED immediately | Per-workspace `logs/` for the failing stage; check `architecture.protected_files` if dev-agent failed |
| Ticket goes to BLOCKED with no message | Stage verifier kicked in — agent didn't produce mechanical effects (e.g. dev-agent didn't commit). See `state.error` and `human_input_question` |
| Ticket loops DEV → SCOPE_CHECK → DEV | Scope-Guard is rejecting; check the scope-guard report in workspace `source/reports/` |
| Ticket loops DEV → QA → DEV | QA gates are red; check QA report. If it's a flaky test, soften with `linting.hard_gate: false` etc. |
| Telegram messages don't arrive | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` set, chat is on the allowlist (see [docs/telegram.md](telegram.md)) |
| Bot ignores my messages | Allowlist (chat id mismatch — `/status` from a different chat won't work). Daemon log shows `tg_message_received` events from allowed chats only |
| AAPT2 / Gradle cache corruption | Use the **🧹 Clear cache & retry** button on the FAILED card or in Telegram |
| All agents fail with quota errors | DEFERRED auto-resume should kick in; if it doesn't, check Anthropic console quota usage |
| Wizard validation fails | Live API check failed before writing config — credentials, scope, or unreachable host. The wizard tells you which |
| Daemon was running but ran out of disk | Workspaces under `<workspaces.base_dir>` may be growing; check `max_workspace_size_gb` and `max_age_days` in `global.yaml` |

---

## Common environment fixes

Most environment issues — git identity, git push auth, JDK installation, Claude Code CLI auth — are covered in the host-configuration section of [docs/setup-guide.md §1](setup-guide.md#host-configuration). Don't duplicate the recipes here; the setup guide is the source of truth.

The four most common host-config root causes:

- **Git identity not set** — `git config --global user.{name,email}` (otherwise dev-agent's `git commit` refuses)
- **`git push` doesn't work from your shell** — fix the SSH key / HTTPS credential helper before running the daemon
- **Missing JDK** — Android/Kotlin projects fail QA silently without `openjdk-17-jdk`
- **Claude Code CLI never authenticated** — run `claude` once interactively, complete the browser flow

---

## When to file a bug

If you've gone through the above and the failure is in the orchestrator, an agent's tool sandbox, the dashboard, or an integration adapter — and the AI assistant agrees it isn't a config issue — open a GitHub issue with:

- Cleave version (`./run.sh` prints it on startup, or `python main.py --config config-live --help` after install)
- The relevant `state.json` (redact tokens)
- The daemon log lines around the failure
- The agent report, if the failure is in an agent

---

## See also

- [docs/dashboard.md](dashboard.md) — Project Health strip details
- [docs/telegram.md](telegram.md) — Telegram-side diagnostics (`/status`, intent classification)
- [docs/labels.md](labels.md) — Jira label semantics
- [docs/setup-guide.md](setup-guide.md) — full installation reference, including pre-flight checks
- [deploy/README.md](../deploy/README.md) — production-only concerns (systemd, log rotation)
