"""Markdown lines as CommonMark reads them: where a line ends, and which lines are in a fenced code block (a line
inside one is never a heading, a section or an item)."""
from __future__ import annotations

import re

# The line breaks str.splitlines() honours and CommonMark does not (\v, \f, \x1c-\x1e, \x85, U+2028, U+2029):
# tools that split lines one way or the other read different lines in a text that holds one.
AMBIGUOUS_BREAKS = frozenset(map(chr, (0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029)))
BOM = chr(0xFEFF)  # CommonMark skips one at the start of the input

_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)$")
_LINE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+")


def split_lines(text: str, keepends: bool = False) -> list[str]:
    """`text`'s lines, split at \\r\\n, \\r or \\n only, as CommonMark splits them. str.splitlines() also
    breaks at U+2028, U+2029, \\v, \\f, \\x1c-\\x1e and \\x85, which GitHub reads as text. As with
    splitlines(), a line break at the end opens no line, and `keepends` keeps each line's ending."""
    lines = _LINE.findall(text)
    return lines if keepends else [line.rstrip("\r\n") for line in lines]


def fenced(lines: list[str]) -> list[bool]:
    """Whether each line (without its line ending) belongs to a fenced code block, its fence lines included.
    A block opens with ``` or ~~~ (up to three spaces in; a ``` line whose info string holds a backtick opens
    none, as in CommonMark) and closes with a line of the same character, at least as long, with only spaces
    or tabs after it."""
    flags: list[bool] = []
    fence = ""
    for line in lines:
        m = _FENCE.match(line)
        if fence:
            flags.append(True)
            if m and m[1][0] == fence[0] and len(m[1]) >= len(fence) and not m[2].strip(" \t"):
                fence = ""
        else:
            opens = m is not None and not (m[1][0] == "`" and "`" in m[2])
            flags.append(opens)
            fence = m[1] if opens else ""
    return flags


def strip_fences(text: str) -> str:
    """`text` stripped; when it then starts with a fence line (```, ```json), the lines inside that first fenced
    block, up to its closing fence or the end, else the stripped text as it is. For a model's answer that
    wraps its JSON in a code block."""
    text = text.strip()
    lines = split_lines(text)
    if not (lines and fenced(lines[:1])[0]):
        return text
    fence = _FENCE.match(lines[0])[1]
    inside: list[str] = []
    for line in lines[1:]:
        m = _FENCE.match(line)
        if m and m[1][0] == fence[0] and len(m[1]) >= len(fence) and not m[2].strip(" \t"):
            break
        inside.append(line)
    return "\n".join(inside)
