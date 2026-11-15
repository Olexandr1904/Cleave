"""Render Cleave's launchd plist from the template in deploy/.

Fills the `{{NAME}}` placeholders of deploy/com.cleave.daemon.plist and
refuses paths launchd cannot use: relative ones, and any under ~/Desktop,
~/Documents, ~/Downloads or iCloud Drive (~/Library/Mobile Documents), where
macOS privacy protection (TCC) blocks a launchd agent, symlinks resolved.

Usage:
    python scripts/render_launchd_plist.py --out <plist> --tmpdir "$(getconf DARWIN_USER_TEMP_DIR)"
"""
from __future__ import annotations

import argparse
import getpass
import os
import plistlib
import posixpath
import re
import sys
from pathlib import Path

LABEL = "com.cleave.daemon"
DEFAULT_PATH = "/opt/homebrew/opt/python@3.12/libexec/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
DEFAULT_LANG = "en_US.UTF-8"
PRIVACY_DIRS = ("Desktop", "Documents", "Downloads", "Library/Mobile Documents")

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = REPO_ROOT / "deploy" / f"{LABEL}.plist"
_PLACEHOLDER = re.compile(r"\{\{([^{}]*)\}\}")


class PlistError(ValueError):
    """The plist cannot be rendered with these values."""


def _fill(obj, values: dict[str, str]):
    if isinstance(obj, str):
        return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), obj)
    if isinstance(obj, dict):
        return {_fill(k, values): _fill(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, values) for v in obj]
    return obj


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _privacy_folder(path: str, homes: set[str]) -> str | None:
    """The protected folder `path` is in, as given or with its symlinks resolved; None if none."""
    for seen in {posixpath.normpath(path).lower(), os.path.realpath(path).lower()}:
        for home in homes:
            for folder in PRIVACY_DIRS:
                prefix = posixpath.join(home, folder).lower()
                if seen == prefix or seen.startswith(prefix + "/"):
                    return folder
    return None


def render(template: bytes, *, home: str, cleave_dir: str, user: str, tmpdir: str,
           path: str = DEFAULT_PATH, lang: str = DEFAULT_LANG) -> bytes:
    for name, value in (("home", home), ("cleave_dir", cleave_dir), ("tmpdir", tmpdir)):
        if not posixpath.isabs(value):
            raise PlistError(f"{name} must be an absolute path, got {value!r}")
    values = {"HOME": home, "CLEAVE_DIR": cleave_dir, "USER": user,
              "PATH": path, "LANG": lang, "TMPDIR": tmpdir}
    homes = {posixpath.normpath(home), os.path.realpath(home)}
    plist = _fill(plistlib.loads(template), values)
    for s in _strings(plist):
        left = _PLACEHOLDER.search(s)
        if left:
            raise PlistError(f"unknown placeholder {left.group(0)} in {s!r}")
        for part in s.split(":"):   # PATH's entries, one by one
            if not posixpath.isabs(part):
                continue   # not a path: realpath would resolve it against the current directory
            folder = _privacy_folder(part, homes)
            if folder:
                raise PlistError(f"macOS privacy protection blocks launchd agents from ~/{folder}: {part!r}")
    return plistlib.dumps(plist, sort_keys=False)


def _write(out: Path, data: bytes) -> None:
    """`data` as `out` in one step: a temp file next to it, then os.replace; no temp file is left."""
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Render the launchd plist for {LABEL} from {TEMPLATE_PATH}.")
    parser.add_argument("--out", required=True, help="where to write the rendered plist")
    parser.add_argument("--tmpdir", required=True, help='the per-user temp dir: "$(getconf DARWIN_USER_TEMP_DIR)"')
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--cleave-dir", default=str(REPO_ROOT))
    parser.add_argument("--user", default=getpass.getuser())
    parser.add_argument("--path", default=DEFAULT_PATH)
    args = parser.parse_args(argv)
    try:
        plist = render(TEMPLATE_PATH.read_bytes(), home=args.home, cleave_dir=args.cleave_dir,
                       user=args.user, tmpdir=args.tmpdir, path=args.path)
    except PlistError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        _write(Path(args.out), plist)
    except OSError as exc:
        print(f"error: cannot write {args.out}: {exc.strerror or exc}", file=sys.stderr)
        return 2
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
