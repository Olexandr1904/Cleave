"""Ukrainian for the skill flow: a run's messages and buttons (orchestrator/skillflow/messages.py), the comparison's
(orchestrator/skillflow/compare_messages.py), the answers to replies and taps on them
(orchestrator/skillflow/replies.py) and the command handler's two skill-flow answers. The sides keep their names in
Latin, as in the config: "staged", "skill"."""
from __future__ import annotations

TEXTS: dict[str, str] = {
    # A run's messages
    "{ticket} · skill run ({skill})": "{ticket} · запуск skill ({skill})",
    "… (cut: see the notes directory for the session's files)": "… (обрізано: файли сесії — у теці нотаток)",
    "Approve": "Схвалити",
    "Drop": "Скасувати",
    "Reply to answer, or tap Approve to say ok.":
        "Дайте відповідь на це повідомлення або натисніть «Схвалити», щоб погодитися.",
    "Reply with new information to continue, or tap Drop.":
        "Щоб продовжити, дайте відповідь із новою інформацією або натисніть «Скасувати».",
    "Approve tries this step again; a reply goes to the session.":
        "«Схвалити» повторює цей крок; відповідь іде в сесію.",
    "{header} — stopped": "{header} — зупинено",
    "{header} — stopped: {why}": "{header} — зупинено: {why}",
    "{header} — {outcome}, needs you": "{header} — {outcome}, потрібна ваша участь",
    "Cleave goes on to the commit, its gates and the draft PR.":
        "Cleave переходить до коміту, його перевірок і чернетки PR.",
    "{header} — {outcome} (no change)": "{header} — {outcome} (без змін)",
    "No worktree at {path}.": "Немає worktree у {path}.",
    "A reply resumes the session.": "Відповідь продовжить сесію.",
    "{header} — draft PR opened: {url}": "{header} — чернетку PR відкрито: {url}",
    "Gates: {results}": "Перевірки: {results}",
    "none configured": "не налаштовано",
    "{header} — deferred until {when} UTC ({reason})": "{header} — відкладено до {when} UTC ({reason})",
    "{header} — interrupted by a restart at the {phase} step": "{header} — перервано перезапуском на кроці {phase}",
    "Approve continues it; Drop ends it.": "«Схвалити» продовжує його; «Скасувати» завершує.",
    "{header} — failed: {reason}": "{header} — збій: {reason}",
    "{header} — worktree left at {path}: {why}": "{header} — worktree залишено в {path}: {why}",
    "Notes: {path}": "Нотатки: {path}",

    # Answers to a reply or a tap on a run's message
    "This skill run is not known; nothing was done.": "Цей запуск skill невідомий; нічого не зроблено.",
    "This skill run cannot be read; nothing was done.":
        "Цей запуск skill не вдається прочитати; нічого не зроблено.",
    "That is not this run's latest message; nothing was done. Use the newest one.":
        "Це не останнє повідомлення цього запуску; нічого не зроблено. Скористайтеся найновішим.",
    "This run's newest message has not gone out yet; nothing was done. Wait for it.":
        "Найновіше повідомлення цього запуску ще не надіслано; нічого не зроблено. Зачекайте на нього.",
    "Reply to a skill run's own message to act on it; nothing was done.":
        "Щоб це спрацювало, надішліть це як відповідь (Reply) на повідомлення запуску skill; нічого не зроблено.",
    "Unknown action; nothing was done.": "Невідома дія; нічого не зроблено.",
    "The skill flow is off on this daemon; nothing was done.":
        "Skill flow на цьому демоні вимкнено; нічого не зроблено.",
    "Internal error ({name}); nothing was done.": "Внутрішня помилка ({name}); нічого не зроблено.",
    "Dropped. The branch {branch} is on the forge; Cleave pushes nothing more.":
        "Скасовано. Гілка {branch} є на GitHub; Cleave більше нічого не пушить.",
    "Dropped. A push of {branch} was started and may have reached the forge; Cleave pushes nothing more.":
        "Скасовано. Пуш {branch} було розпочато, і він міг дійти до GitHub; Cleave більше нічого не пушить.",
    "Dropped. Nothing was pushed.": "Скасовано. Нічого не запушено.",
    "Got it — the session continues with your reply.": "Прийнято — сесія продовжується з вашою відповіддю.",
    "Approved — the session goes on.": "Схвалено — сесія продовжується.",
    "Continuing from the {phase} step.": "Продовжую з кроку {phase}.",
    "The run is finished ({state}); nothing was done.": "Запуск завершено ({state}); нічого не зроблено.",
    "The run is working ({state}); nothing was done. Reply again when it stops.":
        "Запуск працює ({state}); нічого не зроблено. Відповідайте знову, коли він зупиниться.",
    "Your reply from {when} UTC is more than 15 minutes old; nothing was done. Reply again if it still holds.":
        "Вашій відповіді від {when} UTC більше 15 хвилин; нічого не зроблено. Відповідайте знову, якщо вона ще"
        " актуальна.",

    # integrations/telegram/command_handler.py
    "Reply not handled: the skill-run store could not be read; nothing was done.":
        "Відповідь не оброблено: не вдалося прочитати сховище запусків skill; нічого не зроблено.",

    # The comparison message
    "{ticket} · comparison: {summary}": "{ticket} · порівняння: {summary}",
    "Staged: {side}": "Staged: {side}",
    "Skill ({skill}): {side}": "Skill ({skill}): {side}",
    "PR #{number} {url} · {state} · {time} · {tokens_in} in / {tokens_out} out tokens":
        "PR #{number} {url} · {state} · {time} · токенів: {tokens_in} на вході / {tokens_out} на виході",
    "no PR · {state}{outcome} · {time} · {tokens_in} in / {tokens_out} out tokens":
        "без PR · {state}{outcome} · {time} · токенів: {tokens_in} на вході / {tokens_out} на виході",
    "Tap the better side. A reply to this message is kept as the verdict's note.":
        "Оберіть кращий варіант. Відповідь на це повідомлення збережеться як примітка до вердикту.",
    "{minutes} min": "{minutes} хв",
    "{hours} h {minutes:02d} min": "{hours} год {minutes:02d} хв",

    # Buttons
    "Staged": "Staged",
    "Skill": "Skill",
    "Neither": "Жоден",
    "Retry": "Повторити",

    # Side and verdict names
    "staged": "staged",
    "skill": "skill",
    "neither": "жоден",

    # A side moved on, a stuck verdict and its steps
    "{ticket} · comparison: the {side} side moved on after your verdict; nothing was done. A new message comes when"
    " both sides are at rest.":
        "{ticket} · порівняння: бік «{side}» пішов далі після вашого вердикту; нічого не зроблено. Нове повідомлення"
        " прийде, коли обидва боки зупиняться.",
    "{ticket} · verdict {verdict} not finished: {step} failed: {reason}\nRetry continues from that step.":
        "{ticket} · вердикт «{verdict}» не завершено: збій кроку «{step}»: {reason}\n«Повторити» продовжить"
        " із цього кроку.",
    "marking PR #{number} ready": "позначення PR #{number} як готового",
    "retitling PR #{number}": "перейменування PR #{number}",
    "commenting on PR #{number}": "коментування PR #{number}",
    "closing PR #{number}": "закриття PR #{number}",
    "tracker writes": "записування в трекер",
    "ending the skill run": "завершення запуску skill",
    "ending the staged workspace": "завершення робочого простору staged",

    # The verdict's closing message
    "{ticket} · verdict: {verdict}": "{ticket} · вердикт: {verdict}",
    "Ready for review: PR #{number} {url}": "Готовий до рев'ю: PR #{number} {url}",
    "Closed: PR #{number}": "Закрито: PR #{number}",
    "Both PRs stay drafts.": "Обидва PR лишаються чернетками.",
    'Tracker: the "PR opened" comment and the move to review are made.':
        'Трекер: коментар "PR opened" і перехід на рев\'ю зроблено.',
    "Tracker: {problem}": "Трекер: {problem}",
    "Tracker: none configured.": "Трекер: не налаштовано.",
    "Staged workspace: {state}": "Робочий простір staged: {state}",
    "Staged workspace: not found.": "Робочий простір staged: не знайдено.",
    "Skill run: dropped.": "Запуск skill: скасовано.",
    "Skill run: kept; its worktree is removed.": "Запуск skill: збережено; його worktree видалено.",
    "Skill run: {state}; left as it is.": "Запуск skill: {state}; залишено як є.",

    # Answers to a reply or a tap on a comparison's message, and on a decided run's
    "Kept as the comparison's note.": "Збережено як примітку до порівняння.",
    "This comparison is not known; nothing was done.": "Це порівняння невідоме; нічого не зроблено.",
    "The comparison has its verdict ({verdict}); nothing was done.":
        "Порівняння вже має вердикт ({verdict}); нічого не зроблено.",
    "The verdict is being carried out; nothing was done.": "Вердикт уже виконується; нічого не зроблено.",
    "The verdict ({verdict}) is stuck; nothing was done. The Retry button on its newest message continues it.":
        "Вердикт ({verdict}) застряг; нічого не зроблено. Кнопка «Повторити» на його найновішому повідомленні"
        " продовжить його.",
    "The comparison's newest message has not gone out yet; nothing was done. Wait for it.":
        "Найновіше повідомлення порівняння ще не надіслано; нічого не зроблено. Зачекайте на нього.",
    "That is not the comparison's newest message; nothing was done. Use the newest one.":
        "Це не найновіше повідомлення порівняння; нічого не зроблено. Скористайтеся найновішим.",
    "That side has no PR; nothing was done.": "У цього боку немає PR; нічого не зроблено.",
    "Verdict: {verdict}. Cleave marks PR #{number} ready and makes the tracker writes for it{closing}.":
        "Вердикт: {verdict}. Cleave позначає PR #{number} готовим і робить для нього записи в трекер{closing}.",
    ", and closes PR #{number}": ", а також закриває PR #{number}",
    "Verdict: neither. Both PRs stay drafts; both sides end.":
        "Вердикт: жоден. Обидва PR лишаються чернетками; обидва боки завершуються.",
    "Retrying the verdict from the step that failed.": "Повторюю вердикт із кроку, на якому стався збій.",
    "This run's comparison has its verdict; nothing was done.":
        "Порівняння цього запуску вже має вердикт; нічого не зроблено.",
}

NOUNS: dict[str, tuple[str, str, str]] = {}
