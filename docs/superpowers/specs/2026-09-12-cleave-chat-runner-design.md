# Cleave Chat Runner — Design Spec

**Date:** 2026-09-12
**Status:** Draft — awaiting review

## Problem

Cleave's value is not the daemon. It is the agent system: roles with hard
constraints, judged by gates that do not share the author's context, passing
work to each other through files rather than through one long conversation.
That system is reachable today only by filing a Jira ticket and waiting for a
poll cycle.

Work that would benefit from it — a change you are already looking at in your
editor, a ticket you want driven now, a task with no ticket at all — gets none
of it. You either run the full pipeline (clone, config, Jira, Telegram) or you
get a single undifferentiated Claude Code session with no scope guard, no
plan review, and no coverage gate.

This spec defines `cleave-run`: a Claude Code skill that runs the same agents,
from the same prompt files, against the repository you are already in.

## Non-goals

- **Replacing the pipeline.** The daemon remains the path for ticket-driven,
  unattended work. This is a second entry point, not a migration.
- **Appearing on the dashboard.** Deliberately excluded from v1 — see
  *Dashboard isolation* below for why it is not merely "later" but "separately".
- **Forking the agent prompts.** If this skill ever needs a prompt edited, the
  edit belongs in `agents/*.md` where the pipeline gets it too.
- **PR review handling.** The `pr_review` stage (30-minute delay, comment
  fetching, `pr-comment-responder-agent`) has no sensible chat analogue.

## Decisions

