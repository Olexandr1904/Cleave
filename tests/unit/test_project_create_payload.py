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
    "tracker": {
        "provider": "jira",
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
    p = {**VALID_PAYLOAD, "tracker": {
        "provider": "jira",
        "jira": {**VALID_PAYLOAD["tracker"]["jira"], "trigger_labels": []},
    }}
    with pytest.raises(PayloadValidationError) as exc:
        validate_payload(p)
    assert "tracker.jira.trigger_labels" in exc.value.field_errors


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


def test_redact_to_input_md_uses_tracker_repo_label():
    """The redacted input.md uses tracker_repo_label for the label key."""
    md = redact_to_input_md(VALID_PAYLOAD)
    assert "tracker_repo_label:" in md
    assert "jira_repo_label" not in md


def _trello_payload():
    return {
        "identity": {
            "project_id": "marketing",
            "display_name": "Marketing",
            "repo_id": "main",
            "repo_display_name": "Marketing site",
        },
        "tracker": {
            "provider": "trello",
            "trello": {
                "api_key": "kkk",
                "token": "ttt",
                "board_id": "board-xyz",
                "trigger_labels": ["ai-pipeline"],
                "ignore_labels": [],
                "lists": {
                    "todo": "L1", "in_progress": "L2", "in_review": "L3", "done": "L4",
                },
            },
        },
        "vcs": {
            "provider": "github",
            "github": {"owner": "acme", "repo": "site", "token": "gh"},
        },
    }


def test_validate_trello_payload_passes():
    validate_payload(_trello_payload())


def test_validate_trello_missing_in_review_errors():
    p = _trello_payload()
    p["tracker"]["trello"]["lists"]["in_review"] = ""
    with pytest.raises(PayloadValidationError) as exc:
        validate_payload(p)
    assert "tracker.trello.lists.in_review" in exc.value.field_errors


def test_validate_unknown_provider_errors():
    p = _trello_payload()
    p["tracker"]["provider"] = "linear"
    with pytest.raises(PayloadValidationError) as exc:
        validate_payload(p)
    assert "tracker.provider" in exc.value.field_errors


def test_legacy_jira_top_level_lifted():
    p = _trello_payload()
    del p["tracker"]
    p["jira"] = {
        "url": "https://acme.atlassian.net",
        "project_key": "ACME",
        "email": "bot@acme.com",
        "token": "secret",
        "trigger_labels": ["ai-pipeline"],
    }
    validate_payload(p)
    # Provider correctly set
    assert p["tracker"]["provider"] == "jira"
    # Contents are preserved — same dict, no transformation
    assert p["tracker"]["jira"]["url"] == "https://acme.atlassian.net"
    assert p["tracker"]["jira"]["project_key"] == "ACME"
    assert p["tracker"]["jira"]["token"] == "secret"
    assert p["tracker"]["jira"]["trigger_labels"] == ["ai-pipeline"]


def test_legacy_jira_empty_dict_raises():
    """Lifting an empty `jira: {}` produces tracker.jira={} which then fails required-field validation."""
    p = _trello_payload()
    del p["tracker"]
    p["jira"] = {}
    with pytest.raises(PayloadValidationError) as exc:
        validate_payload(p)
    # The empty dict gets lifted, then the Jira required-fields check fires
    assert "tracker.jira.url" in exc.value.field_errors


def test_derive_env_vars_trello():
    vars = derive_env_vars(_trello_payload())
    assert vars["MARKETING_TRELLO_KEY"] == "kkk"
    assert vars["MARKETING_TRELLO_TOKEN"] == "ttt"
    assert vars["MARKETING_GITHUB_TOKEN"] == "gh"
    assert "MARKETING_JIRA_TOKEN" not in vars


def test_redact_trello_input_md_has_tracker_section():
    md = redact_to_input_md(_trello_payload())
    assert "## Tracker" in md
    assert "- provider: trello" in md
    assert "- board_id: board-xyz" in md
    assert "kkk" not in md
    assert "ttt" not in md
