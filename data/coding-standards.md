---
data:
  id: "coding-standards"
  name: "Coding Standards"
  description: "Default coding standards injected into dev agents"
---

# Coding Standards

## General

- Follow existing conventions in the repository — consistency over preference
- Keep functions focused — one responsibility per function
- Use descriptive variable and function names
- Add comments only where the code is not self-explanatory

## Python

- Follow PEP 8 with line length from project config (default: 100)
- Use type hints for function signatures
- Use `from __future__ import annotations` for forward references
- Prefer dataclasses for data-holding classes
- Use `logging` module, never `print()`

## Kotlin/KMP

- Follow project detekt rules
- Use `data class` for DTOs
- Prefer `sealed class` for state modeling
- Use coroutines for async operations

## Testing

- Test file mirrors source file path: `src/foo/bar.py` → `tests/unit/test_bar.py`
- Use descriptive test names: `test_{action}_{condition}_{expected}`
- One assertion per test where practical
- Mock external dependencies, not internal logic
