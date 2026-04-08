# Feature: CLI Entry Point

**Status:** Implemented
**Created:** 2026-04-08
**Updated:** 2026-04-08
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
