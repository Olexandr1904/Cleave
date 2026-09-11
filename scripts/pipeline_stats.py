"""Per-ticket pipeline statistics from the dashboard event store.

Read-only. Answers: how often does each ticket bounce, where, and what does
it cost? Existing data — `stage_transition` and `agent_execution_detail`
events are already persisted with no retention, so this reports on history
already on disk.

`terminal_state` is the last *driver-emitted* transition, not necessarily
where the ticket ended up: only `advance_to_stage` emits `stage_transition`.
Moves made by the dashboard, by Telegram, by the approval gate and by
escalation never emit one, so a ticket parked in BLOCKED or resumed by an
operator still reports the last stage the driver moved it to.

Usage:
    python -m scripts.pipeline_stats [path/to/events.db]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field

# Bounces are backward edges: a stage handing work back to an earlier one.
BOUNCE_EDGES = {
    ("PLAN_REVIEW", "ANALYSIS"),
    ("SCOPE_CHECK", "DEV"),
    ("CODE_REVIEW", "DEV"),
    ("QA", "DEV"),
    ("PR_REVIEW", "DEV"),
}


@dataclass
class TicketStats:
    ticket_id: str
    project_id: str = ""
    stage_entries: dict[str, int] = field(default_factory=dict)
    bounces: dict[str, int] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    duration_seconds: float = 0.0
    terminal_state: str = ""


def collect_ticket_stats(db_path: str) -> list[TicketStats]:
    """Aggregate every ticket's stage entries, bounces and spend."""
    # Read-only on purpose: a plain connect() on a mistyped path silently
    # creates an empty database and then fails with "no such table: events",
    # leaving the typo'd file behind for the next run to trip over.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT timestamp, event_type, project_id, ticket_id, data_json"
            " FROM events WHERE ticket_id IS NOT NULL ORDER BY timestamp ASC, id ASC"
        ).fetchall()
    finally:
        conn.close()

    by_ticket: dict[str, TicketStats] = {}
    entries: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    bounces: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for _ts, event_type, project_id, ticket_id, data_json in rows:
        stats = by_ticket.setdefault(ticket_id, TicketStats(ticket_id=ticket_id))
        if project_id and not stats.project_id:
            stats.project_id = project_id
        try:
            data = json.loads(data_json or "{}")
        except json.JSONDecodeError:
            continue

        if event_type == "stage_transition":
            frm, to = data.get("from_state", ""), data.get("to_state", "")
            if not to:
                continue
            entries[ticket_id][to] += 1
            stats.terminal_state = to
            if (frm, to) in BOUNCE_EDGES:
                bounces[ticket_id][f"{frm}→{to}"] += 1
        elif event_type == "agent_execution_detail":
            stats.input_tokens += int(data.get("input_tokens") or 0)
            stats.output_tokens += int(data.get("output_tokens") or 0)
            stats.duration_seconds += float(data.get("duration") or 0.0)

    for ticket_id, stats in by_ticket.items():
        stats.stage_entries = dict(entries[ticket_id])
        stats.bounces = dict(bounces[ticket_id])
    return list(by_ticket.values())


def format_table(stats: list[TicketStats]) -> str:
    """One line per ticket, widest-first columns."""
    header = f"{'TICKET':<16} {'STATE':<16} {'DEV':>4} {'BOUNCES':>8} {'TOK IN':>9} {'TOK OUT':>8} {'MINS':>6}"
    lines = [header, "-" * len(header)]
    for s in sorted(stats, key=lambda x: x.ticket_id):
        lines.append(
            f"{s.ticket_id:<16} {s.terminal_state:<16} "
            f"{s.stage_entries.get('DEV', 0):>4} {sum(s.bounces.values()):>8} "
            f"{s.input_tokens:>9} {s.output_tokens:>8} {s.duration_seconds / 60:>6.1f}"
        )
    lines.append("")
    edge_totals: dict[str, int] = defaultdict(int)
    for s in stats:
        for edge, count in s.bounces.items():
            edge_totals[edge] += count
    lines.append("Bounces by edge:")
    for edge, count in sorted(edge_totals.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {edge:<28} {count}")
    return "\n".join(lines)


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/events.db"
    print(format_table(collect_ticket_stats(db_path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
