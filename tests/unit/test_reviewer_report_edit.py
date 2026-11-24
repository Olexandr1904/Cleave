# tests/unit/test_reviewer_report_edit.py
from __future__ import annotations

import difflib
import os
import stat
import time

import pytest

from orchestrator.reviewer import report_edit
from orchestrator.reviewer.report import ReportError, parse_report
from orchestrator.reviewer.report_edit import (
    apply_draft,
    apply_edit,
    body_problems,
    drop_summary,
    ensure_base,
    item_text,
    split_at_file_line,
    tick,
    write_atomic,
)
from tests.unit.test_reviewer_report import HEAD, LS, NBSP, NPE_BODY, REPORT

FENCED_9 = "```md\n### [ ] 9. quoted\n```\n"
DRAFT = (7, "Null check in the adapter", "app/src/main/Adapter.kt", 12)
ALLOWED = ("https://github.com/acme-org/", "https://acme.atlassian.net/")


def _changed(old: str, new: str) -> list[str]:
    return [d for d in difflib.ndiff(old.splitlines(), new.splitlines()) if d[:2] in ("- ", "+ ")]


def test_tick_exact():
    body = "The list is read before the null check.\n"
    text = REPORT.replace("### [ ] 2.", "### [x] 2.").replace(body, body + FENCED_9)
    ticked = tick(text, {1, 4})
    assert _changed(text, ticked) == [
        "- ### [ ] 1. NPE on an empty list", "+ ### [x] 1. NPE on an empty list",
        "- ### [x] 2. Retry without backoff", "+ ### [ ] 2. Retry without backoff",
        "- ### [ ] 4. Double refresh on rotation?", "+ ### [x] 4. Double refresh on rotation?",
    ]
    assert len(ticked) == len(text) and FENCED_9 in ticked
    assert FENCED_9 in tick(text, {9})  # a heading in a fence is the finding's text, never ticked


def test_drop_summary():
    text = drop_summary(REPORT)
    assert "## Summary" not in text and "Two real problems" not in text
    assert "## ✅ Looks Good\n- Sync is idempotent.\n" in text
    assert text == REPORT[:REPORT.index("## Summary")]


def test_apply_edit():
    old = parse_report(REPORT, HEAD)
    new = parse_report(apply_edit(REPORT, HEAD, 2, "Use exponential backoff.", frozenset()), HEAD)
    assert new.items[1].body == (
        "**File:** `app/src/main/SyncWorker.kt:41` (anchor moves to line 43)\nUse exponential backoff.")
    assert [i for i in new.items if i.number != 2] == [i for i in old.items if i.number != 2]
    assert new.dropped == old.dropped


@pytest.mark.parametrize("body", ["## Summary\nx", "---\nx", "### [ ] 10. x",
                                  "x\n```x`\n### [x] 10. smuggled\n```"])  # no fence opens at ```x`
def test_apply_edit_refuses_structure(body):
    # Item 2 is the only item of its section: the first two leave every other item as it was and are
    # refused by the edited body and the Summary comparison.
    with pytest.raises(ReportError, match="changes the report's structure"):
        apply_edit(REPORT, HEAD, 2, body, frozenset())


def test_apply_draft():
    new = apply_draft(REPORT, HEAD, *DRAFT, "Is this reachable?", frozenset())
    r = parse_report(new, HEAD, frozenset({7}))
    assert [i.number for i in r.items] == [1, 2, 3, 4, 5, 7, 6]
    item = r.items[5]
    assert (item.kind, item.path, item.line) == ("question", "app/src/main/Adapter.kt", 12)
    assert item.body == "**File:** `app/src/main/Adapter.kt:12`\nIs this reachable?"
    assert [d.number for d in r.dropped] == [7, 8, 9]
    with pytest.raises(ReportError, match="number 7 is used twice"):
        parse_report(new, HEAD)

    no_questions = REPORT[:REPORT.index("## ❓ Questions")] + REPORT[REPORT.index("## 🟣 Pre-existing"):]
    drafted = apply_draft(no_questions, HEAD, *DRAFT, "Is this reachable?", frozenset())
    assert ("**File:** `app/src/main/CartMapper.kt:17`\n\n## ❓ Questions\n\n"
            "### [ ] 7. Null check in the adapter\n**File:** `app/src/main/Adapter.kt:12`\n"
            "Is this reachable?\n\n---\n## 🟣 Pre-existing\n") in drafted
    assert [(i.number, i.kind) for i in parse_report(drafted, HEAD, frozenset({7})).items][3:] == [
        (7, "question"), (6, "preexisting")]

    for body in ("Is this reachable?\n---\nmore", "Is this reachable?\n## Summary\nx"):
        with pytest.raises(ReportError, match="changes the report's structure"):
            apply_draft(REPORT, HEAD, *DRAFT, body, frozenset())