- **Split execution.** `SKILL.md` owns the interactive shell: argument
  parsing, ticket fetch, workspace construction, human gates, final report.
  The stage loop runs as a Workflow script, so iteration counters and pass/fail
  routing are executable code rather than model intent. The pipeline added
  `max_iterations` to `dev` because uncapped bounce loops burned unbounded
  spend ([default-workflow.yaml:35-38](../../../workflows/default-workflow.yaml#L35-L38));
  an LLM-enforced counter would reintroduce exactly that risk in the mode with
  no human watching.
- **Installed globally** at `~/.claude/skills/cleave-run/`, usable in any
  repository.
- **Single source of truth for prompts and graph.** Agents come from
  `$CLEAVE_HOME/agents/*.md` (default `~/Documents/Cleave`); the stage graph
  and every iteration limit are read from
  [workflows/default-workflow.yaml](../../../workflows/default-workflow.yaml)
  at run time and passed to the script as `args`. No copies, no duplicated
  constants.
- **Prompts are adapted, never edited** — via a header preamble prepended to
  each agent's body. See *The header adapter*.
- **Verdicts come from agent responses, not from greps over report files.**
  See *Verdict parsing* — this is the decision with the most evidence behind it.
- **Isolated from the dashboard and the daemon** in v1, with a forward-compatible
  `state.json`.

## Architecture

```
~/.claude/skills/cleave-run/
  SKILL.md                      interactive shell
  references/pipeline.js        Workflow script — the stage loop
  references/stage-header.md    the adapter preamble template
```

Per-run workspace, in the target repository:

```
<repo>/.cleave/<task-id>/
  meta/      ticket.md  parent.md  comments.md  answers.md
  rules/     arch-rules.md
  ba.md  ba-questions.md  developer.md  scope-guard.md  code-review.md  qa.md
  state.json
```

`.cleave/` is added to `.git/info/exclude`, not to the repository's
`.gitignore` — the run leaves no trace in a repo it does not own.

`meta/answers.md` has no pipeline equivalent. It carries your replies to the
BA's open questions and is fed back to the BA on re-entry; the pipeline
delivers the same content over Telegram.

`rules/arch-rules.md` is assembled from whatever the target repo actually has:
`arch-rules.md`, `docs/arch-rules.md`, `CLAUDE.md`, `AGENTS.md`,
`coding-standards*`. When none exist the file is still written, stating
explicitly that there are no configured rules and that conventions must be
inferred from surrounding code — an empty or absent file invites the agent to
invent constraints.

## The header adapter

Each agent's `.md` body is prepended with a generated preamble correcting four
pipeline-specific assumptions. The prompt files themselves are never modified.

1. **Paths.** `ai_pipeline/{ticket_id}/` → `.cleave/<task-id>/`.
2. **Artifacts are not committed.** [dev-agent.md:102](../../../agents/dev-agent.md#L102)
   instructs staging the report directory alongside the code; in someone
   else's repository that is noise.
3. **Escalation goes to chat.** "Notify via Telegram and wait" → "write the
   reason in your report and stop"; the script surfaces it.
4. **Ground Truth is injected.** The changed-file list is computed by the
   runner (`git diff --name-only <merge-base>...HEAD`) and supplied, exactly as
   [agent_runtime.py:338](../../../orchestrator/agent_runtime.py#L338) does.
   `scope-guard-agent` and `code-review-agent` both carry a core principle
   requiring this list to come from the prompt, because agents previously
   derived it with a bad pathspec and reasoned from the wrong answer.

`HARD_SAFETY_RULES` ([agent_runtime.py:809](../../../orchestrator/agent_runtime.py#L809))
is appended last, as in the pipeline.

## Stage graph and control flow

Six of the ten agents are used: `ba-agent`, `plan-review-agent`,
`dev-agent`, `scope-guard-agent`, `code-review-agent`, `qa-agent`. Dropped:
`pm-agent` (prioritises across tickets; a chat run has one task), `fix-agent`,
`project-setup-agent` (already exposed as slash commands), and
`pr-comment-responder-agent` (belongs to the dropped `pr_review` stage).

```
analysis ──→ plan_review ──PASS──→ dev ──→ scope_check ──PASS──→ code_review ──PASS──→ qa ──PASS──→ push gate
   ↑              │FAIL                ↑         │FAIL                 │FAIL              │FAIL
   └──────────────┘                    └─────────┴─────────────────────┴──────────────────┘
```

The autopilot window starts at `analysis`, not at `dev`. This follows from
`plan_review`'s `on_fail: "analysis"` back-edge — the BA must be re-runnable
from inside the loop. The `analysis ↔ plan_review` cycle needs no counter of
its own; `plan_review`'s `max_iterations: 2` bounds it.

Limits, read from the YAML: `plan_review` ≤2, `dev` ≤3 total entries,
`scope_check` / `code_review` / `qa` ≤2 consecutive failures each, counters
resetting on pass. Exhausting any of them escalates to chat.

The script returns a discriminated result: `{outcome: "questions" | "escalated"
| "ready", stage, reason, artifacts}`. `"questions"` and `"escalated"` both
hand control back to `SKILL.md`, which talks to you and may restart the script.

## Verdict parsing

**Verdicts are read from each agent's structured response via `agent()`'s
`schema`, not by matching text in a report file.**

The pipeline's generic matcher, `_looks_like_pass`
([agent_stage.py:87](../../../orchestrator/pipeline/agent_stage.py#L87)),
accepts the substrings `"advances to"` and `"verdict: hold"` as passes, and
`parse_agent_outcome` tests pass before fail. When `plan_review` was added, it
was given a strict anchored regex instead, with this rationale recorded at
[agent_stage.py:107](../../../orchestrator/pipeline/agent_stage.py#L107-L114):

> A plan-review report reading "the plan advances to dev only if..." would be
> read as PASS — the exact inversion this gate exists to prevent.

A substring grep over a judge's prose is not a verdict. A structured field is.
Two further properties follow:

- **Fail closed.** An unparseable or missing verdict is `FAIL`, never `PASS`.
- **No stale-verdict hazard.** `_write_agent_output` only appends, so the
  pipeline must clear `plan-review-agent-output.md` before re-entry and read
  only `last_attempt_block`, or a re-reviewed plan would be scored on its
  previous rejection. Each `agent()` call returns its own response, so this
  class of bug does not exist here.

`plan-review-agent` additionally writes no files at all — its constraints
forbid it, and its verdict is the first line of its response. Any design that
locates verdicts in files cannot accommodate it.

## Mechanical verifiers

Two are ported, both of which exist because of observed failures:

- **`dev` advanced HEAD.** Logic from
  [stage_verifier.py:193](../../../orchestrator/stage_verifier.py#L193): HEAD
  moved during the stage, or the branch carries commits the base does not.
  Catches the dev agent that analyses and exits. The "completed in under 60s
  with no changes" heuristic is kept.
- **The stage produced its artifact.** Applies to `analysis`, `dev`,
  `scope_check`, `code_review`, `qa`. **Not** to `plan_review`, which produces
  no file by design; there, the parsed verdict is the evidence.

A third is ported in reduced form: a grep over newly added test files for
reads of paths under `src/`, approximating
[fake_test_detector.py](../../../orchestrator/fake_test_detector.py). The
`qa-agent` prompt names this failure mode explicitly ("a test that greps
source cannot fail and does not count as coverage"), so the gate is not
speculative — but the port is a heuristic, not a reimplementation.

## Human gates

Two, both blocking:

1. **After `plan_review`** — you see the plan and Surveyor's verdict together.
   This mirrors the pipeline's manual mode, which gates at exactly
   `PLAN_REVIEW` and `QA` ([approval_gate.py:17](../../../orchestrator/approval_gate.py#L17)).
   Reviewing an unreviewed plan is strictly worse: the reviewer exists because
   a plan defect is invisible to every later gate.
2. **Before `git push` + PR.** Branch and commits are local and cheap to undo;
   a push and a PR are visible to other people and are not.

Plus one early exit: if the BA writes `ba-questions.md`, the script stops and
`SKILL.md` puts the questions to you via `AskUserQuestion`. Answers land in
`meta/answers.md` and the BA re-runs. Everything else runs unattended.

## Git

Branch `feature/<task-id>-<slug>`; commits are made by `dev-agent` under its
existing rules. After the push gate: `git push` and `gh pr create`.

## Input

An argument matching `^[A-Z][A-Z0-9]+-\d+$` is treated as a Jira key and
fetched through the Atlassian MCP connector into `meta/ticket.md`,
`meta/parent.md` and `meta/comments.md`. Anything else is free-form task text,
written to `meta/ticket.md` in the same shape, with a slug-derived task id.
Ticket text is wrapped in `<ticket_content>` tags, which every agent's
constraints already require be treated as data.

## Dashboard isolation

The dashboard discovers runs by scanning one directory for `state.json`
([web.py:195](../../../dashboard/web.py#L195)), rooted at
`workspaces.base_dir` = `/Users/olexandrbrazhenko/CleaveData/workspaces`. The
chat runner writes inside the target repository, outside that root. Isolation
is therefore structural, not enforced: chat runs are invisible to the
dashboard, to the daemon, and to `events.db`, and no existing code changes.

Surfacing these runs later is a separate task, and a careful one, because two
paths are actively hostile to a workspace the daemon did not create:

- `delete_workspace` calls `shutil.rmtree` on the path named by
  `state.workspace_root` ([actions.py:452](../../../dashboard/actions.py#L452)).
  The natural value for a chat run — the repository being worked in — would
  make the dashboard's Delete button erase the working repository.
- `_find_workspace` appends any workspace it finds on disk to
  `orchestrator._active_workspaces` ([actions.py:67](../../../dashboard/actions.py#L67)),
  which is the list the poll cycle advances
  ([orchestrator.py:291](../../../orchestrator/orchestrator.py#L291)). A single
  button press would hand your chat run to the live daemon.

**Forward compatibility:** `state.json` is written with `WorkspaceState`-compatible
field names plus `kind: "chat"`, so surfacing runs later requires dashboard
changes rather than a format migration. `workspace_root` points at
`.cleave/<task-id>/` and **never** at the repository root — so that even an
accidental `rmtree` destroys artifacts, not source.

## Known divergences from the pipeline

- **No dev session resume.** `dev` carries `resume_from_previous: true` so it
  remembers why the code looks as it does and patches rather than rewrites.
  Subagents cannot resume a session. Compensated by feeding the re-entered dev
  its own `developer.md` plus the report that bounced it. Weaker than the
  original; the most likely source of behavioural difference.
- **No per-ticket model selection, quota deferral, or Telegram.**
- **`pr_review` absent**, so PR comments are not fed back.

## Success criteria

1. `/cleave <JIRA-KEY>` in an unrelated repository produces `ba.md`, a
   Surveyor verdict, and stops at the plan gate.
2. A deliberately over-broad plan is failed by `plan_review` and returns to
   `analysis`, and the counter escalates after two failures rather than looping.
3. A dev stage that writes no code is caught by the HEAD verifier, not by a
   later gate.
4. A run completes to the push gate on a real ticket, and the diff is one a
   reviewer would accept.
5. During and after all of the above, `CleaveData/workspaces` is unchanged and
   the dashboard lists no new cards.
