"""Detect and recover from Gradle cache corruption (notably AAPT2 daemon failures).

When the AAPT2 binary inside `~/.gradle/caches/<version>/transforms/` becomes
corrupt (interrupted download, partial extract, lost executable bit), every
Gradle-driven build downstream — including pre-push hooks invoked by `git push`
— fails with a distinctive shell-can't-execute-binary error pattern.

The fix is to delete the offending transforms tree and let Gradle re-extract it
on the next build. This module exposes a detector and a remediation helper used
by the orchestrator's failure-notification path and the dashboard's recovery
endpoint.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

# Multiple symptoms point at the same root cause: the
# `<gradle_home>/caches/*/transforms/` tree is in an inconsistent state and the
# fix is identical (delete it, let Gradle re-extract). We match any of:
#
#   1. AAPT2 "Daemon startup failed" — the OS shell tried to exec the corrupt
#      aapt2 binary and failed.
#   2. AAPT2 syntax-error-on-binary — same root cause, different log line; the
#      shell choked on a binary character.
#   3. AarResourcesCompilerTransform / Failed-to-transform pointing at a path
#      under `caches/.../transforms/` — the .aar extract directory exists but
#      its contents (e.g. AndroidManifest.xml) are missing or unreadable.
#
# False positives need to stay rare: every match offers the operator a
# destructive remediation. Keep the patterns specific to "transforms" to avoid
# misclassifying generic build errors that happen to mention aapt2 or aar.
_GRADLE_CACHE_CORRUPTION_PATTERNS = (
    re.compile(r"aapt2[^\n]*Daemon[^\n]*startup failed", re.IGNORECASE),
    re.compile(r"aapt2[^\n]*Syntax error[^\n]*unexpected", re.IGNORECASE),
    # AarResourcesCompilerTransform failure with a transforms/-cache path on
    # the next line, usually pointing at a missing AndroidManifest.xml.
    re.compile(
        r"Execution failed for AarResourcesCompilerTransform[^\n]*\n"
        r"[^\n]*caches[^\n]*transforms[^\n]*",
        re.IGNORECASE,
    ),
    # "Failed to transform <name>.aar" + transforms/ path within a few lines.
    # Lighter-weight than the more specific patterns above, but constrained to
    # paths inside transforms/ so non-cache .aar issues don't match.
    re.compile(
        r"Failed to transform[^\n]*\.aar[\s\S]{0,400}?caches[/\\][^\s/\\]+[/\\]transforms[/\\]",
        re.IGNORECASE,
    ),
)


def looks_like_gradle_cache_corruption(error_message: str | None) -> bool:
    """True if the error matches any known Gradle transforms-cache corruption signature."""
    if not error_message:
        return False
    return any(p.search(error_message) for p in _GRADLE_CACHE_CORRUPTION_PATTERNS)


def clear_gradle_transforms(gradle_home: Path | None = None) -> int:
    """Remove every `<gradle_home>/caches/*/transforms` directory.

    Targets all Gradle version subdirs because the corrupt binary may live in
    an older cache the user no longer tracks consciously. Returns total bytes
    freed across all removed trees. Safe when no transforms dirs exist (returns 0).
    """
    home = gradle_home or _default_gradle_home()
    caches = home / "caches"
    if not caches.is_dir():
        return 0

    freed = 0
    for entry in caches.iterdir():
        if not entry.is_dir():
            continue
        transforms = entry / "transforms"
        if transforms.is_dir():
            freed += _dir_size(transforms)
            shutil.rmtree(transforms)
    return freed


def _default_gradle_home() -> Path:
    env = os.environ.get("GRADLE_USER_HOME")
    if env:
        return Path(env)
    return Path.home() / ".gradle"


def _dir_size(path: Path) -> int:
    """Recursive byte size of a directory tree. Tolerates files vanishing mid-walk."""
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except (OSError, FileNotFoundError):
                pass
    return total
