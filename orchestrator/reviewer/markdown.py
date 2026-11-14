"""Fenced code blocks in Markdown: a line inside one is never a heading, a section or an item."""
from __future__ import annotations

import re

_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)$")


def fenced(lines: list[str]) -> list[bool]:
    """Whether each line (without its line ending) belongs to a fenced code block, its fence lines included.
    A block opens with ``` or ~~~ (up to three spaces in) and closes with a line of the same character, at
    least as long, with nothing after it."""
    flags: list[bool] = []
    fence = ""
    for line in lines:
        m = _FENCE.match(line)
        if fence:
            flags.append(True)
            if m and m[1][0] == fence[0] and len(m[1]) >= len(fence) and not m[2].strip():
                fence = ""
        else:
            flags.append(m is not None)
            fence = m[1] if m else ""
    return flags
