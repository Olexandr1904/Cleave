"""The reviewer in Ukrainian: the review message (its language and translations fixed in the run before its first part
goes out), lines, answers, `text N` and edit results; GitHub text stays English."""
from __future__ import annotations

from datetime import timedelta

from orchestrator.reviewer import runner as runner_module
from orchestrator.reviewer.chat import ReviewerChat
from orchestrator.reviewer.lines import job_failed_line, poll_failed_line
from orchestrator.reviewer.messages import MESSAGE_LIMIT, message_parts
from orchestrator.reviewer.report import parse_report
from orchestrator.reviewer.report_edit import split_at_file_line
from orchestrator.tg_i18n import fill, marked_texts, model_text, telegram_language
from tests.unit import test_reviewer_replies as replies_tests
from tests.unit import test_reviewer_runner as runner_tests
from tests.unit import test_reviewer_small_jobs as small_tests
from tests.unit.test_reviewer_messages import TG
from tests.unit.test_reviewer_report import HEAD, REPORT

HINT = 'Відповідайте на це повідомлення: "post 1 2 4 approve" · "text 2" · "edit 2: …" · "review again"'


def _translating(asked: list):
    async def translations(texts):
        asked.append(list(texts))
        return {t: f"УК {t}" for t in texts}
    return translations


def _ukrainian_runner(rig, language="uk"):
    rig.runner._language_of = lambda project_id: language


def _expected_parts(rig, job, language):
    """The parts the runner sends for the job's saved run, rendered in `language` with the run's translations."""
    run = rig.store.run(job.id)
    with telegram_language(language):
        text = runner_module._message(rig.store.job(job.id), run)
    return message_parts(fill(text, run.translations), limit=MESSAGE_LIMIT)


async def test_review_message_in_ukrainian(tmp_path, monkeypatch):
    rig = runner_tests.Rig(tmp_path, monkeypatch)
    _ukrainian_runner(rig)
    asked = []
    rig.chat.translations = _translating(asked)
    job = rig.add_job()

    await rig.runner.run_job(job)

    [text] = rig.chat.texts
    assert text.startswith("[acme/app] Рев'ю PR #1712 \"Fix cart sync\" · ACME-1234\n")
    assert HINT in text and text.endswith(runner_tests.URL)
    for n, line in TG.items():
        assert f"УК {line}" in text
    assert "УК Fix cart sync" not in text  # a PR title is a human's: kept as written
    assert "" not in text and "" not in text
    [spans] = asked  # one translation for the whole message
    assert set(TG.values()) <= set(spans)
    run = rig.store.run(job.id)
    assert run.language == "uk" and run.translations == {t: f"УК {t}" for t in spans}
    assert run.message_sent


async def test_a_failed_translation_still_sends_the_message(tmp_path, monkeypatch):
    rig = runner_tests.Rig(tmp_path, monkeypatch)
    _ukrainian_runner(rig)  # the fake chat's translations() returns {}: as a failed translation does
    job = rig.add_job()

    await rig.runner.run_job(job)

    [text] = rig.chat.texts
    assert HINT in text and TG[1] in text
    assert rig.store.run(job.id).message_sent


async def test_deliver_keeps_the_language_of_the_parts_already_out(tmp_path, monkeypatch):
    rig = runner_tests.Rig(tmp_path, monkeypatch)
    rig.write = lambda paths: runner_tests.write_review(paths, report=runner_tests.LONG_REPORT, tg={})
    _ukrainian_runner(rig)
    asked = []
    rig.chat.translations = _translating(asked)
    sends, calls = rig.chat.send, []

    async def second_part_fails(text, **anchor):
        calls.append(text)
        if len(calls) == 2:
            raise RuntimeError("telegram down")
        return await sends(text, **anchor)

    rig.chat.send = second_part_fails
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.store.run(job.id).message_ids == [100]

    _ukrainian_runner(rig, "en")  # the operator switched the project to English since
    rig.t = runner_tests.T0 + timedelta(minutes=1)
    await rig.runner.resend_unsent()

    parts = _expected_parts(rig, job, "uk")
    assert len(parts) >= 3 and rig.chat.texts == parts  # every part Ukrainian, none sent twice
    assert len(asked) == 1  # translated once, before the first part
    run = rig.store.run(job.id)
    assert run.message_sent and run.language == "uk"


async def test_a_run_from_before_languages_stays_english(tmp_path, monkeypatch):
    rig = runner_tests.Rig(tmp_path, monkeypatch)
    rig.write = lambda paths: runner_tests.write_review(paths, report=runner_tests.LONG_REPORT, tg={})
    sends, calls = rig.chat.send, []

    async def second_part_fails(text, **anchor):
        calls.append(text)
        if len(calls) == 2:
            raise RuntimeError("telegram down")
        return await sends(text, **anchor)

    rig.chat.send = second_part_fails
    job = rig.add_job()
    await rig.runner.run_job(job)
    run = rig.store.run(job.id)
    run.language = ""  # saved before this change
    rig.store.save_run(run)

    _ukrainian_runner(rig)
    rig.t = runner_tests.T0 + timedelta(minutes=1)
    await rig.runner.resend_unsent()

    assert rig.chat.texts == runner_tests.long_parts()


