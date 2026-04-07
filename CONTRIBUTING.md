# Contributing to Sickle

## Feature Documentation Rule

Any code change that adds, modifies, or removes feature behavior **must** be accompanied by an update to the relevant feature doc in `docs/features/` and the feature index at `docs/features/index.md`.

A pre-commit hook enforces this rule. If you change files in tracked directories without updating a feature doc, the commit will be blocked.

### Tracked Directories

Changes to these directories require a feature doc update:

- `agents/`
- `orchestrator/`
- `integrations/`
- `workflows/`
- `tasks/`
- `checklists/`
- `main.py`

### Directories That Do NOT Require Feature Doc Updates

- `tests/`
- `config/`
- `data/`
- `docs/` (except `docs/features/` itself)
- `deploy/`
- `.gitignore`, `README.md`, `pyproject.toml`, `package.json`

### When to Create a New Feature Doc

Create a new feature doc when adding a new capability that does not fit any existing feature in the index. Use the template below.

### When to Update an Existing Feature Doc

- When modifying behavior covered by that feature
- When completing acceptance criteria (check them off)
- When changing the feature's status (Planned → In Progress → Implemented)

### What to Update

**In the feature doc (`docs/features/<feature>.md`):**
- Relevant sections (requirements, technical approach, etc.)
- `Updated` date
- Change log entry
- Status field if changed
- Check off completed acceptance criteria

**In the index (`docs/features/index.md`):**
- Status column if changed
- Description if refined

### Feature Doc Template

```
# Feature: <Feature Name>

**Status:** Planned | In Progress | Implemented
**Created:** YYYY-MM-DD
**Updated:** YYYY-MM-DD
**Author:** <name>

## Description

Brief overview of what this feature does and why it exists.

## Requirements

- FR1: ...

## Technical Approach

How this will be implemented.

## Dependencies

- Other features or systems this depends on.

## Acceptance Criteria

- [ ] Criterion 1

## Change Log

| Date | Description |
|------|-------------|
| YYYY-MM-DD | Initial draft |
```

### Pre-Commit Hook Setup

After cloning the repository, install the git hooks:

```bash
bash scripts/install-hooks.sh
```

This copies the pre-commit hook from `scripts/pre-commit` into `.git/hooks/` and makes it executable. The hook will block commits that modify tracked directories without updating a feature doc.