@pytest.mark.parametrize("title, path", [
    ("Null\ncheck", "app/src/main/Adapter.kt"),
    ("x" * 201, "app/src/main/Adapter.kt"),
    ("Null check", "app/src/`main`/Adapter.kt"),
    ("Null check", "app/src/main/\nAdapter.kt"),
])
def test_apply_draft_unreadable(title, path):
    with pytest.raises(ReportError, match="^draft unreadable$"):
        apply_draft(REPORT, HEAD, 7, title, path, 12, "Is this reachable?", frozenset())


@pytest.mark.parametrize("number", [42, 3])  # not in the report; an item, not a dropped number
def test_apply_draft_refuses_a_number_not_dropped(number):
    with pytest.raises(ReportError, match=f"^#{number} is not a dropped number$"):
        apply_draft(REPORT, HEAD, number, "Null check", "app/src/main/Adapter.kt", 12, "Is it?", frozenset())


def test_apply_draft_title_at_the_cap():
    new = apply_draft(REPORT, HEAD, 7, "x" * 200, "app/src/main/Adapter.kt", None, "Is this?", frozenset())
    item = parse_report(new, HEAD, frozenset({7})).items[5]
    assert (item.number, item.title, item.line) == (7, "x" * 200, None)


@pytest.mark.parametrize("text, problems", [
    ("![x](https://github.com/acme-org/a.png)", ["has an image"]),
    ("see https://evil.example/x", ["links to https://evil.example/x"]),
    ("https://github.com/other/repo", ["links to https://github.com/other/repo"]),
    ("https://github.com/acme-org/acme-app/pull/1", []),
    ("https://acme.atlassian.net/browse/ACME-1", []),
    ("@octo please look", ["mentions @octo"]),
    ("`@Composable` fun", []),
    ("```kotlin\n@Composable\nfun Screen() {}\n```", []),
    ("mail a@b.com", ["links to a@b.com"]),  # GitHub links an e-mail address (/code-review C9)
])
def test_body_problems(text, problems):
    assert body_problems(text, ALLOWED) == problems


@pytest.mark.parametrize("text, problems", [
    ("contact admin@evil.example", ["links to admin@evil.example"]),
    ("contact admin@evil.example.", ["links to admin@evil.example"]),
    ("mailto:x@evil.example", ["links to mailto:x@evil.example"]),
    ("xmpp:x@evil.example", ["links to xmpp:x@evil.example"]),
    ("XMPP:x@evil.example/phone", ["links to XMPP:x@evil.example/phone"]),
    ("[write](mailto:x@evil.example)", ["links to mailto:x@evil.example"]),
    ("`a@b.com` in a span", []),
    ("```\nadmin@evil.example\n```", []),
    ("user@host without a dot", []),
])
def test_body_problems_refuse_email_autolinks(text, problems):
    """GitHub autolinks a bare `user@host.tld`, `mailto:…` and `xmpp:…`: each is a link outside the allowlist, so
    it fails closed (/code-review C9). Code is still skipped."""
    assert body_problems(text, ALLOWED) == problems


