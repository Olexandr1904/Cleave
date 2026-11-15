"""Cleave's changes to a review report's text: the ticks of a post, the Summary left out, an edited or a drafted
comment spliced in and checked by parsing the result again; and the checks every posted text passes.

The report and the bodies are model output: they are scanned line by line, never evaluated."""
from __future__ import annotations

import dataclasses
import html
import os
import re
import shutil
import tempfile
from bisect import bisect_right
from collections import defaultdict, deque
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NamedTuple

from orchestrator.reviewer.markdown import AMBIGUOUS_BREAKS, BOM, fenced, split_lines
from orchestrator.reviewer.report import (
    _ITEM,
    Report,
    ReportError,
    ReportItem,
    _number,
    parse_report,
)

_STRUCTURE = "the new text changes the report's structure"
_ODD_CHARACTER = "The text holds a character that tools read differently: rewrite it. Nothing was done."
_NOT_CLEAVES = "the job copy's posting directory is not Cleave's"
_MAX_TITLE = 200
_QUESTIONS = "## ❓ Questions"
_AFTER_QUESTIONS = ("🟣", "Dropped", "✅", "Summary")  # a new ❓ section goes before the first of these
_IMAGE = re.compile(r"!\[|<img", re.IGNORECASE)  # "![" covers reference-style images too
# A bare URL runs as GitHub's autolink (cmark-gfm) runs: to ASCII whitespace or "<", past ( ) [ ] " ' and backticks;
# _autolink then cuts off what GitHub leaves out of the link.
_URL = re.compile(
    r"(?:https?|ftp):[/\\]*[^\t-\r <]+"  # any run of slashes or backslashes, or none; GitHub links ftp too
    r"|www\.[^\t-\r <]*"
    r"|(?<![:\w])[/\\]{2}(?![/\\])[^\t-\r <]+",  # protocol-relative
    re.IGNORECASE)
_AUTOLINK_TRAILING = frozenset("?!.,:*_~'\"")  # never the last character of an autolink
_SHOWN = re.compile(r"[^>()\[\]\"'`]*")  # the part of a refused bare URL a problem names, when it is refused itself
_DOT_SEGMENT = re.compile(r"[/\\]\.{1,2}(?:[/\\?#]|$)")  # walks out of an allowed prefix
_MENTION = re.compile(r"(?<![A-Za-z0-9])@([A-Za-z0-9][A-Za-z0-9-]{0,38})")  # only ASCII: "é@x" mentions
# An e-mail address GitHub links: after mailto: or xmpp: (to whitespace or "<", as a bare URL), or bare as
# user@host.tld. It starts only where a run of address characters starts (linear).
_EMAIL = re.compile(
    r"(?:mailto|xmpp):[^\t-\r <]+"
    r"|(?<![A-Za-z0-9.+_-])[A-Za-z0-9.+_-]+@[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+",
    re.IGNORECASE)
# A reference GitHub links to another repo: owner/repo#N (an issue or PR), owner/repo@sha (a commit) or owner@sha (a
# commit in that owner's fork). It starts only where a run of owner characters starts (linear), its leading "-"
# left out of the owner.
_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9-])-*([A-Za-z0-9][A-Za-z0-9-]*)"
    r"(?:/[A-Za-z0-9._-]+(?:#[0-9]+|@[0-9a-fA-F]{7,40})|@[0-9a-fA-F]{7,40}(?![0-9A-Za-z_]))")
