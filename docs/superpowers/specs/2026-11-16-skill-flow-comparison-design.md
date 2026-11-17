# Skill flow and the two-flow comparison — design

**Date:** 2026-11-16
**Status:** approved by the operator on 2026-11-16

## Intent

Cleave's ticket pipeline gives poor results: of about 20 tickets the operator took one or two without changes, and some fixes were made in the wrong place. The pipeline splits one developer's work across six agents that talk through files; the agent that writes the code never builds it, the tests are written by another agent, and the repo's own rules barely reach either.

The operator's hand-run results are good when one session works a ticket from start to finish with the repo's own skills: one skill for bugs and one for tasks. Those skills already have what he wants from Cleave: a confidence table, stop conditions that refuse to start on an unclear ticket, one stop before any code, test-first work, their own code review, and a machine-readable status line for an orchestrator.

So Cleave gets a second way to work a ticket, the **skill flow**: one `claude` session per ticket that runs the repo's skill, with Cleave around it as the conveyor. For a while every ticket is worked by **both** flows, the old **staged flow** and the skill flow, and each opens its own draft PR. The operator compares the two PRs and records which one is better. That record decides whether the staged agents are removed.

## Goals

- A skill flow that works a ticket in one session with the repo's own skills, and ends in a draft PR.
- An unclear ticket does not start: the skill's stop reaches the operator in Telegram, and his reply continues the same session.
- In manual mode the skill's one stop before code waits for the operator's "ok" in Telegram. In auto mode it does not wait.
- A comparison mode per repo: every ticket goes through both flows; two draft PRs; a verdict from the operator; a tally on the dashboard.
- Cleave keeps what it is good at for the skill flow too: its own gates by exit code, the fake-test detector, push and PR by code, quota deferral, watchdogs, Telegram.

## Non-goals

- Removing the staged agents. That is a later decision, made from the tally.
- A skill-only mode (a ticket worked by the skill flow alone). The design must not block it, but this project builds `staged` and `both`.
- Moving the "answer PR comments" logic out of the ticket pipeline into the reviewer subsystem. The operator wants that; it is its own project. Here it is only switched off for compared tickets.
- Running the app on a device from the host. Without an emulator the skill reports `tested: partial`; that is accepted.
- Posting the skill's tracker comment drafts. The tracker stays read-only for the skill flow.

## Anti-goals

- Cleave carrying its own copy of the skills' method. The method lives in the repo and the operator improves it there; a copy in Cleave would lag. (Same rule as reviewer mode: the repo owns its process.)
- A session that holds a forge, tracker or Telegram token.
- Two automated flows answering reviewers or pushing to each other's branch.
- Anything that names an employer, in code, tests, docs or examples.

## Terms

- **Staged flow** — today's pipeline: analysis, plan review, dev, scope check, code review, QA, push, PR review. One workspace per ticket, everything keyed by the ticket id.
- **Skill flow** — the new one: one session per ticket running the repo's skill.
- **Skill run** — one ticket's pass through the skill flow. It has its own id and state; it is not a workspace.
- **Comparison** — the pair (staged workspace, skill run) of one ticket, plus the operator's verdict.

## Approaches

**A. Cleave as a conveyor around the repo's skills (chosen).** One session runs the repo's bug skill or task skill. Cleave prepares the ticket files and the checkout, relays stops to Telegram, runs its own gates, pushes and opens the PR. Trade-off: it needs the skills' plugins on the host, and a repo without such skills cannot use it.

**B. Port the skills' method into Cleave's own prompts.** Works for any repo. Rejected: two copies of the method drift apart, and it copies a repo's text into Cleave.

**C. Keep the staged flow and strengthen its dev agent** (build, tests, repo rules, newer model). Cheapest. Rejected as the main path: six cold agents remain, which is the suspected cause. The cheap parts that help any flow were done on 2026-11-16 (Claude 5 models, longer Bash timeouts).

**How the second flow is hosted.** The staged pipeline keys everything by the ticket id: workspaces on disk, Telegram buttons (`action:ticket_id`), dashboard lookups, events, session ids, the branch name. A second workspace under the same ticket id would break all of them. So the skill flow is **its own subsystem next to the pipeline**, built the way reviewer mode is: its own store, job runner, checkout, session runner, Telegram anchors and dashboard section. The staged pipeline changes in four small places only (see "Changes to the staged flow").

