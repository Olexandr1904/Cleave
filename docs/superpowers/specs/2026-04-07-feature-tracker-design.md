# Feature Tracker System — Design Spec

## Overview

A markdown-based feature tracking system for the Sickle project. Provides a central index of all features with their status, links to detailed per-feature documents (mini-PRDs), and enforces documentation updates via a pre-commit hook and a documented convention.

## Goals

- Single place to see all features and their current status at a glance
- Each feature has a detailed spec with requirements, technical approach, dependencies, and acceptance criteria
- Code changes to feature-relevant directories must be accompanied by doc updates — enforced by a pre-commit hook
- Convention is documented so all contributors (human and AI agents) follow it

## File Structure

```
docs/features/
├── index.md                          # Main table of contents
├── agent-system.md                   # Individual feature docs
├── orchestrator.md
├── workspace-isolation.md
├── configuration-cascade.md
├── jira-integration.md
├── github-integration.md
├── telegram-notifications.md
├── qa-pipeline.md
├── merge-agent.md
└── scope-guard.md

CONTRIBUTING.md                       # Convention documentation (project root)
.git/hooks/pre-commit                 # Enforcement hook
```

## Component 1: Feature Index (`docs/features/index.md`)

A markdown file containing a table with all features:

```markdown
# Sickle — Feature Tracker

| # | Feature | Status | Description |
|---|---------|--------|-------------|
| 1 | [Agent System (BMAD-style)](agent-system.md) | In Progress | Pluggable prompt-file agents with persona, tasks, checklists |
| 2 | [Orchestrator](orchestrator.md) | In Progress | Main loop, slot management, workspace spawning, state machine |
| ...
```

### Statuses

Three possible values:

- **Planned** — feature is defined but no implementation work has started
- **In Progress** — implementation is underway
- **Implemented** — feature is complete and meets acceptance criteria

### Rules

- Each row links to the corresponding feature doc via relative path
- Numbering is for readability only, not used programmatically
- Status in the index must match the status in the feature doc's header
- New features are added at the bottom; ordering can be rearranged for logical grouping

## Component 2: Feature Documents

Each feature gets its own markdown file in `docs/features/` using this template:

```markdown
# Feature: <Feature Name>

**Status:** Planned | In Progress | Implemented
**Created:** YYYY-MM-DD
**Updated:** YYYY-MM-DD
**Author:** <name>

## Description

Brief overview of what this feature does and why it exists.

## Requirements

- FR1: ...
- FR2: ...

## Technical Approach

How this will be implemented — key components, data flow, integration points.

## Dependencies

- Other features or external systems this depends on
- Config files or agents involved

## Acceptance Criteria

- [ ] Criterion 1
- [ ] Criterion 2

## Change Log

| Date | Description |
|------|-------------|
| YYYY-MM-DD | Initial draft |
```

### Rules

- Filename is kebab-case matching the feature identifier (e.g., `workspace-isolation.md`)
- Status field must be kept in sync with the index table
- Change log is appended on every update — never overwritten
- Acceptance criteria checkboxes are checked off as work completes
- `Updated` date is set on every modification

## Component 3: Pre-Commit Hook

A bash script installed at `.git/hooks/pre-commit`.

### Behavior

1. Collects the list of staged files (`git diff --cached --name-only`)
2. Checks if any staged file is in a **tracked directory**
3. If yes, checks if at least one file in `docs/features/` is also staged
4. If no feature doc is staged, prints an error and exits with code 1 (blocks the commit)

### Tracked Directories (trigger doc update requirement)

- `agents/`
- `orchestrator/`
- `integrations/`
- `workflows/`
- `tasks/`
- `checklists/`
- `main.py`

### Excluded Directories (do not require doc updates)

- `tests/`
- `config/`
- `data/`
- `docs/`
- `deploy/`
- `.gitignore`, `README.md`, `pyproject.toml`, `package.json`

### Error Message

```
Commit blocked: You modified files in tracked directories but no feature
doc in docs/features/ was updated.

Modified tracked files:
  - agents/dev-agent.md
  - orchestrator/orchestrator.py

Update the relevant feature doc and docs/features/index.md, then try again.
```

### Installation

The hook script source lives at `scripts/pre-commit` (tracked in git). A `scripts/install-hooks.sh` helper copies it to `.git/hooks/pre-commit` and sets it executable. New clones must run `bash scripts/install-hooks.sh` — this is documented in CONTRIBUTING.md.

## Component 4: Convention Documentation (`CONTRIBUTING.md`)

A file in the project root documenting the rule:

### Content

1. **The Rule** — Any code change that adds, modifies, or removes feature behavior must update the relevant feature doc and the index
2. **When to create a new feature doc** — When adding a new capability that doesn't fit an existing feature
3. **When to update an existing doc** — When modifying behavior, completing acceptance criteria, or changing status
4. **What to update:**
   - Feature doc: relevant sections, `Updated` date, change log entry, status if changed
   - Index: status column if changed, description if refined
5. **Pre-commit hook** — Explains that the hook enforces the rule and how tracked directories work
6. **Hook installation** — Instructions for setting up the hook on a fresh clone

## Component 5: Initial Feature Seed

The following features will be seeded from the existing PRD and architecture docs:

| # | Feature | Initial Status | Source |
|---|---------|---------------|--------|
| 1 | Agent System (BMAD-style) | In Progress | PRD FR1-FR4, agents/ directory exists |
| 2 | Orchestrator | In Progress | PRD FR5-FR8, orchestrator/ directory exists |
| 3 | Workspace Isolation | Planned | PRD FR15-FR18 |
| 4 | Configuration Cascade | Planned | PRD FR9-FR14 |
| 5 | Jira Integration | Planned | PRD FR19-FR22 |
| 6 | GitHub Integration | Planned | PRD FR23-FR27 |
| 7 | Telegram Notifications | Planned | PRD FR28-FR31 |
| 8 | QA Pipeline | Planned | PRD FR32-FR36 |
| 9 | Merge Agent | Planned | PRD FR37-FR39 |
| 10 | Scope Guard | Planned | PRD FR40-FR42 |

Each feature doc will be populated with requirements, technical approach, and acceptance criteria extracted from the existing PRD and architecture documents. The feature docs become the living, up-to-date source; the original PRD/architecture docs remain as historical reference.

## Out of Scope

- Auto-generation of the index from frontmatter (can be added later if needed)
- CI-based enforcement (pre-commit hook is sufficient for now)
- Feature dependency graph or roadmap visualization
- Version numbering for feature docs
