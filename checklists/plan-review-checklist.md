---
checklist:
  id: "plan-review-checklist"
  name: "Plan Review Checklist"
  description: "Validates the implementation plan itself, before any code is written"
---

# Plan Review Checklist

## Form

- [ ] Every acceptance criterion has a pass/fail condition — you can say what
      observation would prove it met
- [ ] No unverifiable language: "works correctly", "is fast", "handles errors
      gracefully", "is robust"
- [ ] The Files to Create / Files to Modify lists are present, concrete (real
      paths, not "the relevant service"), and complete — scope-guard uses this
      list as its allowlist, so a vague or empty list is an automatic FAIL
- [ ] Every task in the plan references an acceptance criterion
- [ ] Assumptions are labelled as assumptions, not stated as established fact
- [ ] Nothing in the plan contradicts rules/arch-rules.md

## Verification against the repository

- [ ] Every file under "Files to Modify" exists in the repo
- [ ] Every file under "Files to Create" does NOT already exist
- [ ] Every symbol, module, class or endpoint the plan names is findable by
      search in the repo, or is explicitly introduced by this plan
