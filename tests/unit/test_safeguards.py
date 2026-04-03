"""Tests for orchestrator/safeguards.py."""

from __future__ import annotations

import pytest

from orchestrator.safeguards import (
    ProtectedFileViolation,
    check_protected_files,
)


class TestCheckProtectedFiles:
    def test_no_violations(self):
        changed = ["src/main.py", "tests/test_main.py"]
        violations = check_protected_files("/repo", changed)
        assert violations == []

    def test_arch_rules_violation(self):
        changed = ["arch-rules.md"]
        violations = check_protected_files("/repo", changed)
        assert len(violations) == 1
        assert violations[0].path == "arch-rules.md"

    def test_arch_rules_in_subdirectory(self):
        changed = ["docs/arch-rules.md"]
        violations = check_protected_files("/repo", changed)
        assert len(violations) == 1

    def test_detekt_config_violation(self):
        changed = [".detekt.yml"]
        violations = check_protected_files("/repo", changed)
        assert len(violations) == 1

    def test_eslint_config_violation(self):
        changed = [".eslintrc.json"]
        violations = check_protected_files("/repo", changed)
        assert len(violations) == 1

    def test_github_workflows_violation(self):
        changed = [".github/workflows/ci.yml"]
        violations = check_protected_files("/repo", changed)
        assert len(violations) == 1

    def test_multiple_violations(self):
        changed = ["arch-rules.md", ".eslintrc", "src/main.py"]
        violations = check_protected_files("/repo", changed)
        assert len(violations) == 2

    def test_extra_protected_patterns(self):
        changed = ["custom-config.toml"]
        violations = check_protected_files("/repo", changed, extra_protected=["custom-config.toml"])
        assert len(violations) == 1

    def test_empty_changed_files(self):
        violations = check_protected_files("/repo", [])
        assert violations == []

    def test_leading_slash_normalized(self):
        changed = ["/arch-rules.md"]
        violations = check_protected_files("/repo", changed)
        assert len(violations) == 1

    def test_ruff_config_violation(self):
        changed = ["ruff.toml"]
        violations = check_protected_files("/repo", changed)
        assert len(violations) == 1

    def test_jenkinsfile_violation(self):
        changed = ["Jenkinsfile"]
        violations = check_protected_files("/repo", changed)
        assert len(violations) == 1
