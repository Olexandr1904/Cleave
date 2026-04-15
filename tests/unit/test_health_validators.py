from __future__ import annotations

from health.validators import ValidatorResult


def test_validator_result_ok_shape():
    r = ValidatorResult(ok=True, name="jira", target="ACME", reason="", fix_hint="")
    assert r.ok is True
    assert r.name == "jira"
    assert r.target == "ACME"
    assert r.reason == ""
    assert r.fix_hint == ""


def test_validator_result_failure_shape():
    r = ValidatorResult(
        ok=False,
        name="git_identity",
        target="/tmp/ws",
        reason="user.email not set",
        fix_hint="git config --global user.email <you@company>",
    )
    assert r.ok is False
    assert "user.email" in r.reason
    assert "git config" in r.fix_hint
