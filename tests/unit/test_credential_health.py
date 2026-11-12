# tests/unit/test_credential_health.py
from __future__ import annotations
import asyncio, json
from datetime import datetime, timedelta, timezone
import pytest
import respx
from config.schemas import (GitHubConfig, GlobalConfig, LoadedProject, ProjectConfig, RepoConfig,
                            TelegramConfig, VCSConfig)
from health.credentials import Credential, CredentialHealth, build_credentials, classify, fix_text
from health.validators import ValidatorResult, check_github

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

def ok():
    return ValidatorResult(True, "x", "t", "", "")

def http(code):
    return ValidatorResult(False, "x", "t", f"HTTP {code}", "", extra={"status": code})

def login(logged_in, expires=None):
    return ValidatorResult(bool(logged_in), "claude_login", "host", "", "",
                           extra={"logged_in": logged_in, "expires_at": expires, "status": None})

class Scripted:
    def __init__(self, *results):
        self.results, self.calls = list(results), 0
    async def __call__(self):
        self.calls += 1
        return self.results[min(self.calls - 1, len(self.results) - 1)]

def cred(key="forge:acme/app", kind="forge", provider="github", check=None, env_var="GH_T"):
    return Credential(key, kind, provider, "GitHub acme-org/acme-app", "acme-org/acme-app",
                      env_var, "vcs.github.token", check or Scripted(ok()))

def service(creds, lines, now=lambda: NOW, **kw):
    async def send(text):
        lines.append(text)
    return CredentialHealth(lambda: creds, send, now=now, tz=timezone.utc, **kw)

@pytest.mark.parametrize("kind,result,state", [
    ("forge", ok(), "ok"), ("forge", http(401), "invalid"), ("forge", http(403), "no_access"),
    ("forge", http(404), "no_access"), ("forge", http(502), "unreachable"), ("forge", http(429), "unreachable"),
    ("tracker", ValidatorResult(False, "jira", "K", "timed out", "", extra={"status": None}), "unreachable"),
    ("telegram", http(404), "invalid"), ("telegram", http(401), "invalid"),
    ("claude_login", login(True, "2026-12-30T00:00:00Z"), "ok"),
    ("claude_login", login(True, "2026-11-22T12:00:00Z"), "expiring"),
    ("claude_login", login(True, "2026-11-20T11:00:00Z"), "invalid"),
    ("claude_login", login(False), "invalid"), ("claude_login", login(None), "unreachable"),
])
def test_classify(kind, result, state):
    assert classify(cred(kind=kind), result, NOW)[0] == state

async def test_failing_sends_one_line_then_recovery():
    lines = []
    c = cred(check=Scripted(http(401), http(401), ok()))
    h = service([c], lines)
    await h.check_all(); await h.check_all()
    assert h.failing("forge:acme/app")
    assert lines == ["⚠️ GitHub acme-org/acme-app: invalid or expired. "
                     "Create a new token, put it in .env as GH_T, restart the daemon."]
    await h.check_all()
    assert lines[-1] == "✅ GitHub acme-org/acme-app: working again." and len(lines) == 2

async def test_no_access_line_has_sso_hint():
    lines = []
    await service([cred(check=Scripted(http(403)))], lines).check_all()
    assert lines == ["⚠️ GitHub acme-org/acme-app: no access (HTTP 403). Check the token's access to "
                     "acme-org/acme-app; a classic GitHub token may need SSO authorization for the organization."]

async def test_unreachable_is_silent_and_keeps_failing_memory():
    lines = []
    h = service([cred(check=Scripted(http(401), http(503), http(401), http(503), ok()))], lines)
    for _ in range(5):
        await h.check_all()
    assert len(lines) == 2 and lines[1].startswith("✅")

async def test_telegram_credential_never_sends():
    lines = []
    await service([cred("telegram", "telegram", "telegram", Scripted(http(401)))], lines).check_all()
    assert lines == []

async def test_login_expiry_warnings_once_each():
    lines, clock = [], [NOW]
    c = cred("claude_login", "claude_login", "claude", Scripted(login(True, "2026-11-24T12:00:00Z")), env_var="")
    h = service([c], lines, now=lambda: clock[0])
    await h.check_all()                                   # 4 days left: nothing
    clock[0] = NOW + timedelta(days=1, hours=1); await h.check_all()   # < 3 days
    clock[0] = NOW + timedelta(days=1, hours=2); await h.check_all()   # still < 3 days: nothing new
    clock[0] = NOW + timedelta(days=3, hours=1); await h.check_all()   # < 1 day
    assert lines == ["⏳ The claude login on the host expires 2026-11-24 12:00. "
                     "Log in again on the host in a GUI terminal."] * 2
    assert h.state("claude_login") == "expiring"

async def test_login_first_seen_under_one_day_sends_one_line():
    lines = []
    c = cred("claude_login", "claude_login", "claude", Scripted(login(True, "2026-11-21T00:00:00Z")), env_var="")
    await service([c], lines).check_all()
    assert len(lines) == 1

