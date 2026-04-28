"""Detect and recover from Gradle cache corruption (notably AAPT2 daemon failures).

When the AAPT2 binary inside `~/.gradle/caches/<version>/transforms/` becomes
corrupt (interrupted download, partial extract, lost executable bit), every
Gradle-driven build downstream — including pre-push hooks invoked by `git push`
— fails with a distinctive shell-can't-execute-binary error pattern.

The fix is to delete the offending transforms tree and let Gradle re-extract it
on the next build. **However**, simply wiping the cache is not enough: any
running Gradle daemon caches in-memory references to the (now-deleted)
transforms paths and reuses them on the next invocation, causing a fast
3-second build failure with the same symptom but a fresh, equally-broken cache
underneath. Recovery therefore requires three steps:

  1. Stop any running Gradle daemon (they auto-respawn on next build).
  2. Remove the daemon registry, so Gradle does not try to reattach to the
     dead daemon.
  3. Wipe `<gradle_home>/caches/*/transforms/`.

This module exposes a detector and a remediation helper used by the
orchestrator's failure-notification path and the dashboard's recovery endpoint.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

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

# Architecture mismatch: Gradle picked the x86-64 build of aapt2 (the directory
# name `aapt2-<ver>-<build>-linux` is the convention for x86-64 Linux; the ARM
# variant is `-linux_aarch64`). On a non-x86 host this binary cannot exec, the
# kernel returns ENOEXEC, and the shell falls back to interpreting the file as
# a script — which produces "Syntax error: '(' unexpected" on the binary's
# header bytes. Cache wipes don't help here: the next download will produce an
# equally-incompatible binary.
_AAPT2_ARCH_MISMATCH_RE = re.compile(
    r"aapt2-[^/\s]+-linux/aapt2:[^\n]*Syntax error[^\n]*unexpected",
    re.IGNORECASE,
)


def looks_like_aapt2_arch_mismatch(error_message: str | None) -> bool:
    """True if the error indicates an x86-64 aapt2 binary on a non-x86 host."""
    if not error_message:
        return False
    return bool(_AAPT2_ARCH_MISMATCH_RE.search(error_message))


def looks_like_gradle_cache_corruption(error_message: str | None) -> bool:
    """True if the error matches any known Gradle transforms-cache corruption signature.

    Returns False for architecture-mismatch errors (handled separately by
    `looks_like_aapt2_arch_mismatch`) — those textually look similar but
    can't be remediated by clearing the cache.
    """
    if not error_message:
        return False
    if looks_like_aapt2_arch_mismatch(error_message):
        return False
    return any(p.search(error_message) for p in _GRADLE_CACHE_CORRUPTION_PATTERNS)


# Help text shown in TG / dashboard when the architecture mismatch is detected.
# Kept here so the orchestrator and dashboard render the same guidance.
ARCH_MISMATCH_HELP = (
    "Gradle picked the x86-64 build of aapt2 but the host can't run it.\n"
    "Pipeline can't auto-fix this. To unblock, choose one:\n"
    "1. Install x86-64 emulation:\n"
    "   sudo apt install qemu-user-static binfmt-support\n"
    "2. Force AGP to use the aarch64 aapt2 build by adding to "
    "~/.gradle/gradle.properties:\n"
    "   org.gradle.jvmargs=-Dos.arch=aarch64\n"
    "3. Run the build on an x86-64 host.\n"
    "After fixing the host, click Retry."
)


_GRADLE_DAEMON_MAIN_CLASS = "org.gradle.launcher.daemon.bootstrap.GradleDaemon"


def _stop_gradle_daemons() -> bool:
    """Kill any running Gradle daemon processes owned by the current user.

    Returns True if at least one daemon was signalled, False otherwise. Errors
    are logged but never raised — daemons are best-effort to stop, and Gradle
    will respawn them as needed on the next build.
    """
    try:
        result = subprocess.run(
            ["pkill", "-TERM", "-f", _GRADLE_DAEMON_MAIN_CLASS],
            check=False, timeout=5, capture_output=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        logger.warning("Could not stop Gradle daemons (pkill failed): %s", e)
        return False
    # pkill exit codes: 0 = at least one matched, 1 = none matched, 2/3 = error
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    logger.warning("pkill returned %d for GradleDaemon: %s",
                   result.returncode, result.stderr.decode(errors="replace"))
    return False


def _wipe_daemon_registry(gradle_home: Path) -> None:
    """Remove `registry.bin` and its lock under each `daemon/<version>/` dir.

    The registry tracks daemon addresses for reattachment. Stale entries cause
    Gradle to wait on dead processes; removing the registry forces a clean
    fresh-daemon spawn on the next build.
    """
    daemon_root = gradle_home / "daemon"
    if not daemon_root.is_dir():
        return
    for ver in daemon_root.iterdir():
        if not ver.is_dir():
            continue
        (ver / "registry.bin").unlink(missing_ok=True)
        (ver / "registry.bin.lock").unlink(missing_ok=True)


def clear_gradle_transforms(gradle_home: Path | None = None) -> int:
    """Full Gradle cache-corruption remediation: stop daemons, wipe registry,
    remove every `<gradle_home>/caches/*/transforms` directory.

    Targets all Gradle version subdirs because the corrupt binary may live in
    an older cache the user no longer tracks consciously. Returns bytes freed
    by the transforms wipe. Daemon kill / registry wipe failures do NOT raise:
    they are logged and the function continues with the cache wipe (which is
    the most-impactful part).
    """
    home = gradle_home or _default_gradle_home()

    # 1. Stop any running daemons. They cache in-memory references to the
    #    transforms tree we are about to delete; if we leave them alive,
    #    builds reuse them and fail with stale-reference errors.
    _stop_gradle_daemons()

    # 2. Wipe daemon registry so the next build can't try to reattach to a
    #    process we just killed.
    _wipe_daemon_registry(home)

    # 3. Wipe the transforms cache itself.
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
