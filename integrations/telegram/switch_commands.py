"""Fixed Telegram commands for the running/paused switches (run before the LLM parser)."""

from __future__ import annotations

import re
from dataclasses import dataclass

_MODES = {
    "тикеты": "tickets", "тікети": "tickets", "tickets": "tickets",
    "ревью": "reviews", "рев'ю": "reviews", "рев’ю": "reviews", "ревʼю": "reviews", "reviews": "reviews",
}
_STATES = {"пауза": "paused", "pause": "paused", "старт": "running", "start": "running"}
_REPO = re.compile(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?")


@dataclass(frozen=True)
class SwitchCommand:
    kind: str  # "set" or "status"
    mode: str = ""
    state: str = ""
    repo: str = ""


def parse_switch_command(text: str) -> SwitchCommand | None:
    tokens = text.strip().lower().rstrip(".!").split()
    if len(tokens) == 1 and tokens[0] in ("статус", "status"):
        return SwitchCommand("status")
    if len(tokens) not in (2, 3):
        return None
    mode, state = _MODES.get(tokens[0]), _STATES.get(tokens[1])
    if mode is None or state is None:
        return None
    repo = tokens[2] if len(tokens) == 3 else ""
    if repo and not _REPO.fullmatch(repo):
        return None
    return SwitchCommand("set", mode, state, repo)


def resolve_scope(repo: str, scopes: list[str]) -> tuple[str | None, str]:
    wanted = repo.lower()
    exact = [s for s in scopes if s.lower() == wanted]
    if len(exact) == 1:
        return exact[0], ""
    matches = [s for s in scopes if s.split("/", 1)[1].lower() == wanted]
    if len(matches) == 1:
        return matches[0], ""
    if matches:
        return None, f'"{repo}" exists in several projects: {", ".join(matches)} — use project/repo'
    return None, f'Unknown repo "{repo}". Known: {", ".join(scopes) or "—"}'


def format_switch_status(snapshot: dict) -> str:
    lines = []
    for mode, title, empty in (("tickets", "Tickets", "no repos"), ("reviews", "Reviews", "no repos with reviews")):
        block = snapshot[mode]
        lines.append(f"{title}: {block['global']}")
        for repo in block["repos"]:
            label = repo["effective"]
            if repo["effective"] != repo["state"]:
                label += " (global)"
            lines.append(f"· {repo['scope']} — {label}")
        if not block["repos"]:
            lines.append(f"· {empty}")
    return "\n".join(lines)
