"""Atomic .env file writer for the project-create wizard.

Writes raw secret assignments to the repo-root .env file that run.sh sources
at startup. All writes are atomic (tempfile + rename) and the resulting file
permissions are 0600.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")


@dataclass
class EnvCollisionError(Exception):
    vars: list[str]

    def __str__(self) -> str:
        return f"env vars already defined: {', '.join(self.vars)}"


def read_existing_vars(env_path: Path) -> set[str]:
    if not env_path.exists():
        return set()
    names: set[str] = set()
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _ASSIGN_RE.match(stripped)
        if m:
            names.add(m.group(1))
    return names


def _atomic_write(env_path: Path, content: str) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".env.", dir=str(env_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, env_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def append_vars(env_path: Path, vars: dict[str, str]) -> None:
    existing = read_existing_vars(env_path)
    collisions = sorted(name for name in vars if name in existing)
    if collisions:
        raise EnvCollisionError(vars=collisions)

    prefix = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    addition = "".join(f"{name}={value}\n" for name, value in vars.items())
    _atomic_write(env_path, prefix + addition)


def remove_vars(env_path: Path, names: list[str]) -> None:
    if not env_path.exists():
        return
    to_remove = set(names)
    kept: list[str] = []
    for line in env_path.read_text(encoding="utf-8").splitlines():
        m = _ASSIGN_RE.match(line.strip())
        if m and m.group(1) in to_remove:
            continue
        kept.append(line)
    content = "\n".join(kept)
    if content and not content.endswith("\n"):
        content += "\n"
    _atomic_write(env_path, content)