_FENCE_INFO = re.compile(r" {0,3}(?:`{3,}|~{3,})(.*)")  # a fence line and its info string
_GITHUB = "https://github.com/"
_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")  # a backslash escape: the ASCII punctuation it stands for
# Where a link destination starts: after an inline link's or image's "](", a reference definition's "]:" (a
# footnote label's too), an href or src attribute's "=". In an autolink it is all between "<" and ">".
_LINK_OPEN = re.compile(r"\]\(|\]:")
_ATTRIBUTE = re.compile(r"(?:href|src)[\t\n\f\r ]*=[\t\n\f\r ]*", re.IGNORECASE)
_AUTOLINK = re.compile(r"<([^\x00-\x20<>]*)>")
_SPACES = re.compile(r"[\x00-\x20]*")  # skipped before a Markdown destination: any run, line breaks too
_POINTY = re.compile(r"<((?:\\.|[^<>\\\n\r])*)>")  # a destination in angle brackets, on one line
_PLAIN = re.compile(r"(?:\\[!-/:-@\[-`{-~]|\\|[^\x00-\x20\x7f()\\])+")  # destination characters but "(" and ")"
_UNQUOTED = re.compile(r"[^\t\n\f\r >]*")  # an unquoted attribute value, as HTML ends it
_TAB_CR_LF = str.maketrans("", "", "\t\r\n")  # browsers delete them anywhere in a URL
_C0_AND_SPACE = "".join(map(chr, range(0x21)))  # ... and strip these from both ends
# A "<" that may open a tag, a comment or an HTML block: a letter, "/", "!" or "?" after it. It is looked for
# anywhere in a line, since a block may open after any container's marker (a footnote label among them).
#
# Deliberately strict, and possibly temporary (operator, 2026-11-15). One such "<" anywhere outside a trusted fence
# makes the whole text checked as plain text, inline code spans included, because inside raw HTML a backtick does
# not start code and GitHub would render the mention or link. The cost is false refusals on ordinary Kotlin and
# Java findings: a generic such as `Provider<Foo>` looks like a tag, so a `@Inject` or a fenced `@Composable` in the
# same comment is then reported as "mentions @Inject" and the operator has to "edit N". Revisit this once real
# reviews show how often it happens. A narrower rule (trust code spans unless the "<" sits outside any code span,
# or starts a line) must keep refusing the known bypasses in tests/unit/test_reviewer_report_edit.py
# (BODY_EDGES: html-block-hides-fence, details-no-blank, footnote-html-block, tag-across-lines): an inline tag
# whose quoted attribute holds a backtick defeats the obvious narrowing.
_HTML_START = re.compile(r"<[A-Za-z/!?]")
# A run of 3+ backticks or tildes after nothing but spaces, tabs, ">" and list markers ("-", "*", "+", or 1-9
# digits and "." or ")", each followed by a space or a tab).
_FENCE_LOOKING = re.compile(r"(?:[ \t>]|[-*+][ \t]|[0-9]{1,9}[.)][ \t])*(?:`{3,}|~{3,})")
_TICKS = re.compile(r"`+")


class _Line(NamedTuple):
    text: str  # without its line ending
    whole: str  # with it
    fenced: bool  # in a fenced code block


def tick(text: str, numbers: set[int]) -> str:
    """Every item heading outside a fence reads `[x]` when its number is in `numbers`, else `[ ]`; no other line
    changes."""
    out = []
    for line in _lines(text):
        whole = line.whole
        if not line.fenced and (m := _ITEM.match(line.text)):
            whole = f"### [{'x' if _number(m[2]) in numbers else ' '}]" + whole[len("### [ ]"):]
        out.append(whole)
    return "".join(out)


def drop_summary(text: str) -> str:
    """`text` without the `## Summary` heading and its lines up to the next `## ` heading outside a fence. A
    heading is the Summary's as parse_report reads it (its text starts with "Summary"); every one goes."""
    kept, in_summary = [], False
    for line in _lines(text):
        if _is_section(line):
            in_summary = line.text[3:].startswith("Summary")
        if not in_summary:
            kept.append(line)
    return _join(kept)


def apply_edit(text: str, head: str, number: int, body: str, drafted: frozenset[int]) -> str:
    """Item `number`'s lines after its first `**File:**` line (after its heading when it has none) up to its
    last non-blank line replaced by `body`. The result must parse to the same report, with only that item's
    body changed, to its kept lines and `body`."""
    if _refused_character(body) is not None:
        raise ReportError(_ODD_CHARACTER)
    old = parse_report(text, head, drafted)
    lines = _lines(text)
    start = next((i for i, line in enumerate(lines)
                  if not line.fenced and (m := _ITEM.match(line.text)) and _number(m[2]) == number), None)
    if start is None:
        raise ReportError(f"item {number} is not in the report")
    end = next((i for i in range(start + 1, len(lines)) if _ends_item(lines[i])), len(lines))
    file_at = next((i for i in range(start + 1, end) if lines[i].text.startswith("**File:**")), start)
    last = max((i for i in range(file_at + 1, end) if lines[i].text.strip()), default=file_at)
    new = _through(lines, file_at + 1) + body + "\n" + _join(lines[last + 1:])
    want = "\n".join([*(line.text for line in lines[start + 1:file_at + 1]), body]).strip()
    items = tuple(dataclasses.replace(i, body=want) if i.number == number else i for i in old.items)
    if _parse_spliced(new, head, drafted) != dataclasses.replace(old, items=items):
        raise ReportError(_STRUCTURE)
    return new


