"""The Telegram language context, tr(), count(), the model-text marks, and the Ukrainian catalogue's checks."""
from __future__ import annotations

import ast
import asyncio
import re
import unicodedata
from collections import Counter
from pathlib import Path
from string import Formatter

import pytest

from orchestrator import tg_i18n
from orchestrator.tg_i18n import (
    N_,
    count,
    current_language,
    detail,
    fill,
    marked_texts,
    model_text,
    telegram_language,
    tr,
)
from orchestrator.tg_uk import NOUNS, TEXTS

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [*(ROOT / d for d in ("orchestrator", "integrations", "health", "dashboard")), ROOT / "main.py"]


@pytest.fixture
def catalogue(monkeypatch):
    monkeypatch.setitem(tg_i18n.TEXTS, "Posted on #{number}.", "Опубліковано в #{number}.")
    monkeypatch.setitem(tg_i18n.TEXTS, "Nothing was done.", "Нічого не зроблено.")
    monkeypatch.setitem(tg_i18n.NOUNS, "file", ("файл", "файли", "файлів"))


def test_english_is_the_default():
    assert current_language() == "en"


def test_language_is_set_for_the_block_only():
    with telegram_language("uk"):
        assert current_language() == "uk"
        with telegram_language("de"):  # nothing Cleave writes in: English
            assert current_language() == "en"
        assert current_language() == "uk"
    assert current_language() == "en"


async def test_each_task_has_its_own_language():
    seen = {}

    async def work(name, language):
        with telegram_language(language):
            await asyncio.sleep(0)
            seen[name] = current_language()

    await asyncio.gather(work("a", "uk"), work("b", "en"))
    assert seen == {"a": "uk", "b": "en"}


def test_tr(catalogue):
    assert tr("Posted on #{number}.", number=7) == "Posted on #7."
    with telegram_language("uk"):
        assert tr("Posted on #{number}.", number=7) == "Опубліковано в #7."
        assert tr("Not in the catalogue {x}.", x=1) == "Not in the catalogue 1."
        assert tr("Nothing was done.") == "Нічого не зроблено."
        assert tr("a {brace} kept without values") == "a {brace} kept without values"


def test_n_is_the_identity():
    assert N_("Nothing was done.") == "Nothing was done."


@pytest.mark.parametrize("n, english, ukrainian", [
    (0, "0 files", "0 файлів"), (1, "1 file", "1 файл"), (2, "2 files", "2 файли"), (5, "5 files", "5 файлів"),
    (11, "11 files", "11 файлів"), (12, "12 files", "12 файлів"), (21, "21 files", "21 файл"),
    (22, "22 files", "22 файли"), (25, "25 files", "25 файлів"), (111, "111 files", "111 файлів"),
])
def test_count(catalogue, n, english, ukrainian):
    assert count(n, "file") == english
    with telegram_language("uk"):
        assert count(n, "file") == ukrainian


def test_count_irregular_plural():
    assert count(1, "reply", "replies") == "1 reply"
    assert count(3, "reply", "replies") == "3 replies"


def test_model_text_is_unmarked_in_english():
    assert model_text("A finding") == "A finding"


def test_marks_round_trip():
    with telegram_language("uk"):
        message = f"1. {model_text('Null check')} · 2. {model_text('Race')} · {model_text('Null check')}"
    assert marked_texts(message) == ["Null check", "Race"]
    assert fill(message, {"Null check": "Перевірка на null"}) == "1. Перевірка на null · 2. Race · Перевірка на null"


def test_a_text_cannot_forge_a_mark():
    with telegram_language("uk"):
        message = model_text("evil  tail  x")
    assert marked_texts(message) == ["evil  tail  x"]
    assert fill("stray  mark ", {}) == "stray  mark "


def test_detail(catalogue):
    assert detail("Nothing was done.") == "Nothing was done."
    assert detail("disk full") == "disk full"
    with telegram_language("uk"):
        assert detail("Nothing was done.") == "Нічого не зроблено."
        assert marked_texts(detail("disk full")) == ["disk full"]


# --- The catalogue -------------------------------------------------------------------------------------------------

def _calls() -> tuple[set[str], set[str], list[str]]:
    """The literals passed to tr()/N_(), the nouns passed to count(), and the tr()/N_() calls given an f-string."""
    texts, nouns, fstrings = set(), set(), []
    for source in SOURCES:
        files = [source] if source.is_file() else source.rglob("*.py")
        for path in files:
            if "tg_uk" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                    continue
                name, args = node.func.id, node.args
                if name in ("tr", "N_") and args:
                    if isinstance(args[0], ast.JoinedStr):
                        fstrings.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                    elif isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                        texts.add(args[0].value)
                elif name == "count" and len(args) >= 2 and isinstance(args[1], ast.Constant):
                    nouns.add(args[1].value)
    return texts, nouns, fstrings


def test_tr_never_takes_an_f_string():
    assert _calls()[2] == []


def test_every_text_has_ukrainian():
    texts, nouns, _ = _calls()
    assert sorted(texts - TEXTS.keys()) == []
    assert sorted(nouns - NOUNS.keys()) == []


def test_every_ukrainian_text_is_used():
    texts, nouns, _ = _calls()
    assert sorted(TEXTS.keys() - texts) == []
    assert sorted(NOUNS.keys() - nouns) == []


def _fields(template: str) -> Counter:
    return Counter((name, spec) for _, name, spec, _ in Formatter().parse(template) if name is not None)


def _kept(template: str) -> Counter:
    """What a translation must keep: texts in double quotes, emoji, and numbers outside placeholders."""
    plain = re.sub(r"\{[^{}]*\}", "", template)
    return Counter(re.findall(r'"[^"\n]*"', template)
                   + [c for c in template if unicodedata.category(c) == "So"]
                   + [f"#{n}" for n in re.findall(r"\d+", plain)])


def test_catalogue_keeps_quotes_emoji_digits_and_placeholders():
    bad = []
    for key, value in TEXTS.items():
        if not value.strip():
            bad.append((key, "empty"))
        if _fields(key) != _fields(value):
            bad.append((key, "placeholders"))
        missing = _kept(key) - _kept(value)
        if missing:
            bad.append((key, sorted(missing)))
    for noun, forms in NOUNS.items():
        if len(forms) != 3 or not all(forms):
            bad.append((noun, "forms"))
    assert bad == []