async def test_job_lines_take_the_project_language(tmp_path, monkeypatch):
    rig = runner_tests.Rig(tmp_path, monkeypatch)
    _ukrainian_runner(rig)
    rig.write = lambda paths: None  # no report written
    job = rig.add_job()

    await rig.runner.run_job(job)

    assert rig.state(job) == ("failed", "no report written")  # stored in English
    assert rig.chat.texts == ["[acme/app] #1712: збій (рев'ю) — звіт не записано"]


def test_a_line_names_a_reason_it_does_not_know_for_translation():
    with telegram_language("uk"):
        line = poll_failed_line("acme/app", "readiness exited 2", 5)
        assert line.startswith("[acme/app] опитування не вдалося: ")
        assert marked_texts(line) == ["readiness exited 2"]
        assert marked_texts(job_failed_line("acme/app", 7, "review", "the session failed")) == []


async def test_text_n_in_ukrainian_with_the_github_note(tmp_path):
    rig = replies_tests.Rig(tmp_path)
    rig.replies._language_of = lambda project_id: "uk"
    asked = []

    async def localize(text):
        asked.append(marked_texts(text))
        return fill(text, {t: f"УК {t}" for t in marked_texts(text)})

    rig.chat.localize = localize
    item = {i.number: i for i in parse_report(REPORT, HEAD).items}[2]

    await rig.reply("text 2")

    [text] = rig.texts()
    assert text.startswith("#2 · app/src/main/SyncWorker.kt:41 (зміщується на 43)\n**УК Retry without backoff**\n\nУК ")
    assert text.endswith("\n\n(На GitHub піде англійський текст.)")
    before, after = split_at_file_line(item.body)
    assert asked == [[item.title, "\n".join(before + after).strip()]]  # one translation: the title and the body
    assert rig.store.run(rig.job.id).report == parse_report(REPORT, HEAD).to_dict()  # nothing stored changes


async def test_answers_take_the_anchored_jobs_project_language(tmp_path):
    rig = replies_tests.Rig(tmp_path, parser=None)
    seen = []
    rig.replies._language_of = lambda project_id: seen.append(project_id) or "uk"

    await rig.reply("whatever this means")
    await rig.reply("text 99")

    assert seen and set(seen) <= {"", "acme"} and "acme" in seen
    assert rig.texts() == [
        'Не зрозумів; нічого не зроблено. Відповідайте, наприклад, "post 1 2 4 approve", "text 2", "edit 2: …",'
        ' "draft 7" або "review again".',
        "Невідомий номер: 99. Нічого не зроблено.",
    ]


async def test_edit_in_ukrainian_keeps_github_text_english(tmp_path):
    rig = small_tests.Rig(tmp_path, content="Use exponential backoff.")
    rig.small._language_of = lambda project_id: "uk"
    review_run = rig.store.run(rig.review.id)
    review_run.language, review_run.translations = "uk", {"retry without backoff": "повтор без затримки"}
    rig.store.save_run(review_run)
    instruction = "згадай експоненційну затримку"

    job = await rig.run(instruction=instruction)

    assert job.state == "done"
    [kw] = rig.llm.calls
    assert "Rewrite the English text" in kw["prompt"] and "повтор без затримки" not in kw["prompt"]
    assert "Retry without backoff" in kw["prompt"]
    edited = small_tests.item(rig.report(), 2)
    assert edited.body.endswith("Use exponential backoff.")  # what GitHub gets: the model's English
    [(text, _)] = rig.chat.sent
    assert text.startswith("[acme/app] #1712: новий текст (нічого не опубліковано)\n\n#2 · ")
    assert 'Відповідайте "post 2", щоб опублікувати, або знову "edit 2: …".' in text
    assert text.endswith("(На GitHub піде англійський текст.)")


async def test_poll_lines_take_the_repos_project_language(tmp_path):
    from orchestrator.reviewer.readiness import ReadinessError
    from tests.unit import test_reviewer_service as service_tests

    r, forge = service_tests.review_pr()
    c = service_tests.ctx(tmp_path, r, forge)
    rig = service_tests.Rig(tmp_path, c)
    asked = []
    rig.service._language_of = lambda project_id: asked.append(project_id) or "uk"
    await rig.service.poll(c)
    r.fail = ReadinessError("exit 3: boom")

    await rig.service.poll(c)

    assert set(asked) == {"acme"}
    [line] = rig.lines
    assert line.startswith("[acme/app] команда readiness не спрацювала: ")
    assert marked_texts(line) == ["exit 3: boom"]
    assert rig.state().reason == "exit 3: boom"  # stored in English


async def test_reviewer_chat_localizes_with_its_translator():
    class Translator:
        async def translations(self, texts, language):
            return {t: f"{language}:{t}" for t in texts}

    chat = ReviewerChat(notifier=None, chat_id="1", translator=Translator())
    with telegram_language("uk"):
        assert await chat.localize(f"a {model_text('b')}") == "a uk:b"
        assert await chat.translations(["x"]) == {"x": "uk:x"}
    assert await ReviewerChat(notifier=None, chat_id="1").translations(["x"]) == {}