def apply_draft(text: str, head: str, number: int, title: str, path: str, line: int | None, body: str,
                drafted: frozenset[int]) -> str:
    """A ❓ item for the dropped `number` appended at the end of the `## ❓ Questions` section, which is made
    when absent. The result must parse to the same report with that one item more."""
    if any(_refused_character(part) is not None for part in (title, path, body)):
        raise ReportError(_ODD_CHARACTER)
    if not (_one_line(title) and len(title) <= _MAX_TITLE and _one_line(path) and "`" not in path):
        raise ReportError("draft unreadable")
    old = parse_report(text, head, drafted)
    if number not in {d.number for d in old.dropped}:
        raise ReportError(f"#{number} is not a dropped number")
    file_line = f"**File:** `{path if line is None else f'{path}:{line}'}`"
    item = f"### [ ] {number}. {title}\n{file_line}\n{body}\n\n---\n"
    lines = _lines(text)
    sections = [i for i, heading in enumerate(lines) if _is_section(heading)]
    questions = next((i for i in sections if lines[i].text.startswith("## ❓")), None)
    if questions is not None:
        at = next((i for i in sections if i > questions), len(lines))
    else:  # parse_report found a Summary, so there is a section to go before
        at = next(i for i in sections if lines[i].text[3:].startswith(_AFTER_QUESTIONS))
        item = f"{_QUESTIONS}\n\n{item}"
    new = _through(lines, at) + item + _join(lines[at:])
    parsed = _parse_spliced(new, head, drafted | {number})
    added = ReportItem(number, "question", title.strip(), path, line, "", f"{file_line}\n{body}".strip())
    mine = [i for i in parsed.items if i.number == number]
    rest = tuple(i for i in parsed.items if i.number != number)
    if mine != [added] or dataclasses.replace(parsed, items=rest) != old:
        raise ReportError(_STRUCTURE)
    return new


def item_text(item: ReportItem) -> str:
    """The item as the operator reads it: number, place and note, the title, and the body without its first
    `**File:**` line."""
    where = item.path if item.line is None else f"{item.path}:{item.line}"
    note = f" ({item.note})" if item.note else ""
    before, after = split_at_file_line(item.body)
    return f"#{item.number} · {where}{note}\n**{item.title}**\n\n" + "\n".join(before + after).strip()


def split_at_file_line(body: str) -> tuple[list[str], list[str]]:
    """An item body's lines before its first `**File:**` line and after it; without one, all of them come after (an
    edit replaces those)."""
    lines = body.split("\n")
    file_at = next((i for i, line in enumerate(lines) if line.startswith("**File:**")), None)
    return ([], lines) if file_at is None else (lines[:file_at], lines[file_at + 1:])


def body_problems(text: str, allowed: tuple[str, ...]) -> list[str]:
    """What keeps `text` from being posted: an image, a link that starts with none of the lowercase `allowed`
    prefixes (or leaves one by a dot segment), an e-mail address (GitHub links it, bare or after mailto: or
    xmpp:), a cross-repo reference to an owner other than the allowed GitHub one, an @-mention. It fails closed: code is skipped only where GitHub surely renders it as code, and
    everything else is checked as text. Each problem is listed once.

    - Lines end where CommonMark ends them (`split_lines`). A fenced block is skipped only when it opens at
      column 0 and is no math block (`_sure_code`). No code at all is skipped when the text outside those holds
      a `<` that may open a tag (`_HTML_START`), wherever it sits: it may start an HTML block, where GitHub
      reads no Markdown.
    - Inline code spans are skipped line by line (`_code_spans`), but a $`…`$ span is math. After a line that
      leaves a backtick run unpaired, holds a `<` outside its code spans or a bare URL that runs into one, the
      paragraph's later lines are checked whole: a span or a tag (its quotes may hold a `>`) may pair across
      lines.
    - Every link destination must be an allowed absolute URL (`_destinations`): relative paths, anchors and
      other schemes are refused. A bare URL is checked where it stands, line by line, as far as GitHub's
      autolink takes it (`_autolink`).
    """
    lines = split_lines(text.removeprefix(BOM))
    code = _sure_code(lines)
    in_html = any(_HTML_START.search(line) for line, in_code in zip(lines, code) if not in_code)
    problems: dict[str, None] = {}
    left_open = False
    whole: list[str] = []  # the lines checked, with their code spans
    checked: list[str] = []  # ... and without the spans skipped
    for line, in_code in zip(lines, code):
        if in_code and not in_html:
            continue
        whole.append(line)
        if not line.strip():
            left_open = False  # a blank line ends the paragraph
            checked.append(line)
            continue
        if in_html or left_open:
            clean = line
        else:
            clean, sure = _code_spans(line)
            left_open = not sure or "<" in clean
        checked.append(clean)
        _note_problems(clean, allowed, problems)
    for raw, cut in _destinations("\n".join(whole), "\n".join(checked)):
        if (shown := _foreign_destination(raw, cut, allowed)) is not None:
            problems[f"links to {shown[:80]}"] = None
    return list(problems)


