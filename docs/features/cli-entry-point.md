# Feature: CLI Entry Point

**Status:** Implemented
**Created:** 2026-04-08
**Updated:** 2026-04-09
**Author:** Oleksandr Brazhenko

## Description

The `main.py` entry point parses CLI arguments and starts the Cleave pipeline. It prints startup diagnostics including the running version, config path, active filters, and a summary of discovered resources and projects.

## Requirements

- FR1: Accept `--config PATH` (required), `--project ID`, `--repo ID`, `--dry-run` flags
- FR2: `--repo` requires `--project`; error otherwise
- FR3: Print version string (`Cleave vX.Y.Z`) at startup so the running version is always visible in logs
- FR4: Read version from `importlib.metadata` when package is installed, falling back to `pyproject.toml`
- FR5: Print project/repo filter and dry-run mode when active
- FR6: Return exit code 0 on success, 1 on configuration error
- FR7: Initialize `EventBus` and `EventStore` and pass them to all components (AgentRuntime, TelegramAdapter, CommandHandler, Orchestrator)
- FR8: When `dashboard.enabled` is true, start a uvicorn server hosting the dashboard web app
- FR9: Persist all events emitted by the bus to the SQLite event store via an async listener

## Technical Approach

- `get_version()` tries `importlib.metadata.version("cleave")` first (catching `PackageNotFoundError` specifically), then parses `pyproject.toml` with a regex fallback wrapped in its own `try/except` that returns `"unknown"` on any error
- Startup line format: `Cleave v{version} starting with config: {path}`

## Acceptance Criteria

- [x] `--help` shows all flags with descriptions
- [x] Missing `--config` exits non-zero with a clear error
- [x] `--repo` without `--project` exits non-zero
- [x] Version string printed at startup (format: `Cleave v{version}`)
- [x] Returns 0 when no matching projects found

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-08 | Initial doc — added version logging at startup |
| 2026-04-08 | Fix `get_version()`: catch `PackageNotFoundError` specifically; wrap pyproject.toml fallback in its own try/except |
| 2026-04-09 | Wire Telegram command layer into startup: initialize `ModeHandler` (with `daemon_state.json` persistence) and, when a `TelegramAdapter` notifier is configured, construct `IntentParser` + `CommandHandler` and attach via `notifier.set_command_handler()` |
| 2026-04-09 | Wire dashboard event system: initialize `EventBus` + `EventStore` after logging setup, inject into all components, start uvicorn dashboard when `dashboard.enabled=true`, persist events to SQLite |
| 2026-04-27 | Add persistent file logging: `RotatingFileHandler` (10 MB × 5 backups) writes to `<logging.dir>/cleave-daemon.log` alongside the existing stderr stream. The configured `logging.dir` (`global.yaml`) is finally used; falls back to `<repo>/data/` if the configured dir isn't writable. Required for post-mortem analysis of agent failures, re-adoption events, and CLI subprocess diagnostics — previously these went only to the run.sh terminal and were lost on scroll/restart. |