IMAGE, EVIL, OCTO = "has an image", "links to https://evil.example", "mentions @octo"
BODY_EDGES = {
    "img-tag-upper": ('<IMG src="https://github.com/acme-org/a.png">', [IMAGE]),
    "www-and-upper": ("see WWW.evil.example/x and HTTPS://GITHUB.COM/ACME-ORG/x", ["links to WWW.evil.example/x"]),
    "cut-at-80": ("[docs](https://evil.example/" + "x" * 100 + ")",
                  ["links to " + ("https://evil.example/" + "x" * 100)[:80]]),
    "spans-by-length": ("`@a` and ``@b ` c`` but `@d", ["mentions @d"]),  # an unclosed run is text
    "escaped-tick": ("\\`@octo` x`", [OCTO]),                              # an escaped backtick opens no span
    "autolink-first": ("<https://github.com/acme-org/`> @octo `", [OCTO]),  # an autolink starts first
    "table-cells": ("| `x | @octo ` |", [OCTO]),                            # a table splits its cells first
    # Fix round 1: whatever Cleave cannot be sure GitHub renders as code is checked as text.
    "fence-backtick-info": ("```x`\n@octo ![i](https://evil.example/p.png) https://evil.example",
                            [IMAGE, "links to https://evil.example/p.png", EVIL, OCTO]),
    "fence-closed-by-list-item": ("- a\n  ```\n@octo https://evil.example ![i](x)", [IMAGE, EVIL, OCTO, "links to x"]),
    "fence-closed-deeper": ("- a\n  ```\n  x\n     ```\n  @octo\n  ```", [OCTO]),
    "html-block-hides-fence": (
        '<div>\n```\n<a href="https://evil.example">x</a> @octo <img src="p.png">\n```\n</div>',
        [IMAGE, EVIL, OCTO, "links to p.png"]),
    "html-block-hides-span": ("<div>\n`@octo` `https://evil.example`\n</div>", [EVIL, OCTO]),
    "html-block-in-list-item": ("- <div>\n  ```\n  @octo\n  ```\n  </div>", [OCTO]),
    "span-across-lines": ("a ` b\nc ` @octo https://evil.example ![i](x) ` d", [IMAGE, EVIL, OCTO, "links to x"]),
    "tag-across-lines": ("x <a title='\n`'>@octo https://evil.example `", [EVIL, OCTO]),
    "paragraph-ends-at-blank": ("a ` b\n\n`@octo` c", []),
    # Since round 4 a "<" and a letter anywhere turns HTML mode on, so every span is checked as text; since
    # 2026-11-24 not a "<" inside a span Cleave is sure of: an HTML block opens only where a line's content starts.
    "kotlin-generic-in-span": ("`List<String>` and `@Composable`", []),
    "xml-name-in-a-reply": ("Deletes `TEST-<FQN>.xml` first.\nNow a broken `@HiltAndroidTest` is caught.", []),
    # ... unless a "[" before the line's first backtick may be a footnote label holding it: read whole.
    "footnote-label-tick": ("See[^`].\n\n[^`]: <div> `\n    `@octo`", [OCTO, "links to div"]),
    "unpaired-tick-reads-whole": ("`List<String>` and ` @x\n`@octo`", ["mentions @x", OCTO]),
    "xml-in-a-fence": ('```xml\n<ImageView android:src="@drawable/icon" />\n```', []),
    # Since Task 9 fix round 1 bare URLs are also read in the raw line ("//evil.example" after "&#58;").
    "entity-in-dest": ("[x](https&#58;//evil.example)", [EVIL, "links to //evil.example"]),
    "backslash-in-dest": ("[x](https\\://evil.example)", [EVIL]),
    "protocol-relative": ("[x](//evil.example)", ["links to //evil.example"]),
    "backslash-relative": ("[x](/\\evil.example)", ["links to /\\evil.example"]),
    "entity-at": ("&#64;octo please", [OCTO]),
    "underscore-emphasis": ("_@octo_", [OCTO]),
    "dot-segment": ("[x](https://github.com/acme-org/../evil-org/phish)",
                    ["links to https://github.com/acme-org/../evil-org/phish"]),
    "encoded-dot": ("[x](https://github.com/acme-org/%2E%2E/evil-org/phish)",
                    ["links to https://github.com/acme-org/%2E%2E/evil-org/phish"]),
    "dot-github-dir": ("https://github.com/acme-org/acme-app/blob/main/.github/CODEOWNERS", []),
    # The review's probes that were already caught, kept as tests.
    "img-tag": ('<img src="https://github.com/acme-org/a.png">', [IMAGE]),
    "a-href": ('<a href="https://evil.example">x</a>', [EVIL]),
    "autolink": ("<https://evil.example>", [EVIL]),
    "reference": ("[x][r]\n\n[r]: https://evil.example", [EVIL]),
    "mention-in-link-text": ("[@octo](https://github.com/acme-org/x)", [OCTO]),
    "a-href-with-span": ('<a href="https://evil.example">`x`</a> `@octo`', [EVIL, OCTO]),
    "blockquote-fence": ("> ```\n@octo", [OCTO]),
    # Fix round 2: only a column-0 fence is trusted; lines and closers as cmark reads them; schemes without
    # slashes. The review's inputs:
    "list-html-block": ("- a\n\n  <div>\n  ```\n  @octo\n  ```", [OCTO]),
    "details-no-blank": ("<details>\n```\n@octo\n```\n</details>", [OCTO]),
    "details-blank-xml": ('<details>\n\n```xml\n<b android:x="@drawable/i"/>\n```\n</details>',
                          ["mentions @drawable"]),
    "quote-fence-then-unprefixed": ("> ```\n<div>\n```\n@octo\n```", [OCTO]),
    "quote-html": ("> <div>\n> ```\n> @octo\n> ```", [OCTO]),
    "html-line-in-real-fence": ("```\n<div>\n```\n@octo", [OCTO]),
    "list-fence-out-of-phase": ("- a\n    ```\n  ```\n  @octo https://evil.example", [EVIL, OCTO]),
    "ordered-out-of-phase": ("1. a\n     ```\n   ```\n   @octo https://evil.example", [EVIL, OCTO]),
    "out-of-phase-hosts-html": (('- a\n    ```\n  ```\n  <div>\n  ```\n  x\n  ```\n'
                                 '  @octo <a href="https://evil.example">x</a>\n  ```'), [EVIL, OCTO]),
    "nbsp-after-closer": (f"```\nx\n```{NBSP}\ny\n```\n@octo https://evil.example", [EVIL, OCTO]),
    "u2028-fence": (f"a{LS}```\n@octo https://evil.example", [EVIL, OCTO]),
    "formfeed-fence": ("a\x0c```\n@octo https://evil.example", [EVIL, OCTO]),
    "http-no-slash": ("[x](http:evil.example)", ["links to http:evil.example"]),
    "http-no-slash-html": ('<a href="http:evil.example">x</a>', ["links to http:evil.example"]),
    # ... and what the column-0 rule costs and keeps.
    "indented-fence-is-text": ("- a\n\n  ```kotlin\n  @Composable\n  ```", ["mentions @Composable"]),
    "android-in-a-column-0-fence": ('```\n<LinearLayout android:background="@drawable/x">\n@Composable\n```', []),
    # Fix round 3: a column-0 fence is trusted only while no earlier fence-looking line is indented or after a
    # list or quote marker (from there `fenced` may be out of step with GitHub); a leading BOM is dropped.
    "list-4sp-fence-then-info-line": ("- a\n    ```\n  ```x\n```\nok\n```\n@octo https://evil.example", [EVIL, OCTO]),
    "marker-line-fence-then-info-line": ("- ```\n  ```x\n```\nok\n```\n@octo https://evil.example", [EVIL, OCTO]),
    "tab-indented-list-fence": ("- a\n\t```\n  ```x\n```\nok\n```\n@octo https://evil.example", [EVIL, OCTO]),
    "deeper-closer-then-column-0": ("- a\n  ```\n     ```\n```\nx\n```\n@octo https://evil.example", [EVIL, OCTO]),
    "bom-first": (f"{chr(0xFEFF)}```\nx\n```\n@octo https://evil.example", [EVIL, OCTO]),
    "indented-opener-then-column-0": ("  ```\n```\n@octo", [OCTO]),
    "column-0-kotlin-and-xml": (("Annotate it:\n\n```kotlin\n@Composable\nfun Screen() {}\n```\n\nand the layout:\n\n"
                                 '```xml\n<LinearLayout android:background="@drawable/x">\n```\n'), []),
    "markdown-sample-in-a-fence": ("````md\n- a\n  ```kotlin\n  @Inject\n  ```\n````\n\n```kotlin\n@Composable\n```",
                                   []),  # a trusted fence's own lines are in step: they move no phase
    # Fix round 4: HTML mode switches on at a "<" that may open a tag (a letter, "/", "!" or "?" after it),
    # wherever it sits outside a trusted fence: an HTML block can open after a footnote label.
    "footnote-html-block": (('See[^1].\n\n[^1]: <pre>\n\n    `<a href="https://evil.example">x</a> '
                             '<img src="https://evil.example/p.png">`\n    end</pre>'),
                            [IMAGE, EVIL, "links to https://evil.example/p.png", "links to pre"]),
    "footnote-html-no-blank": ('See[^1].\n\n[^1]: <div>\n    `<a href="https://evil.example">x</a>`',
                               [EVIL, "links to div"]),
    "tag-after-a-span": ("`@octo` and <b>x</b>", [OCTO]),
    "closing-tag-after-a-span": ("`@octo` and x</b>", [OCTO]),
    "comment-after-a-span": ("`@octo` and <!-- x -->", [OCTO]),
    "instruction-after-a-span": ("`@octo` and <?x?>", [OCTO]),
    "no-tag-after-a-span": ("`@octo` and a < b, x <= y, <3", []),
    "generic-in-prose": ("Use `List<String>` for the rows.", []),
    # Fix round 5: every link destination (inline link or image, reference definition, href or src, autolink)
    # must be an allowed absolute URL, read with entities decoded, escapes dropped, tab, CR and LF deleted. The
    # review's inputs:
    "href-tab-entity": ('<a href="/&#9;/evil.example">x</a>', ["links to //evil.example"]),
    "href-tab-literal": ('<a href="/\t/evil.example">x</a>', ["links to //evil.example"]),
    "relative-root": ("[x](/evil-org/phish)", ["links to /evil-org/phish"]),
    "relative-dotdot": ("[x](../../../evil-org/phish)", ["links to ../../../evil-org/phish"]),
    "relative-href": ('<a href="/evil-org/phish">', ["links to /evil-org/phish"]),
    "relative-reference": ("[r]: /evil-org/phish", ["links to /evil-org/phish"]),
    "relative-reference-used": ("[x][r]\n\n[r]: /evil-org/phish", ["links to /evil-org/phish"]),
    "mailto": ("[x](mailto:a@evil.example)", ["links to mailto:a@evil.example"]),
    "allowed-absolute": ("[x](https://github.com/acme-org/acme-app/pull/1)", []),
    # ... and destinations as CommonMark and browsers read them.
    "balanced-parens-in-dest": ("[x](https://github.com/acme-org/a(b)/../../evil-org)",
                                ["links to https://github.com/acme-org/a(b)/../../evil-org"]),
    "escaped-paren-in-dest": ("[x](https://github.com/acme-org/a\\)/../../evil-org)",
                              ["links to https://github.com/acme-org/a)/../../evil-org",
                               "links to https://github.com/acme-org/a\\)/../../evil-org"]),  # and as written
    "dest-on-the-next-line": ("[x](\n/evil-org/phish)", ["links to /evil-org/phish"]),
    "code-span-as-dest": ("[x](`/../../../../evil-org`)", ["links to `/../../../../evil-org`"]),
    # Since Task 9 the bare-URL check also reads the text as GitHub's autolink would, past "(" and "]".
    "dest-runs-past-another-opener": ("[x](https://github.com/acme-org/a(](https://github.com/acme-org/b)/../../evil)",
                                      ["links to https://github.com/acme-org/a(](https://github.com/acme-org/b)/../../evil)",
                                       "links to https://github.com/acme-org/a(…"]),
    "attribute-backslashes": ('<a href="https://github.com/acme-org/\\..\\..\\evil-org">x</a>',
                              ["links to https://github.com/acme-org/\\..\\..\\evil-org"]),
    "attribute-unquoted": ("<a href=/evil-org/phish>x</a>", ["links to /evil-org/phish"]),
    "attribute-trailing-control": ('<a href="https://github.com/acme-org/..\x01">x</a>',
                                   ["links to https://github.com/acme-org/.."]),
    "autolink-mailto": ("<mailto:a@evil.example>", ["links to mailto:a@evil.example"]),
    "autolink-email": ("<1@evil.example>", ["links to 1@evil.example"]),
    "empty-dest": ("[x]() and [y](<>)", []),
    "bare-ftp": ("see FTP://evil.example/x", ["links to FTP://evil.example/x"]),  # GitHub autolinks ftp:// too
    # Mentions: only an ASCII letter or digit right before "@" makes it no mention.
    "letter-before-at": ("é@octo", [OCTO]),
    "cjk-before-at": ("中@octo", [OCTO]),
    "dot-before-at": ("see.@octo", [OCTO]),
    "slash-before-at": ("a/@octo", [OCTO]),
    "email": ("mail me at a@b.com", ["links to a@b.com"]),  # GitHub links a bare e-mail address (C9)
    # Carried into Task 9 (Task 5's breaker): a bare URL runs to whitespace or "<", as GitHub's autolink does, so
    # a dot segment after ( ) [ ] " ' is seen; trailing punctuation and unbalanced ")" are cut as GitHub cuts them.
    "autolink-paren": ("see https://github.com/acme-org/x(/../../evil-org/phish)",
                       ["links to https://github.com/acme-org/x(/../../evil-org/phish)"]),
    "autolink-quote": ("see https://github.com/acme-org/x'/../../evil-org/phish",
                       ["links to https://github.com/acme-org/x'/../../evil-org/phish"]),
    "autolink-bracket": ("see https://github.com/acme-org/x]/../../evil-org/phish",
                         ["links to https://github.com/acme-org/x]/../../evil-org/phish"]),
    "autolink-double-quote": ('see https://github.com/acme-org/x"/../../evil-org/phish',
                              ['links to https://github.com/acme-org/x"/../../evil-org/phish']),
    "autolink-entity-quote": ("see https://github.com/acme-org/x&#39;/../../evil-org/phish",
                              ["links to https://github.com/acme-org/x'/../../evil-org/phish",
                               "links to https://github.com/acme-org/x&#39;/../../evil-org/phish"]),
    # Fix round 1: cmark-gfm autolinks the raw source, so an entity stays in the link ("&lt;" is no "<" there).
    "autolink-raw-lt": ("see https://github.com/acme-org/x&lt;/../../evil-org/phish",
                        ["links to https://github.com/acme-org/x&lt;/../../evil-org/phish"]),
    "autolink-raw-lt-no-semicolon": ("see https://github.com/acme-org/x&lt/../../evil-org/phish",
                                     ["links to https://github.com/acme-org/x&lt/../../evil-org/phish"]),
    "autolink-raw-tab": ("see https://github.com/acme-org/x&Tab;/../../evil-org/phish",
                         ["links to https://github.com/acme-org/x&Tab;/../../evil-org/phish"]),
    "autolink-raw-newline": ("see https://github.com/acme-org/x&NewLine;/../../evil-org/phish",
                             ["links to https://github.com/acme-org/x&NewLine;/../../evil-org/phish"]),
    "autolink-into-code-span": ("see https://github.com/acme-org/x`/../../evil-org`",
                                ["links to https://github.com/acme-org/x`/../../evil-org`"]),
    "allowed-in-parentheses": ("(https://github.com/acme-org/acme-app/pull/1)", []),
    "allowed-before-punctuation": ('"https://github.com/acme-org/acme-app/pull/1", then https://github.com/acme-org/x/.',
                                   []),
    "url-in-a-code-span": ("`https://evil.example/x` is the stub", []),
    # ... cross-repo references to an owner that is not the allowed one.
    "cross-repo-issue": ("see evil-org/phish#1", ["links to evil-org/phish#1"]),
    "cross-repo-commit": ("see evil-org/phish@1234567", ["links to evil-org/phish@1234567"]),
    "cross-repo-entity": ("see evil-org/phish&#35;1", ["links to evil-org/phish#1"]),
    "same-owner-issue": ("acme-org/acme-app#12 and Acme-Org/acme-app@1234567", []),
    "owner-prefix-issue": ("acme-org-x/acme-app#12", ["links to acme-org-x/acme-app#12"]),
    "path-not-a-reference": ("app/src/main/Cart.kt#L12 and app/src@main", []),
    "user-at-sha": ("see evil@1234567abc", ["links to evil@1234567abc"]),  # a commit in that user's fork
    "same-owner-at-sha": ("acme-org@1234567, a@b.com and this@1234567xyz", ["links to a@b.com"]),  # an e-mail (C9)
    # ... and MathJax: a math fence is never trusted as code, a $`…`$ span is checked as text.
    "math-fence": ("```math\n\\href{https://evil.example}{x}\n```", ["links to https://evil.example}{x}"]),
    "math-fence-info-entity": ("```&#109;ath\n\\href{https://evil.example}{x}\n```",
                               ["links to https://evil.example}{x}"]),
    "math-span": ("$`\\href{https://evil.example}{x}`$", ["links to https://evil.example}{x}"]),
    "code-fence-after-math": ("```math\nx^2\n```\n\n```kotlin\n@Composable\n```", []),
    "math-fence-right-after-code": ("```kotlin\nx\n```\n```math\n\\href{https://evil.example}{x}\n```",
                                    ["links to https://evil.example}{x}"]),
    "indented-math-fence-right-after-code": ("```kotlin\nx\n```\n  ```math\n\\href{https://evil.example}{x}\n  ```",
                                             ["links to https://evil.example}{x}"]),
}


