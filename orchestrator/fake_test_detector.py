"""Detect tests that assert on source text instead of behaviour.

10 of the 21 test files the pipeline authored read production sources as
strings and grep them — PreviewAcmeAtomMigrationGuardTest,
SelectedUserListFlowRowGuardTest, FolderNameDialogFocusWiringTest,
ThemeWindowBackgroundTest, and PR #1529 even added a SourceFileLocator helper
to make it easier. Such a test cannot fail: the agent that wrote the source
wrote the assertion in the same run. It satisfies qa-agent's "every acceptance
criterion must have at least one test" without testing anything.

The signal is deliberately two-part — a file-read primitive *and* a reference
to a path under `src/`. A test that reads its own temp fixture is legitimate
and must not be flagged, or the gate blocks real work.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_TEST_PATH_MARKERS = ("/src/test/", "/src/androidTest/", "/tests/")

_READ_PRIMITIVES = (
    re.compile(r"\breadText\s*\("),
    re.compile(r"\breadLines\s*\("),
    re.compile(r"\bFile\s*\("),
    re.compile(r"\bPaths\.get\s*\("),
    re.compile(r"\bDocumentBuilderFactory\b"),
)

# A path into the tree under test, as it appears in a string literal.
_SOURCE_PATH = re.compile(r"[\"'][^\"']*\bsrc/(main|test|androidTest)/[^\"']*[\"']")


def detect_source_reading_tests(source_dir: Path, changed_files: list[str]) -> list[str]:
    """Return the changed test files that read source or resources as text."""
    offenders: list[str] = []
    for rel in changed_files:
        if not any(marker in f"/{rel}" for marker in _TEST_PATH_MARKERS):
            continue
        try:
            content = (source_dir / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not _SOURCE_PATH.search(content):
            continue
        if any(p.search(content) for p in _READ_PRIMITIVES):
            logger.info("Rejecting source-reading test: %s", rel)
            offenders.append(rel)
    return offenders