def ensure_base(job_dir: Path, report_name: str) -> Path:
    """`job_dir/posting/report.base.md`: the report Cleave edits and ticks copies of, made once from the job's
    `report_name` (kept verbatim beside it as `report.original.md`), unticked. Both files are written into a
    new temp directory renamed to `posting`: they appear together or not at all."""
    posting = job_dir / "posting"
    base = posting / "report.base.md"
    # Cleave makes posting/ and the base together; the archive never copies a "posting" from the run directory.
    if posting.is_symlink() or (posting.exists() and not (posting.is_dir() and base.exists())):
        raise ReportError(_NOT_CLEAVES)
    if posting.exists():
        return base
    data = (job_dir / report_name).read_bytes()  # read once: the original and the base are these bytes
    text = data.decode("utf-8", errors="replace")  # as the runner read it, line endings kept
    if (found := _refused_character(text)) is not None:  # named by code point: the raw character is invisible
        raise ReportError(
            f"the job's report holds a character that tools read differently (U+{ord(found):04X})")
    spare = Path(tempfile.mkdtemp(dir=job_dir, prefix=".posting."))  # mode 0700
    try:
        with open(spare / "report.original.md", "xb") as f:
            f.write(data)
        with open(spare / "report.base.md", "xb") as f:
            f.write(tick(text, set()).encode("utf-8"))
        os.rename(spare, posting)
    except BaseException:
        shutil.rmtree(spare, ignore_errors=True)
        raise
    return base


def write_atomic(path: Path, text: str) -> None:
    """`text` (UTF-8, its line endings as they are) written to a new temp file beside `path`, then renamed over
    it."""
    fd, spare = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
        os.replace(spare, path)
    except BaseException:
        Path(spare).unlink(missing_ok=True)
        raise


def _lines(text: str) -> list[_Line]:
    """`text`'s lines, split where parse_report splits them."""
    bare = split_lines(text)
    return [_Line(*line) for line in zip(bare, split_lines(text, keepends=True), fenced(bare))]


def _join(lines: list[_Line]) -> str:
    return "".join(line.whole for line in lines)


def _through(lines: list[_Line], k: int) -> str:
    """The first `k` lines, ending with a line break."""
    return _join(lines[:k]) + ("\n" if k and lines[k - 1].text == lines[k - 1].whole else "")


def _is_section(line: _Line) -> bool:
    return not line.fenced and line.text.startswith("## ")


def _ends_item(line: _Line) -> bool:
    """A line that ends the item before it, as parse_report reads items: a section, a heading or `---`."""
    return not line.fenced and (line.text.startswith(("## ", "### ")) or line.text.strip() == "---")


def _parse_spliced(text: str, head: str, drafted: frozenset[int]) -> Report:
    try:
        return parse_report(text, head, drafted)
    except ReportError as e:
        raise ReportError(_STRUCTURE) from e


def _refused_character(text: str) -> str | None:
    """The first character in `text` that Cleave never posts, if any: one of `AMBIGUOUS_BREAKS`, or a BOM (it is
    invisible, and no review needs one)."""
    found = [at for c in (*AMBIGUOUS_BREAKS, BOM) if (at := text.find(c)) != -1]
    return text[min(found)] if found else None


def _one_line(text: str) -> bool:
    return "".join(split_lines(text)) == text  # no line break parse_report splits at


