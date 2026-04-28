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

# Two markers, either of which is sufficient evidence that the transforms cache
# is corrupt. The "Daemon startup failed" line follows the binary-execution
# failure; the syntax-error line is the underlying cause (a shell trying to
# interpret a binary file). Real failures observed in production usually emit
# both, but matching either makes the detector resilient to log truncation.
_AAPT2_CORRUPTION_RE = re.compile(
    r"aapt2[^\n]*Daemon[^\n]*startup failed"
    r"|"
    r"aapt2[^\n]*Syntax error[^\n]*unexpected",
    re.IGNORECASE,
)


def looks_like_gradle_cache_corruption(error_message: str | None) -> bool:
    """True if the error matches the AAPT2 transforms-cache corruption signature."""
    if not error_message:
        return False
    return bool(_AAPT2_CORRUPTION_RE.search(error_message))


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
