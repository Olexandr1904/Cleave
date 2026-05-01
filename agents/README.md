# Agent Prompt Files

Cleave agents follow the **BMAD-style** prompt file format. Each agent is a standalone Markdown file with YAML frontmatter metadata and Markdown body instructions.

## File Format

```markdown
---
agent:
  id: "agent-id"          # Unique identifier (matches filename without .md)
  name: "AgentName"       # Human-readable name / persona
  title: "Role Title"     # Role title

persona:
  role: "Role description"
  style: "Communication style"
  identity: "One-line identity statement"

core_principles:
  - "Principle 1"
  - "Principle 2"

dependencies:
  tasks:
    - "task-id"           # References files in tasks/ by id
  checklists:
    - "checklist-id"      # References files in checklists/ by id
  data:
    - "data-id"           # References files in data/ by id
---

# Agent Title

## Activation
Persona activation paragraph.

## Input
What the agent receives.

## Process
Step-by-step instructions.

## Output
What the agent produces.

## Constraints
Hard rules the agent must follow.
```

## Creating a New Agent

1. Create `agents/{agent-id}.md` following the format above
2. Ensure the `id` in frontmatter matches the filename (without `.md`)
3. Reference any tasks, checklists, or data files by their `id` — not file path
4. The agent is automatically discovered by the resource registry on startup
5. Add the agent to the workflow YAML if it should be part of the pipeline

## Existing Agents

| Agent | ID | Role |
|-------|-----|------|
| Marcus | `pm-agent` | Ticket prioritization and routing |
| Alice | `ba-agent` | Requirements validation and implementation planning |
| James | `dev-agent` | Code implementation following plans |
| Scope Guard | `scope-guard-agent` | Validates dev-agent diffs against the plan and architecture rules; fails the stage if scope creep is detected |
| QA | `qa-agent` | Writes tests and runs lint / test / build gates |
| Fix | `fix-agent` | Targeted recovery agent for narrow failure categories |
| PR Comment Responder | `pr-comment-responder-agent` | Classifies PR review comments and decides Fix / Won't Fix / re-investigate |
| Atlas | `project-setup-agent` | Onboards new projects (interactive add/list/remove); used by the dashboard wizard and the `/add-project`, `/list-projects`, `/remove-project` slash commands |

## Dependencies

Agents can declare dependencies on:
- **Tasks** (`tasks/`): Step-by-step procedures the agent follows
- **Checklists** (`checklists/`): Validation checklists the agent runs through
- **Data** (`data/`): Reference data injected into the agent's context

All dependencies are resolved by `id` at startup. Missing dependencies produce warnings in the log.

## Model Selection

Agents do not pick their own Claude model. The model is decided per ticket at workspace creation and stored on `WorkspaceState.model` — every agent dispatched against that ticket uses it. Operators can override per ticket with a Jira label (`model-haiku`, `model-opus`, `model-sonnet`); otherwise the global default from the dashboard is snapshotted. See [docs/features/per-ticket-model-selection.md](../docs/features/per-ticket-model-selection.md).