def _sure_code(lines: list[str]) -> list[bool]:
    """Per line, whether it is in a fenced block GitHub surely renders as code. Adjacent blocks are taken
    together. They count only when the first opens at column 0 while `fenced` is still in step with GitHub: a
    column-0 line ends any list item or quote, and inside a block that opens in step the two agree on every
    line. A fence-looking line that is indented or follows a list or quote marker (`_FENCE_LOOKING`) may be
    read one way by `fenced` and another by GitHub. From the first one outside a trusted block, every block is
    checked as text. Blocks taken together with a math block are never trusted: GitHub renders it with MathJax,
    whose \\href may link."""
    flags = fenced(lines)
    code = [False] * len(lines)
    in_step = True
    start = 0
    while start < len(lines):
        end = start + 1
        while flags[start] and end < len(lines) and flags[end]:
            end += 1
        if (flags[start] and in_step and lines[start][:1] in ("`", "~")
                and not any(_math(line) for line in lines[start:end])):
            code[start:end] = [True] * (end - start)
        elif any(_FENCE_LOOKING.match(line) and line[:1] not in ("`", "~") for line in lines[start:end]):
            in_step = False
        start = end
    return code


def _math(line: str) -> bool:
    """Whether `line` is a fence line that may open a math block: the first word of its info string, read as
    CommonMark reads one (entities decoded, escapes dropped), is "math". Any line of a group of blocks may be one
    (a block opens right after another closes)."""
    if not (m := _FENCE_INFO.match(line)):
        return False
    words = _ESCAPE.sub(r"\1", html.unescape(m[1])).split()
    return bool(words) and words[0].lower() == "math"


def _code_spans(line: str) -> tuple[str, bool]:
    """`line` with each inline code span replaced by a space (a run of backticks opens one, the next run of the
    same length closes it), and whether GitHub surely reads the line's backticks so. A span with a `$` right
    before or after it is math (GitHub's $`…`$), which stays as text. When GitHub might pair the backticks
    otherwise or link past them (a backslash before a run that closes no span, a `<` of an autolink or HTML tag
    before a span, a `|` of a table cell inside one, a bare URL that runs into one) the line comes back whole.
    A run left unpaired keeps the spans before it, but GitHub may pair it on a later line."""
    runs = [m.span() for m in _TICKS.finditer(line)]
    ahead: dict[int, deque[int]] = defaultdict(deque)  # the runs of each length, in order
    for r, (s, e) in enumerate(runs):
        ahead[e - s].append(r)
    spans: list[tuple[int, int]] = []
    closers: set[int] = set()
    unpaired = False
    r = 0
    while r < len(runs):  # linear: every run leaves its deque once
        same = ahead[runs[r][1] - runs[r][0]]
        while same and same[0] <= r:
            same.popleft()
        if same:
            spans.append((runs[r][0], runs[same[0]][1]))
            closers.add(same[0])
            r = same[0] + 1
        else:
            unpaired = True
            r += 1
    spans = [(s, e) for s, e in spans if line[s - 1:s] != "$" and line[e:e + 1] != "$"]  # math is text
    ends = [0, *(e for _, e in spans)]
    gaps = [line[a:b] for a, (b, _) in zip(ends, spans)]  # the text before each span
    if (any(s and line[s - 1] == "\\" and r not in closers for r, (s, _) in enumerate(runs))
            or any("<" in gap for gap in gaps) or any("|" in line[s:e] for s, e in spans)
            or _url_into_span(line, spans)):
        return line, False
    return " ".join([*gaps, line[ends[-1]:]]), not unpaired


def _url_into_span(line: str, spans: list[tuple[int, int]]) -> bool:
    """Whether a bare URL in `line` runs into one of its code spans: GitHub's autolink takes the backticks and
    what follows them into the link."""
    openers = [s for s, _ in spans]
    for m in _URL.finditer(line):
        k = bisect_right(openers, m.start())
        if k < len(openers) and openers[k] < m.end():
            return True
    return False


def _note_problems(line: str, allowed: tuple[str, ...], problems: dict[str, None]) -> None:
    """Links, references and images are read with HTML entities decoded and backslash escapes dropped, as GitHub
    reads a link's destination; bare URLs also as written, since GitHub's autolink reads the source (an entity
    stays in the link), and references and mentions in both forms. A refused bare URL is named by the part of it
    before ( ) [ ] " ' ` or > when that part is refused too, else whole, as GitHub links it."""
    decoded = _ESCAPE.sub(r"\1", html.unescape(line))
    if _IMAGE.search(decoded):
        problems["has an image"] = None
    for text in (decoded, line):
        for m in _URL.finditer(text):
            if _foreign(link := _autolink(m[0]), allowed):
                shown = _SHOWN.match(link)[0]
                problems[f"links to {(shown if _foreign(shown, allowed) else link)[:80]}"] = None
        for m in _EMAIL.finditer(text):  # never an allowed prefix
            problems[f"links to {_SHOWN.match(_autolink(m[0]))[0][:80]}"] = None
    owners = {prefix[len(_GITHUB):].split("/")[0] for prefix in allowed if prefix.startswith(_GITHUB)}
    for text in (line, decoded):
        for m in _REFERENCE.finditer(text):
            if m[1].lower() not in owners:
                problems[f"links to {m[0][:80]}"] = None
    for text in (line, decoded):
        for m in _MENTION.finditer(text):
            problems[f"mentions @{m[1]}"] = None