## How a skill run works

### 1. Intake

A repo's config says which flows work its tickets: `ticket_flow: staged | both` (default `staged`). With `both`, when the staged flow creates a workspace for a ticket, Cleave also creates a skill run for it and a comparison row that joins the two. The skill run takes a **copy of the staged workspace's `meta/` files** as they were at creation (ticket, parent, comments, history, attachments), so both flows start from the same input, taken before Cleave moved the ticket to In Progress.

The skill is chosen by the ticket's issue type: the types listed in `bug_types` use `bug_skill`, every other type uses `task_skill`.

The tickets switch (running or paused, per repo) governs skill runs as it governs workspaces: a paused repo starts no run; a running session finishes.

### 2. Checkout

The skills expect a long-lived main checkout with one worktree per ticket inside it, not a fresh clone per ticket. Cleave keeps one **skill checkout** per repo under its data directory (next to the reviewer's clones, in its own `skill/` tree), made once by hand (see Host prerequisites) and fetched before each run, with the token passed only through the git environment as the reviewer's clone does. After the fetch Cleave moves the checkout to the base branch's head, detached and without force, so the session runs the repo's current skills; a checkout that cannot move (local changes to tracked files) stops the run with a line that says so. Nothing cleans the checkout: the skills keep their notes and worktrees inside it.

Before a run Cleave writes the ticket copy into the skill's notes directory for that ticket (`notes_dir`, a repo-relative pattern such as `ai_instructions/{key}`) under `ticket/`, and checks that every file in `required_files` (for example a local build properties file the operator places once) exists in the checkout. A missing file stops the run with a line that names it.

**One build at a time (the designer's call, not the operator's).** A skill session builds throughout, so it takes the pipeline's one build slot (`max_parallel_builds`, shared by every repo) for as long as a `claude` process or a gate command of the run is alive, and releases it between turns. A run that is `waiting`, `stopped` or `deferred` holds nothing and does not keep the next ticket's run from starting: each ticket has its own worktree, so several runs may be parked at once. The cost is accepted: the two sides of a ticket build one after the other, so a compared ticket takes up to twice as long, and a staged stage that needs the slot waits for a skill session as it already waits for another staged build.

### 3. The session

One `claude -p` run in the skill checkout, started through the existing adapter:

- **Prompt:** a short instruction to run the chosen skill for the ticket key, plus Cleave's **host notes**. The host notes say: the ticket is saved under the notes directory and is to be read there, not fetched; the base branch is already fetched; the session has no network credentials, so it must not fetch, push, open a PR or write to the tracker; the facts the skill's ownership check needs (assignee, status at intake, branches and PRs that carry the key) with Cleave's own staged branch and PR named as Cleave's and not as someone else's work; and the free-text `host_notes` from the repo's config (for example "no emulator on this host").
- **Mode:** in auto mode the instruction carries the skill's `unattended` argument. In manual mode it does not, so the skill makes its one stop before code.
- **Model and effort:** the model the ticket resolved to (the same one the staged workspace uses, so the comparison is fair); the effort from `skill_flow.effort`.
- **Tools:** everything the skill needs, including `Skill` and `Agent`, passed as raw tool names. The CLI is started without `--restricted`, because the skill needs the plugins installed in the host's profile, and with `--strict-mcp-config`, so no MCP server loads.
- **Environment:** a scrubbed one, as the reviewer's session has, plus the build variables the repo needs (`build_env` names, for example `JAVA_HOME`, `ANDROID_HOME`). No forge, tracker, Telegram or API-key variable.
- **Deny rules (the designer's call, not the operator's):** the scrubbed environment keeps `HOME`, so a login stored on the host would still be reachable. Cleave therefore passes its own settings file with deny rules, in the reviewer's style: `gh`, `glab`, `curl`, `wget`, `security`, the git network and config commands (`push`, `fetch`, `pull`, `ls-remote`, `remote`, `config`, `credential`), and reads of credential files. The skills treat a refused tool call as "skip the step and record it", and the facts their ownership check would fetch come from Cleave's host notes. These rules are a second line, not a sandbox.
- **System prompt:** Cleave's existing hard safety rules; the ticket text is data, not instructions.
- **Limits:** `skill_flow.timeout_seconds` wall clock, the adapter's stall and post-result watchdogs, quota deferral. A deferred run resumes the same session when the quota resets.

### 4. What a turn's end means

The skills end an unattended run with one status line as the first line of the final message:

```
<KEY> | <outcome> | <field>: <value> | … | needs user: yes/no
```

Cleave reads the first line of the session's result:

- **A status line for this key** — the run is finished. The outcome is the second field. An outcome that starts with `stopped:` is a stop. Any other outcome is a result: with a change in the worktree it goes to the gates; with no change (for example "no app fix") it ends as `no_change`.
- **Anything else** — the session is waiting for the operator. Cleave sends the message to Telegram and parks the run as `waiting`.

A stop, a `needs user: yes` and a `no_change` each reach the operator in Telegram with the skill's report text and the path of its notes.

### 5. Telegram

Each message Cleave sends for a skill run is anchored by its message id, as review messages are. The operator's reply to that message goes to that run:

- **A waiting run:** the reply text resumes the same session (`--resume`). An "ok" before code is followed by Cleave's own instruction to finish unattended, so the rest of the run ends in a status line.
- **A stopped run:** the reply text resumes the session as new information from the operator.
- **Buttons** on a waiting or stopped message: **Approve** (the same as replying "ok") and **Drop** (ends the run as `dropped`; nothing is pushed).

Messages relay the skill's own text unchanged, in the language the repo's skill writes. Cleave's own lines around it are English.

### 6. Gates, commit, push, draft PR

After a result with a change:

1. **Commit:** Cleave resumes the session and asks it to commit by the repo's convention and to write the PR description to a file in the notes directory. Cleave then checks that the branch is ahead of the base, the worktree is clean and the description file is there.
2. **Cleave's gates** run on that committed tree in the ticket's worktree: the commands in `skill_flow.gates` (test, lint, build), judged by exit code, and the fake-test detector on the diff. The staged flow's gate commands are the default when `gates` is empty.
3. **A failing gate** resumes the session with the failing command and the tail of its output, at most `gate_rounds` times (default 2); the session fixes and commits, and the gates run again. After that the run stops and the operator is told.
4. **Push and PR:** Cleave pushes the branch itself (no force) and opens a **draft** PR against the base branch. The title is the staged flow's title form with ` [skill]` at the end; the body is the description file.
5. The run ends as `pr_open`.

The commit comes before the gates (the designer's call, not the operator's): the gates then judge a clean, committed tree, and what Cleave pushes is exactly the tree they passed.

The worktree stays until the comparison has a verdict or the run is dropped; then Cleave removes it (never with force; a refusal is reported).

### 7. Run states

`queued → running → waiting ⇄ running → gates → pr_open`, with the side exits `stopped`, `no_change`, `failed`, `deferred` (quota; returns to `running`), `dropped` and `interrupted` (daemon restart; the run is offered again, as reviewer jobs are).

A run parks where the operator can move it on: `waiting` (the session asked something), `stopped` (the skill's stop, gates that still fail, a failed commit check, push or session) and `interrupted`. A reply resumes the session with its text; **Approve** continues from the step the run stopped at; **Drop** ends it. `failed` is kept for an error in Cleave itself.

## The comparison

- With `ticket_flow: both`, the staged flow's PR is also opened as a **draft**, with ` [staged]` at the end of its title.
- **At rest.** A side is at rest when it has an open PR or has given up for now: for the staged workspace PR review, blocked, failed or done; for the skill run `pr_open`, `stopped`, `no_change`, `failed` or `dropped`. A side parked for the operator's approval (`waiting`, or the staged approval gates) is not at rest.
- **The comparison message** is sent when both sides are at rest: the ticket, each side's outcome and PR link, time taken and usage, and the buttons. **Staged** and **Skill** are offered only for a side that has a PR; **Neither** always. A reply to the message is saved as the verdict's note.
- **A side that moves on** before a verdict (the operator answered its question and it runs again) reopens the comparison. A new message is sent when both are at rest again, and a verdict is accepted only on the newest message.
- **On a verdict** the comparison is closed for good:
  - the winner's PR is marked ready for review and loses its title mark;
  - the other side ends: its PR, if any, is closed with a one-line comment that names the winner; a staged workspace goes to done from PR review and is archived from any other state through the pipeline's existing archive path; a skill run becomes `dropped`;
  - with **Neither** both PRs stay drafts and both sides end the same way, without closing anything.

  These are the only forge writes the verdict makes, and they happen only on the operator's button.
- **PR comments:** neither flow answers reviewers on a compared ticket. The staged workspace parks at PR review without offering "Review complete".
- **The tracker (confirmed by the operator):** on a compared ticket the staged flow's writes at push time (the "PR opened" comment and the move to in review) are held back, so the tracker does not point colleagues at a draft that may be closed. At the verdict Cleave makes those writes once, for the winner's PR. With **Neither** it makes none. The move to In Progress at intake stays as today.
- **The dashboard** gets a Comparison section: one row per ticket with both outcomes, both PR links and the verdict, and the tally (skill, staged, neither, undecided).

## Changes to the staged flow

Only these, all behind `ticket_flow: both`:

1. After a workspace is created, a skill run and a comparison row are created.
2. The PR is opened as a draft with the title mark (`open_pr` gains a `draft` flag in the GitHub adapter), and the tracker writes at push time are held back.
3. At PR review the workspace does not offer "Review complete" and does not process comments.
4. A verdict ends the workspace (done or archived, as above), with the tracker writes for the winner and without the "ready for merge" notice when the staged side lost.

With `ticket_flow: staged` nothing changes.

**GitHub only (the designer's call, not the operator's).** The skill flow and the comparison need token-in-environment git, draft PRs, mark-ready, retitle and close-with-comment; the reviewer subsystem has the first for GitHub only and the rest are new. `ticket_flow: both` on a repo with another forge is a config error that says so.

## Configuration (per repo)

```yaml
ticket_flow: both            # staged (default) | both
skill_flow:
  bug_skill: fix-this        # the repo's skill for bug tickets
  task_skill: implement-this # the repo's skill for every other ticket
  bug_types: [Bug, Defect]
  notes_dir: "ai_instructions/{key}"
  worktree_dir: ".claude/worktrees/{key}"
  required_files: [local.properties]
  build_env: [JAVA_HOME, ANDROID_HOME, ANDROID_SDK_ROOT]
  host_notes: |
    No emulator on this host: skip the run-the-app step and report it as not run.
  gates:
    test: "…"
    lint: "…"
    build: "…"
  gate_rounds: 2
  effort: xhigh
  timeout_seconds: 14400
```

`ticket_flow: both` with no `skill_flow` section is a config error that names the field. The mode (manual or auto) is the daemon's existing one.

## Security notes

- The session has no token and Cleave does every network write. This is stricter than today's ticket agents, which inherit the daemon's environment.
- The session is not sandboxed and loads the host profile's plugins; it can run any Bash the profile's deny list allows, as today's ticket agents can. The ticket text is untrusted input: the hard safety rules stay in the system prompt, and nothing the session writes is executed by Cleave except through the configured gate commands.
- The PR description and commit message are written by the session and published under the operator's account, as the staged flow's are today.
- The verdict's forge writes use the repo's token in Cleave's own process.

## Host prerequisites (checked before the first run)

- The skills' plugins are installed in the host's `claude` profile, and the session can call the skill by name in `-p` mode.
- The skill checkout is made once by hand (a large first clone outlasts the daemon's git timeout), holds the `required_files`, and is trusted in the GUI.
- The `build_env` variables resolve under launchd.
- A session resumed with `--resume` keeps its context across the gate and commit turns.

## Constraints

- Tests never start the real `claude` and never reach a forge or tracker: fakes, `tmp_path`, local bare repos, respx.
- Subprocesses as elsewhere: own session, process group killed on timeout or cancellation.
- Operator-facing text is English, except the skill's own relayed text.
- No employer names anywhere; examples use `acme`.

## Left to the builder

- Module and table layout of the new subsystem (the reviewer package is the model), and how much of the reviewer's clone, anchors and runner code is shared or copied.
- The exact wording of Cleave's own Telegram lines and of the host notes.
- How a comparison's "time taken and usage" is measured for the staged side.
- The dashboard section's layout.

## Follow-ups (not this project)

- Skill-only mode and the removal of the staged agents, decided from the tally.
- PR-comment answering as part of the reviewer subsystem.

## Decided with the operator (2026-11-16)

- Approach A; the staged agents may go if the skill flow proves better.
- Every ticket through both flows for a while; two draft PRs.
- Manual mode waits for "ok" before code; auto mode does not.
- No flow answers PR comments on a compared ticket.
- The staged flow's tracker writes at push time are held until the verdict and made once, for the winner's PR; none on Neither.
