"""Host-level build environment guards.

Android builds run a gradle daemon plus a Kotlin compiler daemon, each of which
can request gigabytes of JVM heap from each repo's own `gradle.properties`. On a
modest host that oversubscribes RAM and the Kotlin daemon gets OOM-killed
mid-compile (observed: `EOFException` in `compileWithDaemon`). Writing capped
heaps into `GRADLE_USER_HOME/gradle.properties` overrides the per-repo values
(GRADLE_USER_HOME takes precedence over the project file) without editing the
checked-out source.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Keys we manage. Each repo's project gradle.properties may set these much
# higher; the GRADLE_USER_HOME copy wins, so these caps take effect everywhere.
_GRADLE_KEY = "org.gradle.jvmargs"
_KOTLIN_KEY = "kotlin.daemon.jvmargs"


def apply_build_memory_limits(
    gradle_user_home: Path,
    gradle_heap_mb: int,
    kotlin_heap_mb: int,
) -> Path:
    """Write capped `org.gradle.jvmargs` / `kotlin.daemon.jvmargs` into
    `gradle_user_home/gradle.properties`, preserving any other keys present.

    Returns the path written.
    """
    gradle_user_home = Path(gradle_user_home)
    gradle_user_home.mkdir(parents=True, exist_ok=True)
    props_file = gradle_user_home / "gradle.properties"

    desired = {
        _GRADLE_KEY: f"-Xmx{gradle_heap_mb}m -XX:+UseG1GC -Dfile.encoding=UTF-8",
        _KOTLIN_KEY: f"-Xmx{kotlin_heap_mb}m -XX:+UseG1GC",
    }

    existing_lines = (
        props_file.read_text(encoding="utf-8").splitlines()
        if props_file.exists()
        else []
    )

    out: list[str] = []
    seen: set[str] = set()
    for line in existing_lines:
        stripped = line.lstrip()
        key = (
            line.split("=", 1)[0].strip()
            if "=" in line and not stripped.startswith("#")
            else None
        )
        if key in desired:
            out.append(f"{key}={desired[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, val in desired.items():
        if key not in seen:
            out.append(f"{key}={val}")

    props_file.write_text("\n".join(out) + "\n", encoding="utf-8")
    logger.info(
        "Capped build heaps in %s (gradle=%dMB, kotlin=%dMB)",
        props_file, gradle_heap_mb, kotlin_heap_mb,
    )
    return props_file
