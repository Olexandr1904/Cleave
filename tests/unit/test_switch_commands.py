from integrations.telegram.switch_commands import (
    SwitchCommand, format_switch_status, parse_switch_command, resolve_scope)


def test_parse():
    assert parse_switch_command("ревью пауза") == SwitchCommand("set", "reviews", "paused", "")
    assert parse_switch_command("  Тикеты старт acme/app ") == SwitchCommand("set", "tickets", "running", "acme/app")
    assert parse_switch_command("tickets pause app.") == SwitchCommand("set", "tickets", "paused", "app")
    assert parse_switch_command("рев'ю старт") == SwitchCommand("set", "reviews", "running", "")
    assert parse_switch_command("Статус") == SwitchCommand("status")
    for other in ("пости 1 2", "ревью пауза всё сейчас", "ревью", "статус тикетов", "status ACME-1"):
        assert parse_switch_command(other) is None


def test_parse_typographic_apostrophe():
    assert parse_switch_command("рев’ю пауза") == SwitchCommand("set", "reviews", "paused", "")


def test_resolve_scope():
    scopes = ["acme/app", "acme/web", "beta/app"]
    assert resolve_scope("acme/web", scopes) == ("acme/web", "")
    assert resolve_scope("web", scopes) == ("acme/web", "")
    assert resolve_scope("app", scopes) == (
        None, '"app" exists in several projects: acme/app, beta/app — use project/repo')
    assert resolve_scope("nope", scopes) == (
        None, 'Unknown repo "nope". Known: acme/app, acme/web, beta/app')
    assert resolve_scope("nope", []) == (None, 'Unknown repo "nope". Known: —')


def test_resolve_scope_is_case_insensitive():
    assert resolve_scope("app", ["Acme/App"]) == ("Acme/App", "")
    assert resolve_scope("acme/app", ["Acme/App"]) == ("Acme/App", "")


def test_format_switch_status():
    snap = {"tickets": {"global": "paused", "repos": [
                {"scope": "acme/app", "state": "running", "effective": "paused"},
                {"scope": "acme/web", "state": "paused", "effective": "paused"}]},
            "reviews": {"global": "running", "repos": [
                {"scope": "acme/app", "state": "running", "effective": "running"}]}}
    assert format_switch_status(snap) == (
        "Tickets: paused\n· acme/app — paused (global)\n· acme/web — paused\n"
        "Reviews: running\n· acme/app — running")
    empty = {"tickets": {"global": "running", "repos": []}, "reviews": {"global": "running", "repos": []}}
    assert format_switch_status(empty) == "Tickets: running\n· no repos\nReviews: running\n· no repos with reviews"
