"""The language Cleave writes in Telegram: English (the default) or Ukrainian, per project.

A project picks it with `telegram.language` in its project.yaml (global.yaml's when it has none, else English).
Where a project's work starts, telegram_language() makes it the current language, a ContextVar: each asyncio task
has its own. Cleave's own texts stay English templates in the code and go through tr(), which reads the Ukrainian
template from the catalogue (orchestrator/tg_uk) when the current language is Ukrainian. Text a model wrote is
marked with model_text() and translated by a model where the message is sent (orchestrator/tg_translate.py).

Only text bound for Telegram goes through these: what is stored or shown elsewhere (GitHub, the report files, the
dashboard, workspace and reviewer state) stays English. A text stored in English and sent to Telegram too is marked
with N_() where it is written and passed to tr() where the Telegram text is made.
"""
from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from config.schemas import TELEGRAM_LANGUAGES
from orchestrator.tg_uk import NOUNS, TEXTS

ENGLISH = "en"
UKRAINIAN = "uk"

_language: ContextVar[str] = ContextVar("telegram_language", default=ENGLISH)

# Private-use characters around a text a model wrote, from model_text() until fill() replaces it.
_OPEN, _CLOSE = "", ""
_MARKED = re.compile(f"{_OPEN}([^{_OPEN}{_CLOSE}]*){_CLOSE}")


def project_language(global_config: Any, projects: Mapping[str, Any], project_id: str) -> str:
    """The project's language, else global.yaml's, else English. An unknown or empty project id gets the
    global one."""
    project = projects.get(project_id) if project_id else None
    language = (project.config.telegram.language if project is not None else "") or global_config.telegram.language
    return language if language in TELEGRAM_LANGUAGES else ENGLISH


def chat_language(global_config: Any, projects: Mapping[str, Any], chat_id: str) -> str:
    """The language of the projects whose default_chat_id is this chat, when they agree; else the global one."""
    languages = {project_language(global_config, projects, project_id) for project_id, project in projects.items()
                 if project.config.telegram.default_chat_id == chat_id}
    return languages.pop() if len(languages) == 1 else project_language(global_config, projects, "")


@contextmanager
def telegram_language(language: str) -> Iterator[None]:
    """Make `language` the current one until the block ends (English for anything Cleave does not write in)."""
    token = _language.set(language if language in TELEGRAM_LANGUAGES else ENGLISH)
    try:
        yield
    finally:
        _language.reset(token)


def current_language() -> str:
    return _language.get()


def tr(text: str, /, **values: Any) -> str:
    """`text`, an English str.format template, in the current language, filled with `values` (formatted only when
    values are given). A text the catalogue lacks stays English."""
    if _language.get() == UKRAINIAN:
        text = TEXTS.get(text, text)
    return text.format(**values) if values else text


def N_(text: str) -> str:  # noqa: N802 (gettext's name)
    """`text` unchanged: an English text stored or shown elsewhere as it is, listed for the catalogue, that
    tr() translates where Telegram gets it."""
    return text


def count(n: int, singular: str, plural: str = "") -> str:
    """`n` and the noun: "1 file", "3 files" ("3 replies" with `plural`); in Ukrainian "1 файл", "3 файли",
    "5 файлів"."""
    if _language.get() == UKRAINIAN and singular in NOUNS:
        return f"{n} {NOUNS[singular][_ukrainian_form(n)]}"
    return f"{n} {singular if n == 1 else plural or singular + 's'}"


def _ukrainian_form(n: int) -> int:
    """0 for 1, 21, 101…; 1 for 2–4, 22–24…; 2 for the rest (0, 5–20, 25…)."""
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return 0
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return 1
    return 2


def model_text(text: str) -> str:
    """`text`, which a model wrote, marked to be translated where the message is sent; unmarked in English, which
    is never translated. Marker characters already in it are dropped, so it cannot open or close a mark."""
    if _language.get() == ENGLISH:
        return text
    text = text.replace(_OPEN, "").replace(_CLOSE, "")
    return f"{_OPEN}{text}{_CLOSE}" if text else text


def detail(text: str) -> str:
    """A text Cleave composed at run time (a reason, an error's first line): the catalogue's Ukrainian when it
    holds the text as it is, else marked like a model's text."""
    if _language.get() == UKRAINIAN and text in TEXTS:
        return TEXTS[text]
    return model_text(text)


def marked_texts(message: str) -> list[str]:
    """The marked texts in `message`, in order, each once."""
    return list(dict.fromkeys(_MARKED.findall(message)))


def fill(message: str, translations: Mapping[str, str]) -> str:
    """`message` with each marked text replaced by its translation (itself when it has none), and no marker
    character left."""
    filled = _MARKED.sub(lambda m: translations.get(m[1], m[1]), message)
    return filled.replace(_OPEN, "").replace(_CLOSE, "")
