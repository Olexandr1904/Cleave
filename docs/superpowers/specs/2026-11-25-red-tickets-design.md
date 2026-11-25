# Pipeline: red tickets on a busy host — design

Date: 2026-11-25. Small change. The operator asked why the board's tickets were red and approved three fixes ("да,
делай все три"). Four tickets had stopped: one at push (the repo's pre-push detekt found what the QA lint gate did
not run), one at dev (its session killed while a build it ran in the background was still running), two at QA (a
session past its 4-hour wall clock, and gates past their 1-hour timeout) while a self-hosted CI runner on the same
host built back to back for 20 hours.

## Decisions

1. **The lint gate runs what the pre-push hook runs.** A repo config change, not code: the gate's command starts with
   the hook's `./gradlew detekt` and the gate is hard, so QA sends the work back to dev instead of the push blocking.
   Checked first: clean `develop` passes it.
2. **A background task keeps the session.** The adapter reads the stream's `background_tasks_changed` (the full list
   of the session's running background tasks). After a `result`, with a task running, the post-result kill waits
   `BACKGROUND_TASK_GRACE_SECONDS` (an hour, as long as a quality gate may run) instead of 10 minutes; the wall
   clock still bounds the session.
3. **Out of time is transient.** A session killed at its wall clock is deferred (30 minutes) like a transient CLI
   error, not failed. A hard gate that timed out, and only that (every gate that could not run timed out), defers the
   ticket 30 minutes without counting the attempt; any other reason a gate could not run still blocks. The two count
   as one streak (`<stage>: out of time`: on a busy host they alternate, and two keys would never repeat) and stop
   the ticket the second time in a row (each costs hours: a session fails, a gate blocks). At QA the streak survives
   the agent's successful run and ends when the gates run; a block or failure for another reason clears it.
4. **The background-task wait never costs the result.** Within two minutes of the session's wall clock, a session
   waiting on a background task is killed gracefully with its cached result, as the 10-minute grace would have done;
   the wall clock's own kill throws the result away.

## Approaches

- Lint: run the hook's command in the gate (chosen); or send a push the hook refuses back to dev (catches any hook,
  but the work waits a full QA round to learn it, and the hook's output is a push error). Not chosen.
- Background task: wait for it (chosen); or forbid background runs to the agents (the CLI and the prompts would both
  need it, and a model that cannot background a 40-minute build blocks the session's turn instead). Not chosen.
- Out of time: defer (chosen); or a longer budget (a busy host can outlast any budget). Not chosen.