async def test_report_auth_failure_rechecks_once_within_gap():
    check = Scripted(ok(), http(401))
    h = service([cred(check=check)], [], recheck_gap_seconds=60)
    h.report_auth_failure("forge:acme/app", 401)
    for _ in range(4):
        h.report_auth_failure("forge:acme/app", 401)
    await asyncio.sleep(0.05)
    assert check.calls == 1
    h.report_auth_failure("forge:acme/app", 401)        # checked < 60 s ago
    await asyncio.sleep(0.05)
    assert check.calls == 1

async def test_report_unknown_key_is_harmless():
    service([cred()], []).report_auth_failure("forge:nope/x", 401)
    await asyncio.sleep(0)

async def test_send_failure_does_not_raise():
    async def boom(_):
        raise RuntimeError("telegram down")
    h = CredentialHealth(lambda: [cred(check=Scripted(http(401)))], boom, now=lambda: NOW)
    assert (await h.check_all())[0].state == "invalid"

async def test_credentials_fn_called_per_check():
    creds = [cred()]
    h = service(creds, [])
    await h.check_all()
    creds.append(cred("tracker:beta", "tracker", "jira"))
    await h.check_all()
    assert [s.key for s in h.snapshot()] == ["forge:acme/app", "tracker:beta"]

async def test_unknown_before_first_check_is_not_failing():
    h = service([cred()], [])
    assert h.state("forge:acme/app") == "unknown" and not h.failing("forge:acme/app")

async def test_run_stops_on_event():
    check = Scripted(ok())
    h = service([cred(check=check)], [], interval_seconds=3600)
    stop = asyncio.Event()
    task = asyncio.create_task(h.run(stop))
    await asyncio.sleep(0.05); stop.set()
    await asyncio.wait_for(task, 1)
    assert check.calls == 1

def test_build_credentials_and_no_token_values():
    repo = RepoConfig(vcs=VCSConfig(provider="github", github=GitHubConfig(token="ghp_SECRET", owner="acme-org", repo="acme-app")),
                      env_refs={"vcs.github.token": "GH_T"})
    bare = RepoConfig(vcs=VCSConfig(provider="github", github=GitHubConfig(token="", owner="o", repo="r")))
    proj = LoadedProject(config=ProjectConfig(telegram=TelegramConfig(bot_token="123:TGSECRET"),
                                              env_refs={"telegram.bot_token": "ACME_TG"}),
                         repos={"app": repo, "bare": bare})
    creds = build_credentials(GlobalConfig(), {"acme": proj}, "/fake/claude")
    assert [c.key for c in creds] == ["forge:acme/app", "telegram", "claude_login"]
    assert (creds[0].label, creds[0].env_var) == ("GitHub acme-org/acme-app", "GH_T")
    assert creds[1].env_var == "ACME_TG"

async def test_snapshot_never_contains_token_values():
    repo = RepoConfig(vcs=VCSConfig(provider="github", github=GitHubConfig(token="ghp_SECRET", owner="o", repo="r")),
                      env_refs={"vcs.github.token": "GH_T"})
    creds = build_credentials(GlobalConfig(), {"acme": LoadedProject(repos={"r": repo})}, "/fake/claude")
    for c in creds:
        c.check = Scripted(http(401))
    h = service(creds, [])
    await h.check_all()
    assert "ghp_SECRET" not in json.dumps([s.to_dict() for s in h.snapshot()])


# --- Added beyond the brief: the controller's report_auth_failure guarantees ---

def test_report_without_running_loop_is_harmless():
    check = Scripted(ok())
    service([cred(check=check)], []).report_auth_failure("forge:acme/app", 401)
    assert check.calls == 0

async def test_check_all_does_not_start_a_second_check_while_one_is_in_flight():
    gate = asyncio.Event()
    calls = []
    async def slow():
        calls.append(1)
        await gate.wait()
        return http(401)
    h = service([cred(check=slow)], [])
    h.report_auth_failure("forge:acme/app", 401)
    await asyncio.sleep(0)
    round_ = asyncio.create_task(h.check_all())
    await asyncio.sleep(0.01)
    gate.set()
    assert (await round_)[0].state == "invalid" and len(calls) == 1


# --- Fix round 1: cancelling one awaiter never cancels the shared check ---

def gated(gate, calls, result):
    async def check():
        calls.append(1)
        await gate.wait()
        return result
    return check

async def test_cancelled_check_one_does_not_stop_run():
    gate, calls = asyncio.Event(), []
    h = service([cred(check=gated(gate, calls, http(401)))], [], interval_seconds=3600)
    stop = asyncio.Event()
    runner = asyncio.create_task(h.run(stop))
    await asyncio.sleep(0.01)                             # round in flight
    caller = asyncio.create_task(h.check_one("forge:acme/app"))
    await asyncio.sleep(0.01)
    caller.cancel()
    await asyncio.sleep(0.01)
    assert caller.cancelled() and not runner.done()
    gate.set()
    await asyncio.sleep(0.01)
    assert h.state("forge:acme/app") == "invalid" and len(calls) == 1 and not runner.done()
    stop.set()
    await asyncio.wait_for(runner, 1)

