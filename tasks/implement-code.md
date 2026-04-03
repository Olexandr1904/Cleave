---
task:
  id: "implement-code"
  name: "Implement Code from Plan"
  description: "Step-by-step code implementation following the implementation plan"
---

# Implement Code Task

## Steps

1. Read the implementation plan completely before writing any code
2. Create the feature branch if not already created: `{branch_prefix}/{ticket_id}-{slug}`
3. For each file in "Files to Create":
   - Create the file at the specified path
   - Implement the logic described in the plan
   - Follow existing code conventions in the repository
4. For each file in "Files to Modify":
   - Read the current file content
   - Apply only the changes described in the plan
   - Preserve existing functionality
5. Verify no files outside the plan were touched
6. Commit with format: `feat({ticket_id}): {description}`
7. If scope violations were reported, read `scope-report.md` and fix only the violations
