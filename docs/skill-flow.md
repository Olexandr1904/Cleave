# Skill flow

With `ticket_flow: both` on a repo, every ticket the ticket pipeline takes for that repo is also worked by one `claude` session that runs the repo's own skill. The run ends as a draft pull request titled `ACME-1: <ticket title> [skill]`, opened next to the pipeline's own PR. You steer the session from Telegram: its questions and stops come to you, and your reply goes back to the same session.

The pipeline's six agents are "the staged flow"; this one is "the skill flow". Cleave is the conveyor around the skill: it copies the ticket in, relays the session's messages, runs its own gates, pushes, and opens the PR. How to fix or implement is the skill's: Cleave's prompts name the skill, its arguments and facts about the host, nothing else.

The two flows of a ticket are then compared: the staged flow's PR is a draft too, titled `… [staged]`, and once both sides are at rest you pick the better one in Telegram. Your verdict makes the forge and tracker writes. See [The comparison](#the-comparison).

Before the first run, do the one-time host steps in [deploy/README.md](../deploy/README.md#skill-flow-host-prerequisites).

---

## When a run is created

A run is created when the staged flow creates a workspace for a ticket of a repo with `ticket_flow: both`. No run is created when:

- the workspace resumes a PR the ticket already has (it starts at PR review);
- the ticket already has a run that has not ended: one in any state but `dropped` or `failed` (a `pr_open` or `no_change` run counts as not ended).

The run takes a copy of the workspace's ticket files (ticket, parent, comments, attachments) as they are at that moment, before the staged agents add their own; symbolic links are left out. Both flows start from the same input. The run's comparison with the staged workspace is stored with it; when that cannot be stored, the run fails (`comparison not stored: …`) and the ticket is not compared.

- **Skill:** `bug_skill` when the ticket's issue type is one of `bug_types` (any case), else `task_skill`. Only the Jira adapter fills in the issue type: a ticket from another tracker always gets `task_skill`.
- **Model:** the one the ticket's workspace resolved to, so both flows work with the same model.
- **Chat:** the project's Telegram chat (`telegram.default_chat_id` in `project.yaml`), else the global one.

The skill flow runs only when the daemon starts with it on. The startup output says which:

```
  Skill flow: 1 repo(s)
  Skill flow: off (dry-run)
  Skill flow: off (no Telegram)
```

It is off in a dry run and without a Telegram bot token: a run nobody could be told about is never created. Nothing is printed when no repo has `ticket_flow: both`.

A repo with `both` that the daemon cannot serve is skipped at start: its tickets get no runs (the staged flow works them as usual), and runs it had from an earlier start wait until it is served again. It is skipped for:

- no GitHub token: a warning in the log, `Skill flow on for acme/app, but it has no forge token; not run`;
- no Telegram chat (neither its project's `telegram.default_chat_id` nor the global one): a startup line, `Skill flow: off for acme/app (no Telegram chat)`, and a warning;
- a repo id another project uses too: a warning.

Changes to `ticket_flow` and `skill_flow` take effect at the next restart, and a repo added while the daemon runs (by the dashboard's wizard, say) is served after a restart; until then its tickets get no runs.

## Configuration

In the repo's YAML (`config-live/projects/<project>/repos/<repo>.yaml`). The section is checked only when `ticket_flow` is `both` and the repo is enabled; a bad value stops the daemon at start with an error that names the field.

```yaml
ticket_flow: both            # staged (default) | both
skill_flow:
  bug_skill: fix-this
  task_skill: implement-this
  bug_types: [Bug, Defect]
  notes_dir: "ai_instructions/{key}"
  worktree_dir: ".claude/worktrees/{key}"
  required_files: [local.properties]
  build_env: [JAVA_HOME, ANDROID_HOME, ANDROID_SDK_ROOT]
  host_notes: |
    No emulator on this host: skip the run-the-app step and report it as not run.
  gates:
    test: "./gradlew test"
    lint: "./gradlew detekt"
    build: "./gradlew assembleDebug"
  gate_rounds: 2
  effort: xhigh
  timeout_seconds: 14400
```

| Field | Default | Meaning |
|---|---|---|
| `ticket_flow` | `staged` | `staged`: only the staged flow. `both`: the skill flow too, and the comparison of the two. `both` works on GitHub repos only and needs a `skill_flow` section. |
| `bug_skill`, `task_skill` | — | The skill names the session calls with the Skill tool. A letter or digit, then letters, digits, `_`, `:` or `-` (`plugin:skill` works). |
| `bug_types` | `[Bug]` | The issue types that get `bug_skill`. A list. |
| `notes_dir` | — | The skill's notes directory for a ticket, relative to the checkout. Must hold `{key}` (the ticket key) and no other placeholder, no `..`. Cleave writes the ticket's files to `<notes_dir>/ticket/` and reads the PR description from `<notes_dir>/PR_DESCRIPTION.md`. |
| `worktree_dir` | — | The ticket's worktree, relative to the checkout, as the skill makes it. Same rules as `notes_dir`, and apart from it: the same directory as `notes_dir`, or one inside the other, is refused (in any case of letters), since the ticket's copy would land in the worktree and be published, or the worktree could not be made. |
| `required_files` | `[]` | Files that must exist in the checkout before a run (a local build properties file, say). Relative, no `..`. |
| `build_env` | `[]` | Environment variables the session and the gates get from the daemon. Each must be set when the daemon runs. A variable Cleave's config reads a secret from (any `${VAR}` in any string of the global, project or repo config, not only a string's first: `https://${GH_USER}:${GH_TOKEN}@…` names both), or one that makes `claude` bill an API key, is refused. |
| `host_notes` | `""` | Free text added as one more host note to the session's first prompt. |
| `gates` | `{}` | Cleave's gate commands: any of `test`, `lint`, `build`. Empty: the staged flow's `testing.run_command`, `linting.run_command` and `build.check_command`. |
| `gate_rounds` | `2` | How many times a failing gate goes back to the session for a fix. `0`: none. |
| `effort` | `""` | Passed to `claude --effort`. Empty: the CLI's default. |
| `timeout_seconds` | `14400` | The wall-clock limit of one session turn. A turn over it stops the run (`session failed`). |

The base branch is the repo's `vcs.default_branch`. The mode is the daemon's, read when a run is prepared for its first turn. In auto mode Cleave passes the skill its `unattended` argument; in manual mode it does not, and adds a note that the final message of each turn is relayed to you and your reply comes back. Whether the skill stops before code, and waits for your Approve, is the skill's own behaviour in that mode.

## How a run goes

The runner takes one run at a time, the oldest first, for every repo on `both`. A queued run starts only when:

- tickets are running both globally and for the repo (`tickets pause` or `tickets pause app` holds the repo's runs; a run already working goes on until it parks);
- the `claude` login and the repo's GitHub token are not failing their credential checks;
- no usage-limit or login wait is on.

At start the runner waits for the first round of credential checks (two minutes at most). A session turn and a gate command hold the pipeline's build slot (`defaults.max_parallel_builds`, shared with the staged flow's dev and QA stages) while they run, and release it between turns. A session turn holds it for as long as the turn runs, up to `timeout_seconds`. Meanwhile a staged workspace that reaches a build stage waits for the slot, and because the staged loop waits for all its workspaces before its next poll cycle, the whole cycle stands still until the turn ends: no new tickets are taken, and no workspace of any repo goes on to its next step. With `max_parallel_builds: 1` (the default) that can last up to `timeout_seconds` (four hours by default), and the two flows of a ticket build one after the other, so a ticket on `both` takes up to twice as long. The two levers: `max_parallel_builds: 2`, so a staged build stage gets a slot of its own, or a lower `skill_flow.timeout_seconds`.

**Prepare.** Cleave fetches every branch from GitHub, moves the checkout to `origin/<base>` (detached, never forced: local changes that the move would overwrite stop the run; other local changes and untracked files are carried along), checks `required_files` and `build_env`, writes the ticket's files to `<notes_dir>/ticket/` (an earlier copy there is replaced), and reads the host facts: the other branches and open PRs that carry the ticket key, with the staged flow's own branch left out. A branch name or PR title carries the key as a whole key, in any case: not right after a letter or digit and not followed by a digit, so `ACME-1` is in `feature/ACME-1-login`, `acme-1-fix` and `(ACME-1)`, but not in `XACME-1-login` or `ACME-10`.

**The session.** The first prompt names the skill and its arguments (`ACME-1`, or `ACME-1 unattended` in auto mode) and gives the host notes: where the ticket is, that `origin/<base>` is current, that the session has no network credentials, the assignee and status at intake, the other branches and PRs, and `host_notes`. In manual mode it also says that the final message of every turn goes to you and your reply comes back.

**A turn's end.** Cleave reads the first line of the session's final message:

- a status line for this ticket, `ACME-1 | <outcome> | … | needs user: yes/no` (markdown around it or around a field is fine: backticks, bold, italics, strikethrough):
  - an outcome whose first word is `stopped`, whatever markup is around that word (`_stopped_: …`, `**stopped:** …`, `[_stopped_] …`), is the skill's stop: the run parks as `stopped`. `stoppedX`, `stopped2`, `unstopped` or `not stopped` are not;
  - any other outcome with a change in the worktree goes on to the commit, its gates and the draft PR; with `needs user: yes` you get a note and the run goes on without waiting;
  - any other outcome with no change ends the run as `no_change`; nothing at `worktree_dir` counts as no change, and the message then says `No worktree at <path>.`;
- anything else is a question: the run parks as `waiting`.

### States

| State | Meaning |
|---|---|
| `queued` | Waiting for the runner: new, approved or replied to. |
| `running` | The prepare, a session turn (the commit turn included) or the commit check is going on; or the push and the PR, when Approve continues a run at its push step. |
| `gates` | Cleave's gates and their fix turns are running, and then the push and the opening of the PR straight after them. |
| `waiting` | The session asked you something. Parked. |
| `stopped` | The skill stopped, or Cleave stopped the run (below). Parked. |
| `interrupted` | The daemon restarted while the run was `running` or in its `gates`. Parked. |
| `deferred` | A usage limit or a failing `claude` login. Starts again by itself. |
| `no_change` | The skill finished with nothing to change. Final, unless you reply. |
| `pr_open` | The draft PR is open. Final. |
| `failed` | An error in Cleave itself. Final. |
| `dropped` | You dropped it, or a comparison verdict other than skill ended it (`comparison verdict: staged`). Final. |

A run moves through four steps: work, commit, gates, push. It parks in the step it stopped at, and Approve continues it from there.

### What parks a run

- **The session asks** (`waiting`) or **the skill stops** (`stopped`).
- **Cleave stops it** (`stopped`), with the reason in the message. The common reasons:
  - the checkout is missing, is a shallow clone, could not be fetched, or could not move to `origin/<base>`;
  - the courier cannot be used (`the courier at … cannot be used: …`), or something Cleave did not make sits at its path (`Cleave did not make …: remove it by hand`);
  - the ticket's files could not be written (`<notes_dir>/ticket in the skill checkout is a symlink: …`, `the ticket could not be written to …`);
  - `required file missing in the skill checkout: …`;
  - `build variable … is not set in the daemon's environment`;
  - `host facts not read: …`;
  - `session failed: …`, which includes a turn over `timeout_seconds`;
  - `commit check failed: …` (see below);
  - `gates still failing after 2 rounds: test, lint`, with the end of each failing gate's output;
  - `the branch kept moving: …`, when the branch moved again each time it was gated again (see below);
  - `push failed: …` and `pull request not opened: …`;
  - `deferred 3 times (usage limit or claude login)`;
  - a git read in the worktree that failed (`worktree state not read: …`, `changed files not read: …`, `commit not read: …`).
- **A restart** (`interrupted`). The next start parks every run that was `running` or in its `gates` and sends you Approve and Drop for it. On shutdown the daemon gives its tasks 5 s and then kills a session or gate still running.
- **A usage limit or a failing `claude` login** (`deferred`): until the reset time the CLI gives (an hour when it gives none), or 15 minutes for the login. While the wait is on, no run starts. The third deferral in a row stops the run instead.

After a turn that did not end normally (a limit, a failed login, a crash, a restart), what the session gets next depends on where it was cut:

- a later work turn that was cut (the session had already ended a turn): the next message says the previous turn was cut short, then carries your reply, the approval or "Continue where you stopped.";
- a cut first turn with a reply since: the same, resuming the session;
- a cut first turn with nothing since: only "Continue where you stopped.", resuming the session;
- a cut commit turn: the commit request is sent again, with the cut-short line in front;
- a cut gate-fix turn: the commit check and the gates run again first, and when a gate still fails the fix request goes out, with the cut-short line in front.

After a cut first turn, with a reply since or with nothing, when the CLI knows no session of that id (the cut came before it made one), Cleave starts a fresh session with the first prompt instead; the text of a reply goes into that first prompt (`The operator adds: …`).

## Telegram

### Messages

Every message starts with the ticket and the skill, `ACME-1 · skill run (fix-this)`. The skill's own text is relayed unchanged, in the language the skill writes, up to 9 000 characters; a longer text is cut, with a closing line that points to the notes directory. The session is asked to write `REPORT.md` there when the work is finished or stopped for good: in the first prompt in manual mode, and in every message that resumes its work (your reply, Approve, "Continue where you stopped."), all but the bare "Continue where you stopped." after a cut first turn. Cleave writes no `REPORT.md` and does not check that one is there. A long message goes out in several parts.

**Language.** Cleave's own lines, in a run's messages, the comparison's, the answers to your replies and taps, and the button labels, follow the project's `telegram.language` (`en` or `uk`; `global.yaml`'s when the project sets none; see [telegram.md](telegram.md#language)). An answer about no run or comparison Cleave knows follows the chat's language. What Cleave did not write is sent as it is, never translated: the skill's own text and status line, a run's outcome, and every reason (git, forge and gate output, why a run or a verdict stopped). In Ukrainian the two sides keep their names `staged` and `skill`, as in the config. The tables below give the English. Text written to GitHub and the tracker, the stored texts and the dashboard stay English.

| When | Message | Buttons |
|---|---|---|
| The session asks | its text, `Notes: <path>`, "Reply to answer, or tap Approve to say ok." | Approve, Drop |
| The skill stops | `— stopped: <condition>`, its text, the notes path, "Reply with new information to continue, or tap Drop." | Approve, Drop |
| Cleave stops the run | `— stopped: <reason>`, the details (a failing gate's output, say), "Approve tries this step again; a reply goes to the session." | Approve, Drop |
| After a restart | `— interrupted by a restart at the <step> step`, "Approve continues it; Drop ends it." | Approve, Drop |
| A result that needs you | `— <outcome>, needs you`, its text, "Cleave goes on to the commit, its gates and the draft PR." | none |
| No change | `— <outcome> (no change)`, then `No worktree at <path>.` when nothing is at `worktree_dir`, its text, "A reply resumes the session." | none |
| The PR is open | `— draft PR opened: <url>`, the status line, `Gates: test ok, lint ok, build ok`; the same words when Cleave found an open PR of the branch and used it instead of opening one | none |
| Deferred | `— deferred until 14:05 UTC (usage limit reached)`; not again when the run is deferred again straight after, before any of its turns ended normally | none |
| Failed | `— failed: <reason>` | none |
| A worktree Cleave could not remove | `— worktree left at <path>: <why>` | none |

A run parks whether or not Telegram answers. A message that cannot go out is sent again once Telegram is back, in order. A message in several parts goes on from the part that failed: the parts already out are not sent again, and Approve and Drop count on all of its parts.

### Replies and buttons

Reply to one of the run's messages (a Telegram reply, in one of the bot's allowed chats):

- **A text reply** goes to the session as your answer, or as new information. It is taken on any message of the run, Cleave's answers to you included, and the run goes back to its work step with it.
- **Approve** is the button, or a reply whose whole text is one OK word: `ok`, `okay`, `yes`, `go`, `approve`, `ок`, `окей`, `так`, `да`, `добре` (any case; one closing `.` or `!` is fine). "ok, but skip the UI tests" is a text reply.
- **Drop** is the button. The run ends as `dropped` and Cleave pushes nothing more for it. A branch it already pushed, and a PR for it, stay as they are. The answer says what was pushed:
  - `Dropped. The branch <branch> is on the forge; Cleave pushes nothing more.`: a push returned;
  - `Dropped. A push of <branch> was started and may have reached the forge; Cleave pushes nothing more.`: a push was started but did not return (git reported it failed, or a restart cut it), and it may have landed all the same;
  - `Dropped. Nothing was pushed.`: no push was started.

Approve and Drop count only on the run's newest message with buttons (any of its parts), and not while a newer one is still waiting to go out. A reply older than 15 minutes, by Telegram's time (one sent while the daemon was down, say), runs nothing. A tap has no age limit.

Once the run's comparison has a verdict (being carried out, stuck or closed), a text reply, Approve or Drop on any of the run's messages is answered `This run's comparison has its verdict; nothing was done.`: the verdict ends the run.

What each state takes:

| State | Text reply | Approve | Drop |
|---|---|---|---|
| `waiting` | resumes the session with your text | sends the session `ok` and tells it to finish as the skill's unattended mode describes | yes |
| `stopped` | resumes the session with your text | continues from the step it stopped at: Cleave's step again, or, after the skill's own stop, "Continue where you stopped." | yes |
| `interrupted` | resumes the session with your text | continues the same session at the same step | yes |
| `no_change` | resumes the session with your text | no | no |
| `queued`, `deferred` | no | no | yes |
| `running`, `gates` | no | no | no |
| `pr_open`, `failed`, `dropped` | no | no | no |

Every reply and tap gets one answer, as a reply: what was done (`ACME-1 · Approved — the session goes on.`, `ACME-1 · Got it — the session continues with your reply.`, `ACME-1 · Continuing from the gates step.`, `ACME-1 · Dropped. Nothing was pushed.`), or why nothing was (the run is working or finished, that is not its newest message, its newest has not gone out yet, the reply is too old). So a second tap on Approve is answered that the run is working, and nothing is done twice.

## What Cleave checks before it publishes

After a result with a change, Cleave asks the session to commit by the repo's convention and to write the PR description to `<notes_dir>/PR_DESCRIPTION.md` in the main checkout. Then:

1. **The commit check**, in the ticket's worktree:
   - the change is on a named branch, neither the base branch nor the staged flow's branch for the ticket;
   - that branch was not on GitHub at the prepare's fetch (`the change is on <branch>, which is already on the forge and is not this run's`): a branch someone else pushed, a colleague's that carries the key or a release branch, is never pushed to, not even after the run's own push. The one exception is the branch of the run's last push, which it pushes to again: also when that push was reported failed or was cut by a restart, since it may have landed all the same;
   - the worktree is clean: nothing modified or untracked;
   - the branch has at least one commit past `origin/<base>`;
   - the PR description file is there, is not a symbolic link (one is refused: it could lead to any file the daemon can read, and the text is published) and is not empty.

   A problem stops the run in its commit step (`commit check failed: …`); Approve asks the session for the commit again.
2. **Cleave's gates** on that commit: the `test`, `lint` and `build` commands, each through `/bin/sh -c` in the worktree, with the session's environment (no token), one hour each, judged by exit code. Then the check for tests that read source files as text instead of testing behaviour, on the files the branch changed.
3. **A failing gate** goes back to the session with its command, exit code and the end of its output, at most `gate_rounds` times. The session fixes and commits; the commit check and the gates run again. After the last round the run stops with the failing output; Approve gives the gates their rounds again.
4. **The commit that is pushed is the commit the gates passed.** Cleave records the head it read before the gates ran, and before the push runs the commit check again and compares the branch's head with it; a branch that moved since (by hand, between a stop and Approve, or by a commit made while the gates ran, a gate's own included) is gated again first. That takes the run back to its gates step: gates that fail there stop it in the gates step, and Approve gives them their rounds again. When the branch moved again each of the 2 times it was gated again, the run stops in its push step (`the branch kept moving: …`).
5. **The push** sends that commit itself, the one the gates passed, to the branch of the same name on GitHub, not the branch by name: a commit made on the branch after the compare stays in the checkout. A commit that is not on the branch's history is refused before anything reaches GitHub (`push failed: the commit … is not on <branch>`). It never forces: a branch there with other history rejects it, and the run stops with git's reason. The base branch is never pushed.
6. **The pull request**: the branch's open PR when there is one, else a new **draft** PR against the base branch, titled `ACME-1: <ticket title> [skill]`, with the description as its body (Cleave's token masked; at most 60 000 characters). GitHub takes a title of at most 256 characters: a longer ticket title is cut to fit, ending with `…` before ` [skill]`. The run ends as `pr_open`. When the push landed but the PR could not be opened, Approve pushes again (nothing new goes up) and opens it.

## The comparison

Every skill run on a repo with `ticket_flow: both` has a comparison with the staged workspace it was made for. When both sides are at rest, Cleave sends one Telegram message that shows both and asks which is better. Your tap is the verdict, and the verdict makes the forge and tracker writes. A ticket with no skill run (see [When a run is created](#when-a-run-is-created)) has no comparison and is worked by the staged flow as on any other repo.

### The staged flow on a compared ticket

Only a ticket that has a comparison is affected. For it the staged flow changes in three places:

- **The PR is a draft**, titled `ACME-1: <ticket title> [staged]`, cut to GitHub's 256 characters as the skill flow's title is.
- **No tracker writes at push time.** The move to In Review and the `PR opened: <url>` comment are held: the verdict makes them for the winning PR.
- **PR review answers no comments and offers no "Review Complete".** The PR message says `Draft PR opened: <url>`, that Cleave answers no review comments on it, and that it makes the tracker writes once you pick the better PR. The workspace then stays in PR review until the verdict.

### At rest

A side is at rest when nothing more happens on it without you:

| Side | At rest in |
|---|---|
| Staged | `PR_REVIEW`, `BLOCKED`, `FAILED`, `DONE`, `ARCHIVED`; also a workspace that is gone (removed, or its state cannot be read), shown as `gone` with no PR |
| Skill | `pr_open`, `stopped`, `no_change`, `failed`, `dropped`; also a run that is not found (`gone`) |

A skill run that is `waiting`, `interrupted` or `deferred` is not at rest: it waits for you or goes on by itself.

Cleave's comparison keeper looks at every open comparison every 30 seconds, and at once after a tap. It runs in its own task, so the build slot a skill turn holds does not hold it back, and it starts with the daemon, without waiting for the first credential round: it calls no `claude`.

### The comparison message

```
ACME-1 · comparison: Fix login
Staged: PR #12 https://github.com/acme-org/acme-app/pull/12 · PR_REVIEW · 2 h 10 min · 1200000 in / 40000 out tokens
Skill (fix-this): no PR · no_change: no app fix needed · 1 h 05 min · 800000 in / 30000 out tokens
Tap the better side. A reply to this message is kept as the verdict's note.
```

Each side shows its PR and its state, or, for a side without a PR, `no PR`, its state and its outcome when it has one (the skill run's outcome or reason, the staged workspace's error); then the time from intake until it came to rest; and the tokens it used: the session's usage for the skill flow, the sum the staged agents logged for the staged flow.

| Button | Offered |
|---|---|
| Staged | when the staged side has a PR |
| Skill | when the skill side has a PR |
| Neither | always |

A tap is taken only:

- on the comparison's newest message, any of its parts (`That is not the comparison's newest message; nothing was done. Use the newest one.`), and not while a newer one is still waiting to go out (`The comparison's newest message has not gone out yet; nothing was done. Wait for it.`);
- in the comparison's own chat: from any other chat the answer is `This comparison is not known; nothing was done.`;
- while the comparison is open: a second tap is answered `The verdict is being carried out; nothing was done.`, one while the verdict is stuck `The verdict (skill) is stuck; nothing was done. The Retry button on its newest message continues it.`, and one after the verdict `The comparison has its verdict (skill); nothing was done.`;
- for a side that has a PR (`That side has no PR; nothing was done.`). Neither is always taken.

A tap has no age limit. A tap that is taken is answered with what follows: `ACME-1 · Verdict: skill. Cleave marks PR #13 ready and makes the tracker writes for it, and closes PR #12.`, or `ACME-1 · Verdict: neither. Both PRs stay drafts; both sides end.`

### A side that moves on

You can still work either side while the message stands: reply to the skill run's stop, answer the staged flow's escalation. When a side leaves rest, the standing message's buttons are refused from then on, and once both sides are at rest again a new comparison message comes. When both stay at rest but one of them changed (a run that stopped again for another reason, say), the new message comes at once.

When a side moved on after your tap but before the verdict's first step, the verdict is undone and nothing is written: `ACME-1 · comparison: the skill side moved on after your verdict; nothing was done. A new message comes when both sides are at rest.`

### The note

A reply to any of the comparison's messages is kept as the verdict's note, shown on the dashboard. It is taken at any age, in any state of the comparison, an OK word too, and changes nothing else. A second reply is added to the note. The answer is `ACME-1 · Kept as the comparison's note.`

### What a verdict does

The verdict runs in steps, in this order. Each step is recorded once it is done, so a Retry or a restart goes on from the step that did not finish. The winner is the side you tapped; the loser is the other side.

1. **Ready.** The winner's PR is marked ready for review.
2. **Title.** The ` [skill]` or ` [staged]` mark is taken off the end of the winner's title.
3. **Comment.** When the loser's PR is still open, it gets the comment `Closed by Cleave: the comparison of the two flows chose the skill flow's PR #13.` A PR already closed gets none.
4. **Close.** The loser's PR is closed, when it is still open. A side without a PR has nothing to close.
5. **Tracker.** The writes the staged flow held, for the winner's PR: the move to In Review and the `PR opened: <url>` comment. They are made once: a failed write is a line in the closing message (`Tracker: move to In Review failed: TimeoutError`), never a stuck verdict, and it is never tried again. Make that write by hand. A project with no tracker gets the line `Tracker: none configured.`
6. **Skill run.** When the skill side wins, its run stays `pr_open` and is released: its worktree is removed. A winning run that is not `pr_open` (one that failed after its PR was opened, say) is left as it is, worktree and all: `Skill run: failed; left as it is.` Otherwise the run ends as `dropped` (`comparison verdict: staged`) and its worktree is removed as a dropped run's. A run you set working again just before the verdict (`running`, `gates`) makes this step wait until the run parks: that wait is not a stuck verdict.
7. **Staged workspace.** It ends, whichever side wins: a workspace in PR review goes to `DONE`, one in `BLOCKED` or `FAILED` is archived (`BLOCKED` through `FAILED`), one already `DONE` or `ARCHIVED` stays as it is. The line is `Staged workspace: <state>`. A workspace that is gone is the line `Staged workspace: not found.` One that moved on (`DEFERRED`, or working again) is left as it is, never archived under you, and the line names the error's class (`Staged workspace: InvalidTransitionError`).

The staged flow's own end of a ticket does not run: its `PR ready for merge` notice is never sent, and the verdict's closing message takes its place:

```
ACME-1 · verdict: skill
Ready for review: PR #13 https://github.com/acme-org/acme-app/pull/13
Closed: PR #12
Tracker: the "PR opened" comment and the move to review are made.
Skill run: kept; its worktree is removed.
Staged workspace: DONE
```

**Neither** makes no forge and no tracker write: both PRs stay open as drafts (`Both PRs stay drafts.`), the skill run is dropped and the staged workspace ends, as above.

Removing a worktree at the verdict follows the rule for a dropped run's: never forced, only when no other run of the ticket that has not ended works there; when git refuses, the worktree stays and you get the `worktree left at` line. The staged workspace's directory is not removed. The age cleanup deletes only `ARCHIVED` workspaces, so one the verdict ended as `DONE` stays on disk until you archive it from the dashboard.

### A stuck verdict and Retry

A forge write that fails (a GitHub error, a timeout, a refusal) stops the verdict on its step, as `stuck`:

```
ACME-1 · verdict skill not finished: commenting on PR #12 failed: GitHub POST /repos/acme-org/acme-app/issues/12/comments → 502: bad gateway
Retry continues from that step.
```

with a Retry button. The reason is GitHub's text as it is, with the token masked, cut to 300 characters. Retry is taken only on that newest message, in the comparison's chat, and goes on from the failed step: no step that finished runs again. An error in Cleave itself stops the verdict the same way, with the error's class as the reason. The answer to Retry is `ACME-1 · Retrying the verdict from the step that failed.` Retry is the only way on from a stuck verdict: when a step keeps failing (a winner PR you closed by hand, say), fix its cause on GitHub first.

A restart in the middle of a verdict does not stop it: the next start goes on from its next step. Only the step in flight at the restart can run again. Marking ready, the title and closing are safe to repeat; the loser comment and the tracker writes can then be made twice. The loser comment can also be posted twice without a restart: it goes through the GitHub adapter's retries, so a timeout after GitHub stored the comment posts it again.

### Turning it off

`ticket_flow` is read at start. Set a repo back to `staged`, or start the daemon without the repo served, only when every comparison of the repo has its verdict:

- a comparison of a repo that is not served waits as it is, and is carried on once the repo is served again;
- a staged workspace whose repo is no longer on `both` is not compared any more: it goes back to the staged flow's own PR review, on its draft `[staged]` PR, with the tracker writes still not made, and its review comments are answered;
- when the daemon serves no skill repo at all (every `both` repo skipped at start, a dry run, or no Telegram), no ticket is compared: the staged workspace of a repo still on `both` goes back to the staged flow's own PR review in the same way, and only the comparison itself waits.

### The dashboard

The **Comparison** section (`#/comparison`) shows the tally at the top: Skill, Staged, Neither and Undecided. A comparison counts for its verdict from your tap on, also while the verdict is carried out or stuck; Undecided counts the open ones. Below it, one row per comparison, the newest first: the ticket and its title, each side (state, PR, time, tokens), the verdict (or the state while it is open, being carried out or stuck, with the reason when stuck) and the note. An open comparison's sides are read live; once it has a verdict, the row shows the sides as you decided on them. The section shows the newest 200 comparisons; the tally counts every comparison. A side that cannot be read is shown as `gone`. With the toolbar's auto-refresh on (the default) it refreshes every 30 seconds; else use Refresh. It is read-only: the verdict is a Telegram tap. On a daemon that serves no skill repo the section has no data behind it and shows `No comparisons yet.`, even when the database holds comparisons from an earlier start.

## Where things live on the host

Everything is under the directory of `reviewer.db`: `~/CleaveData/reviewer/` by default, or the directory of `reviewer.db_path` when that is set.

```
~/CleaveData/reviewer/
  reviewer.db                          the runs (table skill_runs), the comparisons (comparisons) and the
                                       Telegram messages of both (skill_anchors)
  skill/
    repos/acme/app/                    the skill checkout, made by hand
      ai_instructions/ACME-1/          notes_dir: ticket/, the skill's notes, REPORT.md, PR_DESCRIPTION.md
      .claude/worktrees/ACME-1/        worktree_dir: the ticket's worktree, made by the skill
    repos/acme/app.courier.git/        Cleave's courier
    runs/<run id>/
      ticket/                          the ticket's files as they were at intake
      session/settings.json            the deny rules the session runs with
      session/progress.log             every turn's progress lines
      session/stream-<n>.jsonl         the CLI's raw output of turn n
```

The courier is a small bare repository of Cleave's own, next to the checkout. It borrows the checkout's objects and is made anew once per prepare and once per push. Both go through it, so the git calls that carry the token never read the checkout's own `.git/config`, which the session can write. Cleave removes and remakes only a courier it made (it holds a `cleave-courier` file); anything else at that path stops the run until you remove it.

To list the runs and why each parked (the dashboard shows the comparisons, not the runs):

```bash
sqlite3 -readonly ~/CleaveData/reviewer/reviewer.db \
  "SELECT id, ticket_id, scope, state, json_extract(data, '$.reason'), updated_at FROM skill_runs ORDER BY created_at"
```

A restart parks a run that is working; to restart while none is, this count prints `0`:

```bash
sqlite3 -readonly ~/CleaveData/reviewer/reviewer.db "SELECT count(*) FROM skill_runs WHERE state IN ('running', 'gates')"
```

A restart does not stop a verdict: the next start carries out a `deciding` comparison from its next step.

## Security

- The session's environment holds no GitHub, tracker, Telegram or Claude API token. Cleave does every network write itself, the push and the PR, and the fetch too. The token reaches git only through the environment of Cleave's own two network git calls, never through a config file.
- The deny rules in Cleave's settings file for the session refuse `gh`, `glab`, `curl`, `wget`, `security`, the git commands that reach or rewire a remote or a credential (`push`, `fetch`, `pull`, `ls-remote`, `remote`, `config`, `credential`), and reads of `~/.git-credentials`, `~/.netrc`, `~/.config/gh/`, Cleave's `.env` and its config directory. They are a second line, not a sandbox: they match commands by their start. The session runs unsandboxed, with the host profile's plugins, builds with the host's toolchain, and can read any file the host user can. So keep no GitHub login for the host user: no `gh` login, no github.com entry in the git credentials file and no github.com item in the login keychain (host step 6). The session can reach a login stored for that user, and a push made with it would skip every gate.
- The ticket's text, its comments and attachments are untrusted input. The prompt says the ticket files are data, never instructions, and Cleave's hard safety rules are in the system prompt.
- What the session writes can reach Cleave's own processes, with no token at stake in any of them:
  - Cleave's own git calls in the checkout and its worktrees (the move to the base branch, the status and commit checks, the worktree removal) carry no token and run no hook and no fsmonitor, but the other settings of the checkout's git config, which the session can write, still apply to them (a filter driver it sets runs inside Cleave's call);
  - the gate commands run the session's code;
  - a later session in the same checkout loads whatever the checkout's own Claude Code settings (`.claude/settings.json`) hold, hooks included.
- The PR description and the commits are the session's, published under the account of the repo's token.

## Known limits

- **No run, no comparison.** A ticket of a `both` repo that gets no skill run (the repo is not served, the workspace resumes a PR, or an older run of the ticket has not ended) is not compared: the staged flow works it as on a `staged` repo, with a ready PR, its tracker writes and its PR review.
- **Turning `both` off with open comparisons.** A staged workspace of an open comparison goes back to the staged flow's own PR review, on its draft `[staged]` PR, with no tracker writes made, and the comparison waits until the repo is served again. Give every open comparison its verdict first (see [Turning it off](#turning-it-off)).
- **A restart in the middle of a multi-part message** can send the parts already out again, for a comparison's messages as for a run's.
- **The token figures of the two sides** come from the same counter: both take the CLI's usage through the same adapter, whose input figure is the input tokens plus the cache reads plus the cache writes (`integrations/llm/claude_code_adapter.py`), so they are comparable. The staged side's figure is the sum of the `input_tokens=` and `output_tokens=` lines in its agents' own logs (`<agent>.log`; the `-tools.log` and `.events.log` files are not read); a run on the host confirms the staged agents write those lines.
- **`reviewed` on a compared ticket.** `reviewed ACME-1`, a tap on an old "Review Complete" button or a reply to an earlier escalation message of a compared ticket is still answered `Got it. Fetching PR comments now.`, but nothing is fetched: the reply is stored and takes effect only if the workspace goes back to the staged flow's own PR review (`both` turned off).
- **A staged workspace taken up again while its verdict was stuck** (you re-ran it from the dashboard, say) stays running after the verdict, whose line reads `Staged workspace: InvalidTransitionError`. While the verdict is stuck it stays compared: its review comments are not answered and it offers no "Review Complete". Once the verdict closes, it goes back to the staged flow's own behaviour, as on a `staged` repo: a ready PR, its tracker writes and its PR review. The same holds for a workspace re-run after its verdict closed. Archive it from the dashboard if you do not want that.
- **A `both` repo skipped at start while another repo is served.** Its open comparisons' staged workspaces stay parked as compared until the repo is served again; only a warning at start says so.
- **Worktrees.** Only the worktree of a dropped run, or of the skill run a verdict chose (released), is removed, never forced, and only when no other run of the ticket that has not ended works there; when git refuses (changes, untracked files) it stays and you get a line. The worktree of a run that ends any other way stays in the checkout until you remove it (`git -C <checkout> worktree remove <path>`).
- **A dropped or released run's local branch stays in the checkout.** Removing the worktree does not delete its branch.
- **Other open PRs.** The open PRs that carry the ticket key, listed in the host notes, are read from one page of 100 open PRs.
- **Switched off.** When the daemon serves no repo (none on `both`, every `both` repo skipped at start, or a dry run) but Telegram is on, a reply to an old skill-run or comparison message, and a tap on one of their buttons, is answered `The skill flow is off on this daemon; nothing was done.` in the chat's language, and is never read as a command. This holds once the daemon's database has any skill-run or comparison message; a daemon that never sent one reads every reply as before.
