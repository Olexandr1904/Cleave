"""The Ukrainian catalogue of what Cleave writes in Telegram (see orchestrator/tg_i18n.py).

TEXTS maps each English template, exactly as the code passes it to tr() or N_(), to its Ukrainian; NOUNS maps each
noun count() takes to its three Ukrainian forms (1, 2–4, 5 and more). One module per area; a key may be in two of
them only with the same Ukrainian. A Ukrainian template keeps its English one's placeholders, the commands and other
texts in double quotes, emoji and numbers (tests/unit/test_tg_i18n.py).
"""
from __future__ import annotations

from orchestrator.tg_uk import commands, pipeline, reviewer, skillflow

_AREAS = (reviewer, pipeline, commands, skillflow)


def _merged(name: str) -> dict:
    merged: dict = {}
    for area in _AREAS:
        for key, value in getattr(area, name).items():
            if merged.setdefault(key, value) != value:
                raise ValueError(f"orchestrator.tg_uk: {key!r} has two Ukrainian texts")
    return merged


TEXTS: dict[str, str] = _merged("TEXTS")
NOUNS: dict[str, tuple[str, str, str]] = _merged("NOUNS")
