# Feature: CLI Entry Point

**Status:** Implemented
**Created:** 2026-04-08
**Updated:** 2026-04-09
**Author:** Oleksandr Brazhenko

## Description

The `main.py` entry point parses CLI arguments and starts the Sickle pipeline. It prints startup diagnostics including the running version, config path, active filters, and a summary of discovered resources and projects.

## Requirements

- FR1: Accept `--config PATH` (required), `--project ID`, `--repo ID`, `--dry-run` flags
- FR2: `--repo` requires `--project`; error otherwise
- FR3: Print version string (`Sickle vX.Y.Z`) at startup so the running version is always visible in logs
- FR4: Read version from `importlib.metadata` when package is installed, falling back to `pyproject.toml`
- FR5: Print project/repo filter and dry-run mode when active
- FR6: Return exit code 0 on success, 1 on configuration error
- FR7: Initialize `EventBus` and `EventStore` and pass them to all components (AgentRuntime, TelegramAdapter, CommandHandler, Orchestrator)
- FR8: When `dashboard.enabled` is true, start a uvicorn server hosting the dashboard web app
- FR9: Persist all events emitted by the bus to the SQLite event store via an async listener

## Technical Approach

- `get_version()` tries `importlib.metadata.version("sickle")` first (catching `PackageNotFoundError` specifically), then parses `pyproject.toml` with a regex fallback wrapped in its own `try/except` that returns `"unknown"` on any error
- Startup line format: `Sickle v{version} starting with config: {path}`

## Acceptance Criteria

- [x] `--help` shows all flags with descriptions
- [x] Missing `--config` exits non-zero with a clear error
- [x] `--repo` without `--project` exits non-zero
- [x] Version string printed at startup (format: `Sickle v{version}`)
- [x] Returns 0 when no matching projects found

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-08 | Initial doc — added version logging at startup |
| 2026-04-08 | Fix `get_version()`: catch `PackageNotFoundError` specifically; wrap pyproject.toml fallback in its own try/except |
| 2026-04-09 | Wire Telegram command layer into startup: initialize `ModeHandler` (with `daemon_state.json` persistence) and, when a `TelegramAdapter` notifier is configured, construct `IntentParser` + `CommandHandler` and attach via `notifier.set_command_handler()` |
| 2026-04-09 | Wire dashboard event system: initialize `EventBus` + `EventStore` after logging setup, inject into all components, start uvicorn dashboard when `dashboard.enabled=true`, persist events to SQLite |
