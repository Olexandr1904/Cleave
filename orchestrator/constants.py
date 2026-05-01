"""Shared constants for the Cleave pipeline."""

# Agent structured report files — written BY the agent via tool calls.
# Use these to check whether a stage's work product exists (e.g. smart retry).
REPORT_BA = "ba.md"
REPORT_BA_QUESTIONS = "ba-questions.md"
REPORT_DEV = "developer.md"
REPORT_SCOPE_GUARD = "scope-guard.md"
REPORT_QA = "qa.md"

# Runtime output files — written by agent_runtime after executing each agent.
# These exist whenever the agent ran, regardless of whether it succeeded.
# Use these for outcome detection, stage verification, and blocked-reason display.
RUNTIME_OUTPUT_BA = "ba-agent-output.md"
RUNTIME_OUTPUT_DEV = "dev-agent-output.md"
RUNTIME_OUTPUT_SCOPE_GUARD = "scope-guard-agent-output.md"
RUNTIME_OUTPUT_QA = "qa-agent-output.md"

# Maps workflow stage_id → agent structured report.
# Use for smart retry (confirms stage work product exists, not just that agent ran).
STAGE_REPORT_FILE: dict[str, str] = {
    "analysis": REPORT_BA,
    "dev": REPORT_DEV,
    "scope_check": REPORT_SCOPE_GUARD,
    "qa": REPORT_QA,
}

# Maps workflow stage_id → runtime output file.
# Use for outcome detection, stage verification, and blocked-reason display.
STAGE_RUNTIME_OUTPUT: dict[str, str] = {
    "analysis": RUNTIME_OUTPUT_BA,
    "dev": RUNTIME_OUTPUT_DEV,
    "scope_check": RUNTIME_OUTPUT_SCOPE_GUARD,
    "qa": RUNTIME_OUTPUT_QA,
}