def test_cross_repo_references_need_an_allowed_github_owner():
    assert body_problems("see acme-org/acme-app#12", ("https://acme.atlassian.net/",)) == [
        "links to acme-org/acme-app#12"]
    assert body_problems("see -evil-org/phish#1", ALLOWED) == ["links to -evil-org/phish#1"]


def _runtime(text: str) -> float:
    """The best of three runs: noise only ever adds time."""
    runs = []
    for _ in range(3):
        start = time.perf_counter()
        body_problems(text, ALLOWED)
        runs.append(time.perf_counter() - start)
    return min(runs)


HOSTILE = {
    "owner-run": lambda n: "a-" * n + "a/b",  # never reaches a reference
    "repo-run": lambda n: "a/" + "b." * n,  # never reaches "#" or "@"
    "user-at-run": lambda n: "a-" * n + "a@b",  # never reaches a sha
    "parentheses-and-spans": lambda n: "see https://github.com/acme-org/" + "(" * n + " `x` " * (n // 100),
    "urls-over-a-span": lambda n: "`" + "https://x " * (n // 5) + "`",
    "mail-run": lambda n: "a." * n + "a@b",  # never reaches a domain with a dot
}


@pytest.mark.parametrize("hostile", list(HOSTILE.values()), ids=list(HOSTILE))
def test_body_problems_in_linear_time(hostile):
    # Four times the input takes about four times as long when linear, sixteen when quadratic.
    assert _runtime(hostile(160_000)) < 8 * _runtime(hostile(40_000))


@pytest.mark.parametrize("text, problems", list(BODY_EDGES.values()), ids=list(BODY_EDGES))
def test_body_problems_edges(text, problems):
    assert body_problems(text, ALLOWED) == problems


def test_ensure_base_and_item_text(tmp_path):
    name = f"report_{HEAD[:9]}.md"
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    ticked = REPORT.replace("### [ ] 1.", "### [x] 1.").replace("\n", "\r\n")
    (job_dir / name).write_bytes(ticked.encode("utf-8"))
    posting = job_dir / "posting"
    base = ensure_base(job_dir, name)
    assert base == posting / "report.base.md"
    assert (posting / "report.original.md").read_bytes() == ticked.encode("utf-8")
    assert base.read_bytes() == REPORT.replace("\n", "\r\n").encode("utf-8")
    assert stat.S_IMODE(posting.stat().st_mode) == 0o700
    assert sorted(os.listdir(posting)) == ["report.base.md", "report.original.md"]
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in posting.iterdir()}
    (job_dir / name).write_text("changed", encoding="utf-8")
    assert ensure_base(job_dir, name) == base
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in posting.iterdir()} == before

    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "report.base.md").write_text(REPORT, encoding="utf-8")
    for i, make in enumerate([
        lambda p: p.mkdir(),
        lambda p: p.symlink_to(planted, target_is_directory=True),
        lambda p: p.write_text("x", encoding="utf-8"),
    ]):
        other = tmp_path / f"job{i}"
        other.mkdir()
        (other / name).write_text(REPORT, encoding="utf-8")
        make(other / "posting")
        with pytest.raises(ReportError, match="^the job copy's posting directory is not Cleave's$"):
            ensure_base(other, name)

    item = parse_report(REPORT, HEAD).items[1]
    assert item_text(item) == (
        "#2 · app/src/main/SyncWorker.kt:41 (moves to 43)\n**Retry without backoff**\n\nNetwork retry rule.")


