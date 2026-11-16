"""The Telegram language on the dashboard: `telegram.language` in global.yaml and in each project's project.yaml.

A save rewrites only the `language:` line of the file's `telegram:` block (adding it, or the block, when missing),
checks the result by reading the file back, and applies to the running daemon at once: every Telegram message reads
the language from the live config (orchestrator/tg_i18n.py)."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from config.schemas import TELEGRAM_LANGUAGES
from dashboard.reviews_api import _cross_site
from orchestrator.tg_i18n import project_language

_TELEGRAM = re.compile(r"telegram:[ \t]*(#.*)?")  # the block's own line
_LANGUAGE = re.compile(r"([ \t]+)language:")
_CHILD = re.compile(r"([ \t]+)\S")


class YamlEditError(ValueError):
    """The file's `telegram:` section is not a block Cleave can edit line by line."""


def set_yaml_language(path: Path, language: str) -> None:
    """Set `telegram.language` in the YAML file at `path`; every other line stays as it is. Raises YamlEditError
    (the file untouched) when its telegram section is not a block mapping, or the edit would read back otherwise."""
    if language not in TELEGRAM_LANGUAGES:
        raise ValueError(f"language must be one of {TELEGRAM_LANGUAGES} (got {language!r})")
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    entry = f'language: "{language}"\n'
    start = next((i for i, line in enumerate(lines) if _TELEGRAM.fullmatch(line.rstrip("\r\n"))), None)
    if start is None:
        if any(re.match(r"telegram[ \t]*:", line) for line in lines):
            raise YamlEditError("the telegram section is not a block; edit the file by hand")
        new = text + ("\n" if text and not text.endswith("\n") else "") + "telegram:\n  " + entry
    else:
        end = start + 1  # the block: its indented lines, blank lines and comments, up to the next top-level line
        while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t#"):
            end += 1
        block = range(start + 1, end)
        found = next((i for i in block if _LANGUAGE.match(lines[i])), None)
        if found is not None:
            lines[found] = _LANGUAGE.match(lines[found])[1] + entry
        else:
            child = next((m for i in block if not lines[i].lstrip().startswith("#")
                          and (m := _CHILD.match(lines[i]))), None)
            lines.insert(start + 1, (child[1] if child else "  ") + entry)
        new = "".join(lines)
    _check(text, new, language)
    _write_atomic(path, new)


def _check(old: str, new: str, language: str) -> None:
    """`new` must read as `old` with telegram.language set, and as nothing else."""
    try:
        before, after = yaml.safe_load(old) or {}, yaml.safe_load(new)
    except yaml.YAMLError as e:
        raise YamlEditError(f"the file does not read as YAML after the edit: {e}") from None
    if not isinstance(before, dict) or not isinstance(before.get("telegram") or {}, dict):
        raise YamlEditError("the telegram section is not a mapping; edit the file by hand")
    expected = {**before, "telegram": {**(before.get("telegram") or {}), "language": language}}
    if after != expected:
        raise YamlEditError("the edit would change more than telegram.language; edit the file by hand")


def _write_atomic(path: Path, text: str) -> None:
    """Replace the file through a temp file beside it, keeping its mode."""
    mode = path.stat().st_mode & 0o7777
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def build_language_routes(global_config: Any, projects: dict[str, Any], config_dir: str) -> list[Route]:
    """GET and PUT /api/settings/telegram-language, on the daemon's live config."""

    async def get_language(request: Request) -> JSONResponse:
        return JSONResponse({
            "options": list(TELEGRAM_LANGUAGES),
            "global": project_language(global_config, projects, ""),
            "projects": [{
                "id": project_id,
                "name": project.config.project.name or project_id,
                "language": project.config.telegram.language,
                "effective": project_language(global_config, projects, project_id),
            } for project_id, project in sorted(projects.items())],
        })

    async def put_language(request: Request) -> JSONResponse:
        if _cross_site(request):
            return JSONResponse({"error": "cross-site request refused"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        language, project_id = body.get("language"), body.get("project_id")
        if language not in TELEGRAM_LANGUAGES:
            return JSONResponse({"error": f"language must be one of {list(TELEGRAM_LANGUAGES)}"}, status_code=400)
        if project_id is None:
            path, telegram = Path(config_dir) / "global.yaml", global_config.telegram
        elif project_id in projects:
            project = projects[project_id]
            path, telegram = Path(project.config_dir) / "project.yaml", project.config.telegram
        else:
            return JSONResponse({"error": f"no project {project_id!r}"}, status_code=404)
        try:
            set_yaml_language(path, language)
        except YamlEditError as e:
            return JSONResponse({"error": f"{path.name}: {e}"}, status_code=409)
        telegram.language = language
        return JSONResponse({"language": language,
                             "effective": project_language(global_config, projects, project_id or "")})

    return [
        Route("/api/settings/telegram-language", get_language, methods=["GET"]),
        Route("/api/settings/telegram-language", put_language, methods=["PUT"]),
    ]
