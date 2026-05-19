# Agent Failure Summarizer — Design Spec

**Date:** 2026-05-19
**Status:** Approved

## Problem

When a pipeline agent crashes, times out, or otherwise fails to produce its expected runtime output, the operator-facing block reason is a generic string that doesn't tell them what broke or how to fix it.

Concrete symptoms observed on 2026-05-19 — three tickets blocked simultaneously, all with the same dashboard message:

> `qa agent produced no output (may have timed out or crashed). Check pipeline logs.`

The actual cause (Kotlin compiler daemon OOM + `EOFException` during `./gradlew testDebugUnitTest`, captured in `logs/qa-agent.events.log`) was never surfaced. The operator has no path from the dashboard to a concrete remediation; they have to SSH to the host and tail per-ticket logs.

The same UX gap exists on the FAILED transition path: [agent_stage.py:218](../../../orchestrator/pipeline/agent_stage.py#L218) stores `result.error` verbatim in `state.error`, which is typically a short, cryptic CLI exit message.

## Decisions

- **Add a synchronous LLM summarizer** that converts the failing stage's events log into a short, operator-facing "Reason / Suggested action" string.
- **Single reusable module** — `orchestrator/agent_failure_summarizer.py`. Two integration points: missing-output (BLOCKED) and agent_failed (FAILED).
- **Two-block output, ≤300 chars** — fits the existing dashboard chip + Telegram preview without re-templating.
- **Model: Sonnet 4.6** — sufficient for short crash-summarization, faster than Opus, consistent enough for the operator UX without changing the pipeline agents' model.
- **Graceful fallback** — on any error (timeout, API, malformed output, log missing), return `None`; callers use the existing generic message. The pipeline never blocks on summarizer health.
- **No retries, no async, no message editing** — synchronous call inside escalation; latency is acceptable because this path fires only on rare failures.
- **Cache via existing state field** — the summary is stored in `human_input_question` (BLOCKED) or `error` (FAILED) the same way today's reason is. No new caching layer.

## Public API

New module: `orchestrator/agent_failure_summarizer.py`

```python
def summarize_agent_failure(
    workspace: Workspace,
    stage_id: str,
    *,
    reason_kind: Literal["missing_output", "agent_failed"],
    raw_error: str | None = None,
) -> str | None:
    """Build a short operator-facing 'Reason / Suggested action' string by
    feeding a smart slice of the failing stage's events log to Sonnet 4.6.

    Returns the formatted string on success, or None on any failure
    (LLM error, log missing, malformed response, summarizer disabled).
    Callers fall back to their existing generic message when None is
    returned. Never raises.
    """
```

## Input — smart slice extractor

Pure helper inside the same module:

```python
def extract_log_slice(workspace, stage_id, raw_error: str | None) -> str
```

Sections, in order, separated by literal headers so the LLM can tell context apart from evidence:

1. `=== TICKET ===` — `ticket_id`, `title`, failing `stage_id`, `previous_state`, `stage_iterations[stage_id]`, `raw_error` if present. From `state.json`. ≤200 chars.
2. `=== STAGE HISTORY ===` — last 10 lines of `source/ai_pipeline/<ticket>/pipeline-log.md`. Anchors the failure in the run timeline.
3. `=== OUTPUT FILES ===` — for each known agent (`ba`, `scope-guard`, `dev`, `qa`): `<name>-agent-output.md: present|missing`. Single binary fact per file. No contents.
4. `=== CRASH SIGNALS ===` — from `logs/<stage>-agent.events.log`:
   - all `event=tool_result is_error=True` lines (last 20)
   - all `event=system` lines that aren't routine heartbeats (last 10)
   - any line matching the regex `Exception|OutOfMemory|BUILD FAILED|killed|timeout` case-insensitive (last 10)
5. `=== LOG TAIL ===` — final 50 lines of the events log verbatim.

**Hard cap: ~6 KB total.** Crash signals are never truncated; the log tail is head-truncated if the slice exceeds the cap.

**Excluded by design:**
- `ai_pipeline/<ticket>/dev-agent-output.md` and other prior-stage outputs — they describe what was being attempted, not why the current stage died. Including them invites the LLM to hallucinate failure causes from the implementation narrative.
- `meta/ticket.md` — describes the bug being fixed, not the pipeline failure. Same hallucination risk.
- Other stages' event logs.

The prompt also explicitly instructs the model to treat sections 1–3 as context only and to derive the failure cause exclusively from sections 4–5.

## Prompt

Constant in the module:

```
You are diagnosing a single pipeline-stage failure in an automated
code-fix system. Your job is to tell the human operator WHAT broke and
WHAT TO DO — in under 300 characters total.

Input below is divided into labeled sections:
- TICKET / STAGE HISTORY / OUTPUT FILES are CONTEXT ONLY.
- CRASH SIGNALS and LOG TAIL contain the actual failure evidence.
Do NOT infer failure causes from the ticket description or from prior
stage outputs. Only the failing stage's log explains the failure.

Output EXACTLY this format, no preamble, no markdown headers, no code
fences:

Reason: <one or two sentences naming the concrete failure mode
observed in the log. If the log shows no clear failure, say 'No clear
failure signal in stage log.' Do not guess.>

Suggested action: <one or two short bullet-style steps the operator
can take. Prefer concrete config/command changes over vague advice.
End with 'Retry the stage once resolved.' when retryable.>

Hard limits: max 300 chars total, no markdown, no emoji, plain text.

=== INPUT ===
{slice}
```

Call parameters: `model="claude-sonnet-4-6"`, `max_tokens=200`, `temperature=0`, timeout 30s, prompt caching enabled on the static system prefix (same pattern as commit `ccce3a6`).

## Integration points

### 1. BLOCKED — missing runtime output

In [orchestrator/escalation.py:70-71](../../../orchestrator/escalation.py#L70-L71):

```python
if candidate.exists():
    stage_output = candidate
else:
    summary = summarize_agent_failure(
        workspace, stage_id, reason_kind="missing_output"
    )
    if summary:
        return summary
    return f"{stage_id} agent produced no output (may have timed out or crashed). Check pipeline logs."
```

The summary, when present, becomes the value of `human_input_question` in state.json and the Telegram message body — same downstream flow as today.

### 2. FAILED — agent failure

In [orchestrator/pipeline/agent_stage.py:218-220](../../../orchestrator/pipeline/agent_stage.py#L218-L220):

```python
summary = summarize_agent_failure(
    workspace, stage_id, reason_kind="agent_failed",
    raw_error=result.error,
)
final_error = summary or result.error or ""
workspace.transition(Stage.FAILED)
workspace.update_state(error=final_error, raw_error=result.error)
await notify_failed_fn(workspace, final_error)
```

A new `raw_error` field is added to `WorkspaceState` to preserve the original CLI error string for forensics. The operator-facing `error` field holds the summary when one is available.

### 3. Out of scope for this change

[action_stage.py:93](../../../orchestrator/pipeline/action_stage.py#L93) follows the same pattern and is a candidate for a follow-up integration. The summarizer API supports it as-is; deferred so this change stays focused.

## Configuration

Single block in `config-live/global.yaml`:

```yaml
failure_summarizer:
  enabled: true
  model: "claude-sonnet-4-6"
  timeout_seconds: 30
```

`enabled: false` short-circuits the function to return `None` immediately — no LLM call, no logging, callers fall back to generic messages. Lets operators turn the feature off without code changes if the Anthropic API is degraded.

Defaults baked into code so missing config = enabled with the defaults above. No migration needed.

## Error handling

`summarize_agent_failure` never raises. Every failure mode returns `None` and writes a single `WARNING` log line tagged `agent_failure_summarizer` with `ticket_id`, `stage_id`, and a short error tag (so operators can grep for summarizer health):

| Failure mode | Behaviour |
|---|---|
| `enabled: false` in config | Return `None` silently (no log). |
| Events log file missing or empty | Return `None`, log `no_log`. |
| LLM call raises (network, 5xx, auth) | Return `None`, log `api_error: <type>`. |
| LLM call times out (>30s) | Return `None`, log `timeout`. |
| LLM response doesn't contain `Reason:` and `Suggested action:` | Return `None`, log `malformed`. |
| LLM response >500 chars | Return `None`, log `too_long`. |

No retries. A synchronous retry compounds the latency the operator already accepted and adds little; the fallback is a usable degraded experience.

## Testing

Three layers, all using existing pytest patterns under `tests/`:

1. **`extract_log_slice` unit tests** — pure function. Fixtures: synthetic `events.log` + `state.json` + `pipeline-log.md` in `tmp_path`. Assert:
   - section headers present in correct order
   - error lines from CRASH SIGNALS included
   - prior-stage outputs **absent**
   - 6 KB cap respected; tail truncated before crash signals when over budget
2. **`summarize_agent_failure` unit tests** — mock the Anthropic client. Verify:
   - Returns the LLM's string verbatim on a well-formed response
   - Returns `None` on timeout, API error, malformed output, response >500 chars, and `enabled: false`
   - Slice is passed to the LLM (assert via mock `messages.create` call args)
3. **Integration test in `tests/orchestrator/test_escalation.py`** — extend existing coverage of `build_blocked_reason`:
   - runtime output missing + summarizer returns string → reason equals the summary
   - runtime output missing + summarizer returns `None` → reason equals the legacy fallback string

No live LLM calls in tests. No new fixture files beyond what already exists in `tests/fixtures/`.

## Risks & non-goals

- **Latency on BLOCKED transitions.** Adds up to 30s of synchronous wait when the LLM is slow. Acceptable: this path fires only on rare crashes, and the operator-facing improvement is large.
- **Hallucination risk.** Mitigated by (a) explicit "don't guess" prompt instruction, (b) excluding prior-stage outputs from the slice, (c) `temperature=0`, (d) `malformed`/`too_long` validators rejecting suspicious output. Not eliminated. Worst case: an inaccurate summary; operator can still see the raw events log via the dashboard's per-ticket drill-down.
- **API dependence.** A new pipeline path now needs the Anthropic API to be up. Mitigated by the graceful-fallback path: failures degrade to today's behaviour, not worse.
- **Out of scope:** dashboard "summarize again" button, async/streaming summarization, summarizer applied to AWAITING_APPROVAL or DEFERRED states, `action_stage.py` integration (follow-up).