# --- Fix round 2b: no line separator that str.splitlines() honours and cmark does not ---

ODD_CHARACTER = "The text holds a character that tools read differently: rewrite it. Nothing was done."
SPLITLINES_ONLY = [0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029]


@pytest.mark.parametrize("body", [f"x{LS}### [x] 10. smuggled", *(f"x{chr(cp)}y" for cp in SPLITLINES_ONLY),
                                  f"x{chr(0xFEFF)}y"])  # a BOM is invisible, and no review needs one
def test_apply_edit_refuses_a_line_separator(body):
    with pytest.raises(ReportError) as e:
        apply_edit(REPORT, HEAD, 2, body, frozenset())
    assert str(e.value) == ODD_CHARACTER


@pytest.mark.parametrize("title, path, body", [
    (DRAFT[1], DRAFT[2], "Is this\x0creachable?"),
    (f"Null{LS}check", DRAFT[2], "Is this reachable?"),
    (DRAFT[1], f"app/src/main/Adapter.kt{chr(0x85)}", "Is this reachable?"),
    (f"{chr(0xFEFF)}Null check", DRAFT[2], "Is this reachable?"),
])
def test_apply_draft_refuses_a_line_separator(title, path, body):
    with pytest.raises(ReportError) as e:
        apply_draft(REPORT, HEAD, 7, title, path, 12, body, frozenset())
    assert str(e.value) == ODD_CHARACTER


