---
checklist:
  id: "merge-checklist"
  name: "Merge Gate Checklist"
  description: "Final verification before merging a PR"
---

# Merge Gate Checklist

- [ ] Scope certificate exists in workspace context
- [ ] All PR review comments are resolved
- [ ] CI tests are passing
- [ ] CI lint check is passing
- [ ] CI build check is passing
- [ ] No merge conflicts (or only in non-plan files, resolved to base)
- [ ] PR uses configured merge method
- [ ] Jira ticket transitioned to Done
- [ ] Jira comment posted with PR URL
- [ ] Telegram success notification sent
