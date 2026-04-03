---
checklist:
  id: "dev-scope-checklist"
  name: "Developer Scope Checklist"
  description: "Ensures developer stays within implementation plan scope"
---

# Developer Scope Checklist

- [ ] Only files listed in implementation plan were created or modified
- [ ] No architecture rules files were modified
- [ ] No lint configuration files were modified
- [ ] No CI/CD configuration files were modified
- [ ] No external dependencies added beyond what the plan specifies
- [ ] No bonus refactoring outside ticket scope
- [ ] Existing tests not deleted or modified (unless ticket requires it)
- [ ] All commits include ticket ID in the format: `feat({ticket_id}): {description}`
- [ ] Code follows existing repository conventions
- [ ] Feature branch name follows format: `{prefix}/{ticket_id}-{slug}`
