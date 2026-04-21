"""Payload validation and transformation for POST /api/projects/create."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
VCS_PROVIDERS = {"github", "gitlab"}


@dataclass
class PayloadValidationError(Exception):
    field_errors: dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:
        return "; ".join(f"{k}: {v}" for k, v in self.field_errors.items())


def _require(errors: dict[str, str], obj: dict, key: str, path: str) -> Any:
    if key not in obj or obj[key] in ("", None):
        errors[path] = "required"
        return None
    return obj[key]


def _require_slug(errors: dict[str, str], value: Any, path: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not SLUG_RE.match(value):
        errors[path] = "must be lowercase alnum/hyphen slug starting with a letter"


def validate_payload(payload: dict[str, Any]) -> None:
    errors: dict[str, str] = {}

    identity = payload.get("identity") or {}
    pid = _require(errors, identity, "project_id", "identity.project_id")
    _require_slug(errors, pid, "identity.project_id")
    _require(errors, identity, "display_name", "identity.display_name")
    rid = _require(errors, identity, "repo_id", "identity.repo_id")
    _require_slug(errors, rid, "identity.repo_id")
    _require(errors, identity, "repo_display_name", "identity.repo_display_name")

    jira = payload.get("jira") or {}
    _require(errors, jira, "url", "jira.url")
    _require(errors, jira, "project_key", "jira.project_key")
    _require(errors, jira, "email", "jira.email")
    _require(errors, jira, "token", "jira.token")
    labels = jira.get("trigger_labels")
    if not isinstance(labels, list) or len(labels) == 0:
        errors["jira.trigger_labels"] = "must be a non-empty list"

    vcs = payload.get("vcs") or {}
    provider = vcs.get("provider")
    if provider not in VCS_PROVIDERS:
        errors["vcs.provider"] = f"must be one of {sorted(VCS_PROVIDERS)}"
    elif provider == "github":
        gh = vcs.get("github") or {}
        _require(errors, gh, "owner", "vcs.github.owner")
        _require(errors, gh, "repo", "vcs.github.repo")
        _require(errors, gh, "token", "vcs.github.token")
    elif provider == "gitlab":
        gl = vcs.get("gitlab") or {}
        _require(errors, gl, "url", "vcs.gitlab.url")
        _require(errors, gl, "project_id", "vcs.gitlab.project_id")
        _require(errors, gl, "token", "vcs.gitlab.token")

    if errors:
        raise PayloadValidationError(field_errors=errors)


def derive_env_vars(payload: dict[str, Any]) -> dict[str, str]:
    project_id = payload["identity"]["project_id"]
    prefix = project_id.upper().replace("-", "_")
    vars: dict[str, str] = {
        f"{prefix}_JIRA_TOKEN": payload["jira"]["token"],
    }
    vcs = payload["vcs"]
    provider = vcs["provider"]
    if provider == "github":
        vars[f"{prefix}_GITHUB_TOKEN"] = vcs["github"]["token"]
    else:
        vars[f"{prefix}_GITLAB_TOKEN"] = vcs["gitlab"]["token"]

    tg = (payload.get("extras") or {}).get("telegram_bot_token")
    if tg:
        vars[f"{prefix}_TELEGRAM_BOT_TOKEN"] = tg

    return vars


def redact_to_input_md(payload: dict[str, Any]) -> str:
    project_id = payload["identity"]["project_id"]
    prefix = project_id.upper().replace("-", "_")
    identity = payload["identity"]
    jira = payload["jira"]
    vcs = payload["vcs"]
    quality = payload.get("quality") or {}
    extras = payload.get("extras") or {}

    lines: list[str] = ["# Project Setup Input", "", "## Identity"]
    lines.append(f"- project_id: {identity['project_id']}")
    lines.append(f"- display_name: {identity['display_name']}")
    lines.append(f"- repo_id: {identity['repo_id']}")
    lines.append(f"- repo_display_name: {identity['repo_display_name']}")

    lines += ["", "## Jira"]
    lines.append(f"- url: {jira['url']}")
    lines.append(f"- project_key: {jira['project_key']}")
    lines.append(f"- email: {jira['email']}")
    lines.append(f"- token_var: {prefix}_JIRA_TOKEN")
    labels = jira["trigger_labels"]
    lines.append("- trigger_labels: [" + ", ".join(labels) + "]")
    repo_label = labels[-1] if len(labels) > 1 else labels[0] if labels else ""
    lines.append(f"- jira_repo_label: {repo_label}  # use this as jira_repo_label in repo YAML")
    lines.append(
        "- ignore_labels: [" + ", ".join(jira.get("ignore_labels") or []) + "]"
    )
    statuses = jira.get("statuses") or {}
    lines.append(f"- statuses: {statuses}")

    lines += ["", "## VCS"]
    provider = vcs["provider"]
    lines.append(f"- provider: {provider}")
    if provider == "github":
        gh = vcs["github"]
        lines.append(f"- owner: {gh['owner']}")
        lines.append(f"- repo: {gh['repo']}")
        lines.append(f"- token_var: {prefix}_GITHUB_TOKEN")
        lines.append(f"- default_branch: {gh.get('default_branch', 'develop')}")
        lines.append(f"- branch_prefix: {gh.get('branch_prefix', 'feature')}")
        lines.append(f"- merge_method: {gh.get('merge_method', 'squash')}")
    else:
        gl = vcs["gitlab"]
        lines.append(f"- url: {gl['url']}")
        lines.append(f"- project_id: {gl['project_id']}")
        lines.append(f"- token_var: {prefix}_GITLAB_TOKEN")
        lines.append(f"- default_branch: {gl.get('default_branch', 'develop')}")
        lines.append(f"- branch_prefix: {gl.get('branch_prefix', 'feature')}")

    lines += ["", "## Quality"]
    for key in ("lint", "test", "build"):
        q = quality.get(key)
        if q and q.get("command"):
            lines.append(f"- {key}: {{command: \"{q['command']}\", hard_gate: {q.get('hard_gate', True)}}}")

    lines += ["", "## Extras"]
    tg_var = f"{prefix}_TELEGRAM_BOT_TOKEN" if extras.get("telegram_bot_token") else None
    lines.append(f"- telegram_bot_token_var: {tg_var}")
    lines.append(f"- telegram_chat_id: {extras.get('telegram_chat_id')}")
    lines.append(f"- arch_rules_file: {extras.get('arch_rules_file')}")
    lines.append("- protected_files: [" + ", ".join(extras.get("protected_files") or []) + "]")
    lines.append(f"- max_concurrent_tickets: {extras.get('max_concurrent_tickets')}")

    return "\n".join(lines) + "\n"