@pytest.mark.parametrize("cp", [0x2029, 0xFEFF])
def test_ensure_base_refuses_a_report_with_a_line_separator(tmp_path, cp):
    name = f"report_{HEAD[:9]}.md"
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / name).write_text(REPORT.replace(NPE_BODY, f"Read{chr(cp)}first.\n"), encoding="utf-8")
    with pytest.raises(ReportError) as e:
        ensure_base(job_dir, name)
    assert str(e.value) == f"the job's report holds a character that tools read differently (U+{cp:04X})"
    assert chr(cp) not in str(e.value)
    assert os.listdir(job_dir) == [name]  # nothing made: the post fails closed
    (job_dir / name).write_text(REPORT, encoding="utf-8")  # a plain report still passes
    assert ensure_base(job_dir, name).read_text(encoding="utf-8") == REPORT


def test_write_atomic(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("precious", encoding="utf-8")
    target = tmp_path / "out.md"
    target.symlink_to(victim)
    write_atomic(target, "a\r\nb ❓")
    assert target.read_bytes() == "a\r\nb ❓".encode() and not target.is_symlink()
    assert victim.read_text(encoding="utf-8") == "precious"
    assert sorted(os.listdir(tmp_path)) == ["out.md", "victim"]


def test_ensure_base_is_all_or_nothing(tmp_path, monkeypatch):
    name = f"report_{HEAD[:9]}.md"
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / name).write_text(REPORT, encoding="utf-8")
    (job_dir / ".posting.left").mkdir()  # a temp directory left by a process killed mid-way

    def crash(text, numbers):
        raise OSError("disk full")

    monkeypatch.setattr(report_edit, "tick", crash)
    with pytest.raises(OSError, match="disk full"):
        ensure_base(job_dir, name)
    assert sorted(os.listdir(job_dir)) == [".posting.left", name]  # no posting/, and its temp directory went
    monkeypatch.undo()
    base = ensure_base(job_dir, name)
    assert base.read_text(encoding="utf-8") == REPORT
    assert sorted(os.listdir(job_dir / "posting")) == ["report.base.md", "report.original.md"]
    assert stat.S_IMODE((job_dir / "posting").stat().st_mode) == 0o700


def test_split_at_file_line():
    """The lines before and after the first `**File:**` line; without one, all of them come after."""
    assert split_at_file_line("Lead\n**File:** `a.kt:1`\nText\n**File:** `b.kt`") == (
        ["Lead"], ["Text", "**File:** `b.kt`"])
    assert split_at_file_line("**File:** `a.kt:1`") == ([], [])
    assert split_at_file_line("Text\nmore") == ([], ["Text", "more"])
