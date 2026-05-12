import pytest

from dashboard.project_create_payload import (
    PayloadValidationError,
    derive_env_vars,
    redact_to_input_md,
    validate_payload,
)


VALID_PAYLOAD = {
    "identity": {
        "project_id": "acme",
        "display_name": "Acme Corp",
        "repo_id": "acme-app",
        "repo_display_name": "Acme App",
    },
    "jira": {
        "url": "https://acme.atlassian.net",
        "project_key": "ACME",
        "email": "bot@acme.com",
        "token": "jira-raw",
        "trigger_labels": ["ai-pipeline", "acme-app"],
        "ignore_labels": [],
        "statuses": {
            "todo": "To Do",
            "in_progress": "In Progress",
            "in_review": "In Review",
            "done": "Done",
        },
    },
    "vcs": {
        "provider": "github",
        "github": {
            "owner": "acme",
            "repo": "acme-app",
            "token": "gh-raw",
            "default_branch": "develop",
            "branch_prefix": "feature",
            "merge_method": "squash",
        },
    },
    "quality": {
        "lint":  {"command": "npm run lint",  "hard_gate": True},
        "test":  {"command": "npm test",      "hard_gate": True},
        "build": {"command": "npm run build", "hard_gate": True},
    },
    "extras": {
        "telegram_bot_token": None,
        "telegram_chat_id": None,
        "arch_rules_file": "docs/arch-rules.md",
        "protected_files": [],
        "max_concurrent_tickets": None,
    },
}


def test_validate_payload_happy_path():
    validate_payload(VALID_PAYLOAD)  # does not raise


def test_validate_payload_rejects_missing_project_id():
    p = {**VALID_PAYLOAD, "identity": {**VALID_PAYLOAD["identity"]}}
    del p["identity"]["project_id"]
    with pytest.raises(PayloadValidationError) as exc:
        validate_payload(p)
    assert "identity.project_id" in exc.value.field_errors


def test_validate_payload_rejects_bad_slug():
    p = {**VALID_PAYLOAD, "identity": {**VALID_PAYLOAD["identity"], "project_id": "Bad Slug!"}}
    with pytest.raises(PayloadValidationError) as exc:
        validate_payload(p)
    assert "identity.project_id" in exc.value.field_errors


def test_validate_payload_rejects_empty_trigger_labels():
    p = {**VALID_PAYLOAD, "jira": {**VALID_PAYLOAD["jira"], "trigger_labels": []}}
    with pytest.raises(PayloadValidationError) as exc:
        validate_payload(p)
    assert "jira.trigger_labels" in exc.value.field_errors


def test_validate_payload_rejects_bad_vcs_provider():
    p = {**VALID_PAYLOAD, "vcs": {**VALID_PAYLOAD["vcs"], "provider": "bitbucket"}}
    with pytest.raises(PayloadValidationError):
        validate_payload(p)


def test_derive_env_vars_github():
    vars = derive_env_vars(VALID_PAYLOAD)
    assert vars == {
        "ACME_JIRA_TOKEN": "jira-raw",
        "ACME_GITHUB_TOKEN": "gh-raw",
    }


def test_derive_env_vars_includes_telegram_when_provided():
    p = {**VALID_PAYLOAD}
    p["extras"] = {**VALID_PAYLOAD["extras"], "telegram_bot_token": "tg-raw"}
    vars = derive_env_vars(p)
    assert vars["ACME_TELEGRAM_BOT_TOKEN"] == "tg-raw"


def test_redact_to_input_md_contains_var_names_not_secrets():
    md = redact_to_input_md(VALID_PAYLOAD)
    assert "jira-raw" not in md
    assert "gh-raw" not in md
    assert "ACME_JIRA_TOKEN" in md
    assert "ACME_GITHUB_TOKEN" in md
    assert "trigger_labels: [ai-pipeline, acme-app]" in md


def test_redact_to_input_md_uses_tracker_label_not_jira_repo_label():
    """The schema field is `tracker_label` post-refactor; the redacted
    input.md the atlas agent reads must use the current name."""
    md = redact_to_input_md(VALID_PAYLOAD)
    assert "tracker_label:" in md
    assert "tracker_label in repo YAML" in md
    assert "jira_repo_label" not in md