async def test_cancelled_check_all_does_not_break_concurrent_round():
    gate, calls = asyncio.Event(), []
    h = service([cred(check=gated(gate, calls, ok()))], [])
    round_ = asyncio.create_task(h.check_all())
    await asyncio.sleep(0.01)
    refresh = asyncio.create_task(h.check_all())
    await asyncio.sleep(0.01)
    refresh.cancel()
    await asyncio.sleep(0.01)
    gate.set()
    assert (await asyncio.wait_for(round_, 1))[0].state == "ok" and len(calls) == 1
    assert refresh.cancelled()

async def test_run_continues_after_credentials_fn_raises(caplog):
    check, attempts = Scripted(ok()), []
    def creds():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("config reloading")
        return [cred(check=check)]
    h = CredentialHealth(creds, None, now=lambda: NOW, interval_seconds=0.01)
    stop = asyncio.Event()
    runner = asyncio.create_task(h.run(stop))
    for _ in range(100):
        if check.calls:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(runner, 1)
    assert check.calls >= 1 and "Credential health round failed" in caplog.text

async def test_cancelling_run_ends_it_promptly():
    gate, calls = asyncio.Event(), []
    h = service([cred(check=gated(gate, calls, ok()))], [], interval_seconds=3600)
    runner = asyncio.create_task(h.run(asyncio.Event()))
    await asyncio.sleep(0.01)                             # round in flight, stop never set
    runner.cancel()
    await asyncio.wait([runner], timeout=1)
    assert runner.cancelled()
    gate.set()                                            # the shielded check still applies
    await asyncio.sleep(0.01)
    assert h.state("forge:acme/app") == "ok"


async def test_missing_claude_binary_gets_path_fix():
    missing = ValidatorResult(False, "claude_login", "host", "claude not found", "",
                              extra={"logged_in": None, "expires_at": None, "status": None})
    c = cred(key="claude_login", kind="claude_login", provider="claude", check=Scripted(missing))
    st = await service([c], []).check_one("claude_login")
    assert st.state == "unreachable"
    assert st.fix == "Add the directory with claude (/opt/homebrew/bin) to the daemon's PATH and restart it."


@pytest.mark.parametrize("kind,provider,env_var,state,reason,fix", [
    ("forge", "github", "GH_T", "invalid", "",
     "Create a new token, put it in .env as GH_T, restart the daemon."),
    ("forge", "github", "", "invalid", "",
     "Create a new token, set it in vcs.github.token, restart the daemon."),
    ("forge", "github", "GH_T", "no_access", "",
     "Check the token's access to acme-org/acme-app; "
     "a classic GitHub token may need SSO authorization for the organization."),
    ("tracker", "jira", "JIRA_T", "no_access", "", "Check the token's access to acme-org/acme-app."),
    ("claude_login", "claude", "", "invalid", "", "Log in again on the host in a GUI terminal."),
    ("claude_login", "claude", "", "expiring", "", "Log in again on the host in a GUI terminal."),
    ("claude_login", "claude", "", "unreachable", "claude not found",
     "Add the directory with claude (/opt/homebrew/bin) to the daemon's PATH and restart it."),
    ("forge", "github", "GH_T", "unreachable", "HTTP 502",
     "Network or service unavailable; next check in 15 min."),
])
def test_fix_text(kind, provider, env_var, state, reason, fix):
    assert fix_text(cred(kind=kind, provider=provider, env_var=env_var), state, reason) == fix


# --- Code-review fix round, batch B ---

class _Py310Datetime(datetime):
    """datetime as Python 3.10 parses ISO strings: a trailing Z is a ValueError."""
    @classmethod
    def fromisoformat(cls, value):
        if value.endswith("Z"):
            raise ValueError(f"Invalid isoformat string: {value!r}")
        return datetime.fromisoformat(value)

def test_parse_time_turns_trailing_z_into_utc(monkeypatch):
    from health import credentials
    monkeypatch.setattr(credentials, "datetime", _Py310Datetime)
    parsed = credentials._parse_time("2026-11-25T10:00:00.000Z")
    assert parsed == datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)
    assert parsed.utcoffset() == timedelta(0)

@pytest.mark.parametrize("headers,body,state", [
    ({"x-ratelimit-remaining": "0"}, {"message": "Forbidden"}, "unreachable"),
    ({}, {"message": "API rate limit exceeded for user ID 1."}, "unreachable"),
    ({"x-ratelimit-remaining": "4999"}, {"message": "Resource not accessible by integration"}, "no_access"),
])
@respx.mock
async def test_github_rate_limit_403_is_unreachable(headers, body, state):
    respx.get("https://api.github.com/repos/acme-org/acme-app").respond(403, headers=headers, json=body)
    result = await check_github("t", "acme-org", "acme-app")
    assert result.extra["status"] == (429 if state == "unreachable" else 403)
    assert classify(cred(), result, NOW)[0] == state
