# Feature: QA Pipeline

**Status:** Planned
**Created:** 2026-04-07
**Updated:** 2026-04-07
**Author:** Oleksandr Brazhenko

## Description

QA Agent that writes tests covering all acceptance criteria and edge cases, then runs the full quality suite: test runner, linter, and build check. Never deletes or modifies existing tests unless the ticket explicitly requires behavior changes. Follows existing test conventions in the repo.

## Requirements

- FR1: QA Agent prompt file at `agents/qa-agent.md`
- FR2: Receives `test-scenarios.md`, current code, and `ticket.json` as context
- FR3: Writes unit tests covering all AC scenarios and edge cases from test-scenarios
- FR4: Runs test suite (`testing.run_command`), linter (`linting.run_command`), build check (`build.check_command`)
- FR5: Never deletes or modifies existing tests unless ticket explicitly requires behavior change
- FR6: New tests follow same conventions as existing tests in the repo
- FR7: If tests fail, agent attempts fix up to `max_qa_iterations`; exceeded → Telegram escalation
- FR8: Output: green test suite, new tests committed, lint and build passing

## Technical Approach

- QA Agent is a BMAD-style prompt file with test-writing instructions
- Agent reads existing test files to learn conventions before writing new tests
- Runs quality commands via subprocess in the workspace repo directory
- Iterates on failures: reads error output, fixes, re-runs up to max iterations
- All commands configurable per-repo via config (test runner, linter, build checker)

## Dependencies

- Agent System for prompt loading and execution
- Workspace Isolation for test execution environment
- Configuration Cascade for quality gate commands and thresholds
- Telegram Notifications for escalation on max iterations

## Acceptance Criteria

- [ ] QA Agent writes tests covering all acceptance criteria scenarios
- [ ] Test suite, linter, and build check all run and pass
- [ ] Existing tests are not modified unless ticket requires it
- [ ] Failed tests trigger retry up to max iterations
- [ ] Max iterations exceeded triggers Telegram escalation

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