def _autolink(url: str) -> str:
    """The part of a bare URL's run that GitHub links, as cmark-gfm's autolink_delim cuts it: no trailing
    ? ! . , : * _ ~ ' or ", no closing ")" beyond those the run opens, no trailing entity-like "&name;"."""
    end = len(url)
    opened, closed = url.count("("), url.count(")")
    while end:
        c = url[end - 1]
        if c == ")" and closed > opened:
            closed -= 1
        elif c == ";":
            k = end - 2
            while k > 0 and url[k].isascii() and url[k].isalpha():
                k -= 1
            if k < end - 2 and url[k] == "&":
                end = k
                continue
        elif c not in _AUTOLINK_TRAILING:
            break
        end -= 1
    return url[:end]


def _destinations(whole: str, checked: str) -> Iterator[tuple[str, bool]]:
    """Every link destination GitHub may read, as (its raw text, whether it was cut). Markdown ones are read in
    `whole`: a code span right after "](" is part of the destination. Attributes and autolinks are read in
    `checked`: a `<` before a code span keeps it as text (HTML mode, `_code_spans`). A destination is read only up to
    the next opener of its kind, which keeps the reading linear. One that runs on past it is cut, and refused."""
    yield from _bounded(whole, _LINK_OPEN, _markdown_destination)
    yield from _bounded(checked, _ATTRIBUTE, _attribute_value)
    for m in _AUTOLINK.finditer(checked):
        if ":" in m[1] or "@" in m[1]:  # a scheme or an email address: else it is a tag
            yield m[1], False


def _bounded(text: str, opener: re.Pattern[str], read: Callable[[str, int, int], tuple[int, int]]
             ) -> Iterator[tuple[str, bool]]:
    found = list(opener.finditer(text))
    for m, after in zip(found, [*found[1:], None]):
        bound = len(text) if after is None else after.start()
        start, end = read(text, m.end(), bound)
        yield text[start:end], end >= bound and after is not None


def _markdown_destination(text: str, at: int, bound: int) -> tuple[int, int]:
    """Where the destination after "](" or "]:" at `at` lies, as CommonMark reads it: in angle brackets, else a
    run without spaces or controls whose parentheses balance (a backslash escapes one)."""
    at = _SPACES.match(text, at, bound).end()
    if m := _POINTY.match(text, at, bound):
        return at + 1, m.end() - 1
    end, depth = at, 0
    while end < bound:
        if m := _PLAIN.match(text, end, bound):
            end = m.end()
        elif text[end] == "(" or (text[end] == ")" and depth):
            depth += 1 if text[end] == "(" else -1
            end += 1
        else:  # a space, a control, or the ")" that closes the link
            break
    return at, end


def _attribute_value(text: str, at: int, bound: int) -> tuple[int, int]:
    """Where the value of the attribute whose "=" ends at `at` lies, as HTML reads it."""
    if text.startswith(('"', "'"), at):
        end = text.find(text[at], at + 1, bound)
        return at + 1, bound if end == -1 else end
    return at, _UNQUOTED.match(text, at, bound).end()


def _foreign_destination(raw: str, cut: bool, allowed: tuple[str, ...]) -> str | None:
    """How to show the destination `raw` when it is not an allowed absolute URL, else None. It is read as
    GitHub may read it: entities decoded and backslash escapes dropped (Markdown), entities decoded only (HTML),
    or as written; each with tab, CR and LF deleted and C0 controls and spaces stripped from its ends, as a
    browser does. Every reading must be allowed. An empty one is the page itself."""
    entities = html.unescape(raw)
    forms = [form.translate(_TAB_CR_LF).strip(_C0_AND_SPACE)
             for form in (_ESCAPE.sub(r"\1", entities), entities, raw)]
    if cut:
        return forms[0] + "…"
    return next((form for form in forms if form and _foreign(form, allowed)), None)


def _foreign(url: str, allowed: tuple[str, ...]) -> bool:
    low = url.lower()
    return not low.startswith(allowed) or "%2e" in low or _DOT_SEGMENT.search(low) is not None
