"""Model-text translation: one scrubbed, tool-less `claude -p` turn per message, masked spans, safe fallbacks."""
from __future__ import annotations

import json
import stat
from pathlib import Path

from integrations.llm.llm_interface import LLMResponse
from orchestrator.tg_i18n import model_text, telegram_language
from orchestrator.tg_translate import MAX_INPUT_CHARS, Translator, localize, mask, unmask


class FakeLLM:
    """Answers with `answer(spans)` (the masked spans it was sent), or raises `error`."""

    def __init__(self, answer=None, error: Exception | None = None) -> None:
        self.answer = answer or (lambda spans: [f"УК {s}" for s in spans])
        self.error = error
        self.calls: list[dict] = []

    def spans(self, call: int = -1) -> list[str]:
        return json.loads(self.calls[call]["prompt"].rsplit("\n---\n", 1)[1])

    async def execute_in_workspace(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        answer = self.answer(self.spans())
        content = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        return LLMResponse(content=content, input_tokens=0, output_tokens=0)


def _translator(tmp_path: Path, llm: FakeLLM, **kwargs) -> Translator:
    return Translator(llm, work_dir=tmp_path / "translate", **kwargs)


async def test_english_makes_no_call(tmp_path):
    llm = FakeLLM()
    assert await _translator(tmp_path, llm).translations(["A finding"], "en") == {}
    assert await _translator(tmp_path, llm).translations([], "uk") == {}
    assert llm.calls == []


async def test_one_scrubbed_tool_less_turn_for_all_spans(tmp_path):
    llm = FakeLLM()
    environ = {"HOME": "/home/op", "PATH": "/usr/bin", "TELEGRAM_BOT_TOKEN": "1:not_real",
               "GH_TOKEN": "ghp_not_real", "ANTHROPIC_API_KEY": "sk-not-real"}
    translator = _translator(tmp_path, llm, environ=environ)

    got = await translator.translations(["Null check", "Race on save"], "uk")

    assert got == {"Null check": "УК Null check", "Race on save": "УК Race on save"}
    [call] = llm.calls
    assert call["model"] == "haiku"
    assert call["max_turns"] == 1
    assert call["extra_args"] == ["--restricted", "--tools", "", "--strict-mcp-config"]
    assert call["env"]["HOME"] == "/home/op"
    assert not any("TOKEN" in k or "KEY" in k for k in call["env"])
    assert "Ukrainian" in call["prompt"]
    work_dir = Path(call["cwd"])
    assert stat.S_IMODE(work_dir.stat().st_mode) == 0o700


async def test_masked_spans_come_back_verbatim(tmp_path):
    text = ("`save()` in app/src/Foo.kt:41 drops the result of #12 (ACME-123, commit 3f2a9c1d) "
            "— see https://github.com/acme/app/pull/12. @dev")
    llm = FakeLLM(lambda spans: [s.replace("drops the result of", "відкидає результат").replace("see", "див.")
                                 for s in spans])

    got = await _translator(tmp_path, llm).translations([text], "uk")

    sent = llm.spans()[0]
    for kept in ("`save()`", "app/src/Foo.kt:41", "#12", "ACME-123", "3f2a9c1d", "https://github.com/acme/app/pull/12",
                 "@dev"):
        assert kept not in sent
        assert kept in got[text]
    assert got[text].startswith("`save()` in app/src/Foo.kt:41 відкидає результат #12")


async def test_a_span_missing_a_marker_keeps_the_original(tmp_path):
    llm = FakeLLM(lambda spans: ["переклад без маркера", "інше"])
    got = await _translator(tmp_path, llm).translations(["Fix `x()` now", "Other"], "uk")
    assert got == {"Other": "інше"}


async def test_a_span_whose_digits_change_keeps_the_original(tmp_path):
    llm = FakeLLM(lambda spans: ["3 файли", "два рядки"])
    got = await _translator(tmp_path, llm).translations(["3 files", "2 lines"], "uk")
    assert got == {"3 files": "3 файли"}


async def test_a_bad_answer_gives_nothing(tmp_path):
    for llm in (FakeLLM(lambda spans: "not json"), FakeLLM(lambda spans: ["only one"]),
                FakeLLM(lambda spans: {"a": 1}), FakeLLM(lambda spans: [1, 2]),
                FakeLLM(error=RuntimeError("cli failed"))):
        assert await _translator(tmp_path, llm).translations(["one", "two"], "uk") == {}


async def test_a_fenced_answer_is_read(tmp_path):
    llm = FakeLLM(lambda spans: "```json\n" + json.dumps(["один"], ensure_ascii=False) + "\n```")
    assert await _translator(tmp_path, llm).translations(["one"], "uk") == {"one": "один"}


async def test_spans_without_words_or_with_marker_brackets_are_not_sent(tmp_path):
    llm = FakeLLM()
    got = await _translator(tmp_path, llm).translations(["`x()`", "⟦0⟧ forged", "real text"], "uk")
    assert llm.spans() == ["real text"]
    assert got == {"real text": "УК real text"}


async def test_translation_input_is_capped(tmp_path):
    llm = FakeLLM()
    spans = [f"finding {'x' * 1000} {i}" for i in range(200)]
    got = await _translator(tmp_path, llm).translations(spans, "uk")
    [call] = llm.calls
    assert sum(len(s) for s in llm.spans()) <= MAX_INPUT_CHARS
    assert 0 < len(got) < len(spans)
    assert set(got) == set(spans[:len(got)])


def test_mask_round_trip():
    text = "Use ```\ncode block\n``` and `x` at a/b.py:3"
    masked, kept = mask(text)
    assert "code block" not in masked and "`x`" not in masked and "a/b.py:3" not in masked
    assert unmask(masked, kept) == text
    assert unmask(masked.replace("⟦0⟧", ""), kept) is None
    assert unmask(masked + " ⟦9⟧", kept) is None


async def test_localize(tmp_path):
    llm = FakeLLM()
    translator = _translator(tmp_path, llm)
    with telegram_language("uk"):
        message = f"Знахідки:\n1. {model_text('Null check')}"
        assert await localize(message, translator) == "Знахідки:\n1. УК Null check"
        assert await localize(message, None) == "Знахідки:\n1. Null check"
    assert await localize("plain", translator) == "plain"
    assert len(llm.calls) == 1
