---
checklist:
  id: "qa-checklist"
  name: "QA Quality Gate Checklist"
  description: "Validates all quality gates pass before merge"
---

# QA Quality Gate Checklist

- [ ] Every acceptance criterion has at least one test
- [ ] Edge cases from test-scenarios.md are covered
- [ ] New tests follow existing repo test conventions
- [ ] No existing tests were deleted or modified (unless ticket requires it)
- [ ] Full test suite passes (existing + new tests)
- [ ] Linter passes with zero errors
- [ ] Build check passes
- [ ] Test file paths mirror source file paths
- [ ] Test names are descriptive and follow naming convention
