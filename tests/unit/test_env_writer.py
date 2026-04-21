import os
import stat
from pathlib import Path

import pytest

from dashboard.env_writer import (
    EnvCollisionError,
    append_vars,
    read_existing_vars,
    remove_vars,
)


def test_read_existing_vars_parses_simple_assignments(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("FOO=bar\nBAZ=qux\n", encoding="utf-8")
    assert read_existing_vars(env_path) == {"FOO", "BAZ"}


def test_read_existing_vars_ignores_comments_and_blanks(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("# comment\n\nFOO=bar\n", encoding="utf-8")
    assert read_existing_vars(env_path) == {"FOO"}


def test_read_existing_vars_missing_file_returns_empty(tmp_path: Path):
    assert read_existing_vars(tmp_path / ".env") == set()


def test_append_vars_atomic_write_and_chmod(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING=1\n", encoding="utf-8")
    append_vars(env_path, {"ACME_JIRA_TOKEN": "abc", "ACME_GITHUB_TOKEN": "def"})
    content = env_path.read_text(encoding="utf-8")
    assert "EXISTING=1" in content
    assert "ACME_JIRA_TOKEN=abc" in content
    assert "ACME_GITHUB_TOKEN=def" in content
    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode == 0o600


def test_append_vars_creates_file_if_missing(tmp_path: Path):
    env_path = tmp_path / ".env"
    append_vars(env_path, {"X": "y"})
    assert env_path.read_text(encoding="utf-8") == "export X=y\n"


def test_append_vars_raises_on_collision(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("ACME_JIRA_TOKEN=old\n", encoding="utf-8")
    with pytest.raises(EnvCollisionError) as exc:
        append_vars(env_path, {"ACME_JIRA_TOKEN": "new"})
    assert exc.value.vars == ["ACME_JIRA_TOKEN"]


def test_remove_vars_deletes_lines(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("FOO=1\nACME_JIRA_TOKEN=abc\nBAR=2\n", encoding="utf-8")
    remove_vars(env_path, ["ACME_JIRA_TOKEN"])
    assert env_path.read_text(encoding="utf-8") == "FOO=1\nBAR=2\n"
