"""Ukrainian for the reviewer's messages, lines and answers (orchestrator/reviewer)."""
from __future__ import annotations

TEXTS: dict[str, str] = {
    # messages.py: the review and re-check messages
    '[{scope}] Review of PR #{number} "{title}"{ticket}': '[{scope}] Рев\'ю PR #{number} "{title}"{ticket}',
    "head {head} · {files} · {candidates} → {confirmed} confirmed · {questions} · {preexisting} pre-existing"
    " · {dropped} dropped":
        "коміт {head} · {files} · {candidates} → підтверджено: {confirmed} · {questions}"
        " · наявних раніше: {preexisting} · відкинуто: {dropped}",
    "Suggested: {event} — {reason}": "Пропоную: {event} — {reason}",
    "Already posted at this head: {comments}": "Уже опубліковано на цьому коміті: {comments}",
    "Already posted at this head: {comments} and the Summary":
        "Уже опубліковано на цьому коміті: {comments} і Summary",
    "Dropped: {dropped}": "Відкинуто: {dropped}",
    "No findings.": "Зауважень немає.",
    'Reply to this message: "post 1 2 4 approve" · "text 2" · "edit 2: …" · "review again"':
        'Відповідайте на це повідомлення: "post 1 2 4 approve" · "text 2" · "edit 2: …" · "review again"',
    '[{scope}] Re-check of PR #{number} "{title}"': '[{scope}] Повторна перевірка PR #{number} "{title}"',
    "head {head} · {replies}": "коміт {head} · {replies}",
    'Reply to this message: "post replies {numbers}" · "text {first}"':
        'Відповідайте на це повідомлення: "post replies {numbers}" · "text {first}"',
    "No replies drafted.": "Чернеток відповідей немає.",
    "… {lines} — the full report is on the dashboard": "… ще {lines} — повний звіт на дашборді",

    # lines.py: names of job kinds and turn actions
    "review": "рев'ю",
    "recheck": "повторна перевірка",
    "edit": "редагування",
    "draft": "чернетка",

    # lines.py: the poll's and the runner's lines
    "[{scope}] #{number}: replies in {threads} thread(s) — handle them locally · {url}":
        "[{scope}] #{number}: відповіді в тредах ({threads}) — обробіть їх локально · {url}",
    "[{scope}] #{number}: your turn — {action} · {url}": "[{scope}] #{number}: ваша черга — {action} · {url}",
    "[{scope}] #{number} is large ({files} files) — no automatic review · {url}":
        "[{scope}] #{number} великий (файлів: {files}) — без автоматичного рев'ю · {url}",
    "[{scope}] readiness command failed: {reason}. Keeping the last board; next try in {minutes} min.":
        "[{scope}] команда readiness не спрацювала: {reason}. Лишаю останню дошку; наступна спроба через {minutes} хв.",
    "[{scope}] poll failed: {reason}. Keeping the last board; next try in {minutes} min.":
        "[{scope}] опитування не вдалося: {reason}. Лишаю останню дошку; наступна спроба через {minutes} хв.",
    "[{scope}] reviews are off: {doc} is not on {base}.": "[{scope}] рев'ю вимкнено: {doc} немає в {base}.",
    "[{scope}] #{number}: {kind} failed — {reason}": "[{scope}] #{number}: збій ({kind}) — {reason}",
    "[{scope}] #{number}: {kind} deferred — usage limit reached; next try at {at} UTC":
        "[{scope}] #{number}: відкладено ({kind}) — вичерпано ліміт використання; наступна спроба о {at} UTC",
    "[reviewer] claude is not logged in on the host. Log in from a GUI Terminal (run claude, then /login); reviews"
    " wait until then.":
        "[reviewer] claude на хості не залогінений. Увійдіть із графічного Terminal (запустіть claude, потім /login);"
        " доти рев'ю чекають.",
    '[{scope}] The "{section}" section of {doc} changed on the base branch: check that Cleave\'s Prepare still'
    " matches it.":
        '[{scope}] Розділ "{section}" у {doc} змінився в базовій гілці: перевірте, чи Prepare у Cleave досі йому'
        " відповідає.",
    "[{scope}] #{number}: worktree {path} was not removed — {reason}":
        "[{scope}] #{number}: worktree {path} не видалено — {reason}",

    # lines.py: answers about a job's state
    "Outdated: there is a review of a newer head. Nothing was done.":
        "Застаріло: є рев'ю новішого коміту. Нічого не зроблено.",
    "Outdated: there is a newer review. Nothing was done.": "Застаріло: є новіше рев'ю. Нічого не зроблено.",
    "Outdated: this report no longer matches the PR ({reason}). Nothing was done.":
        "Застаріло: цей звіт більше не відповідає PR ({reason}). Нічого не зроблено.",
    'This job ended {state}: only "review again" and "skip" work here. Nothing was done.':
        'Ця задача завершилась зі станом {state}: тут діють лише "review again" і "skip". Нічого не зроблено.',
    'Not understood; nothing was done. Reply like "post replies 1 3", "text 1" or "review again".':
        'Не зрозумів; нічого не зроблено. Відповідайте, наприклад, "post replies 1 3", "text 1" або "review again".',
    'Not understood; nothing was done. Reply like "post 1 2 4 approve", "text 2", "edit 2: …", "draft 7" or'
    ' "review again".':
        'Не зрозумів; нічого не зроблено. Відповідайте, наприклад, "post 1 2 4 approve", "text 2", "edit 2: …",'
        ' "draft 7" або "review again".',

    # lines.py and posting.py: posting
    "PR #{number} is closed: nothing posted.": "PR #{number} закрито: нічого не опубліковано.",
    'Head moved ({old} → {new}): nothing posted. Reply "review again" for a review of the new head.':
        'Head змістився ({old} → {new}): нічого не опубліковано. Відповідайте "review again", щоб отримати рев\'ю'
        " нового коміту.",
    'You already submitted a review on this PR after the report ({when}): nothing posted. Add "anyway" to your'
    " reply to post regardless.":
        "Ви вже надіслали рев'ю цього PR після звіту ({when}): нічого не опубліковано. Щоб опублікувати попри це,"
        ' додайте до відповіді "anyway".',
    '#{number}: its text {problem}: nothing posted. Fix it with "edit {number}: …".':
        '#{number}: його текст {problem}: нічого не опубліковано. Виправте через "edit {number}: …".',
    "#{number}: its text {problem}: nothing posted. A re-check reply cannot be edited here: answer that thread on"
    " GitHub yourself.":
        "#{number}: його текст {problem}: нічого не опубліковано. Відповідь повторної перевірки тут не редагується:"
        " дайте відповідь у цьому треді на GitHub самі.",
    "Not posted on #{number}: {why}.": "Не опубліковано в #{number}: {why}.",
    "Nothing left to post on #{number} at {head}: what you named is already posted.":
        "У #{number} на {head} публікувати нічого: усе, що ви назвали, вже опубліковано.",
    'The Summary already landed at #{number} at {head}: add "no summary". Nothing was done.':
        'Summary вже опубліковано в #{number} на {head}: додайте "no summary". Нічого не зроблено.',
    "inline": "інлайн",
    "moved anchor": "прив'язку зміщено",
    "in the review body": "у тексті рев'ю",
    "already posted, skipped": "уже опубліковано, пропущено",
    "Posted on #{number} at {head} — {event}, {reviews}: {urls}":
        "Опубліковано в #{number} на {head} — {event}, {reviews}: {urls}",
    "Summary only": "Лише Summary",
    "Posted replies on #{number}:": "Відповіді в #{number} опубліковано:",
    "Partly posted replies on #{number} — {why}.": "Відповіді в #{number} опубліковано частково — {why}.",
    "Check the PR on GitHub before posting again: {url}": "Перевірте PR на GitHub, перш ніж публікувати знову: {url}",
    'Reply "{retry}" to post the rest.': 'Відповідайте "{retry}", щоб опублікувати решту.',
    "Nothing posted on #{number}: the replies tool has nothing to post, but GitHub does not show everything posted"
    " ({why}). Check the PR on GitHub: {url}":
        "У #{number} нічого не опубліковано: інструменту відповідей нічого публікувати, але GitHub показує не все як"
        " опубліковане ({why}). Перевірте PR на GitHub: {url}",
    "none": "жодного",
    "Partly posted on #{number} at {head}: batch {batches} of {total} landed ({urls}); landed {landed}; not landed"
    " {missing} — {why}.":
        "Опубліковано частково в #{number} на {head}: пройшла партія {batches} з {total} ({urls}); пройшли {landed};"
        " не пройшли {missing} — {why}.",
    "The tool also reports batch {batches} posted, which GitHub does not show: check the PR on GitHub.":
        "Інструмент також повідомляє, що опубліковано партію {batches}, але GitHub її не показує: перевірте PR на"
        " GitHub.",
    "Posting on #{number} ran, but GitHub does not show it as planned ({why}). Check the PR on GitHub before posting"
    " again: {url}":
        "Публікацію в #{number} виконано, але GitHub показує її не так, як планувалося ({why}). Перевірте PR на"
        " GitHub, перш ніж публікувати знову: {url}",
    "Already approved #{number} at {head}. Nothing was done.": "#{number} уже схвалено на {head}. Нічого не зроблено.",
    'An approve of #{number} at {head} may have landed: check the PR, then add "anyway" to your reply to approve'
    " again. Nothing was done.":
        "Схвалення #{number} на {head}, можливо, вже пройшло: перевірте PR, а щоб схвалити ще раз, додайте до"
        ' відповіді "anyway". Нічого не зроблено.',
    "Posting on #{number} ran, but GitHub could not be read to check it ({error}). Check the PR before posting"
    " again: {url}":
        "Публікацію в #{number} виконано, але GitHub не вдалося прочитати для перевірки ({error}). Перевірте PR,"
        " перш ніж публікувати знову: {url}",
    "Approved #{number} at {head} without a body: {url}": "Схвалено #{number} на {head} без тексту: {url}",
    "GitHub could not be read for #{number} ({error}): nothing posted.":
        "Не вдалося прочитати GitHub для #{number} ({error}): нічого не опубліковано.",
    "Posting on #{number} ran, but stopped on an internal error ({name}): it may have landed. Check the PR before"
    " posting again.":
        "Публікацію в #{number} запущено, але вона зупинилася на внутрішній помилці ({name}): можливо, щось уже"
        " опубліковано. Перевірте PR, перш ніж публікувати знову.",
    "Not a post command. Nothing was done.": "Це не команда публікації. Нічого не зроблено.",
    "Waiting for the first poll: nothing posted.": "Чекаю на перше опитування: нічого не опубліковано.",
    "The report changed while posting (an edit landed): look at it again and reply. Nothing was done.":
        "Звіт змінився під час публікації (застосовано правку): перегляньте його ще раз і відповідайте. Нічого не"
        " зроблено.",
    "Prepare's threads snapshot is not in this job's copy: nothing posted.":
        "Знімка тредів від Prepare немає в копії цієї задачі: нічого не опубліковано.",
    " · resolved (reply not seen on GitHub: resolved threads are not listed)":
        " · тред закрито (відповіді на GitHub не видно: закриті треди не показуються)",
    "{n}: answered {url}": "{n}: відповідь {url}",
    "{n}: resolved": "{n}: тред закрито",
    "{n}: replied {url}": "{n}: відповідь у треді {url}",
    "{n}: already posted": "{n}: уже опубліковано",
    "#{n}: your reply is in thread {thread}; resolve it on GitHub yourself.":
        "#{n}: ваша відповідь є в треді {thread}; закрийте його на GitHub самі.",
    "#{n}: thread {thread} was resolved, but the tool reports no reply in it; reply on GitHub yourself if it is"
    " missing.":
        "#{n}: тред {thread} закрито, але інструмент не повідомляє про відповідь у ньому; якщо її немає,"
        " відповідайте на GitHub самі.",
    "#{n}: thread {thread} was resolved, but the reply in it is not seen; reply on GitHub yourself if it is"
    " missing.":
        "#{n}: тред {thread} закрито, але відповіді в ньому не видно; якщо її немає, відповідайте на GitHub самі.",
    "The tool exited {code}, but GitHub shows everything posted.":
        "Інструмент завершився з кодом {code}, але GitHub показує, що все опубліковано.",
    "The tool's output was not understood ({unknown}), but GitHub shows everything posted.":
        "Вивід інструмента не вдалося розібрати ({unknown}), але GitHub показує, що все опубліковано.",
    'Reply "approve no summary" to approve without a body.':
        'Відповідайте "approve no summary", щоб схвалити без тексту.',

    # lines.py: edit and draft results, interrupted posts
    "[{scope}] #{pr}: new text (nothing posted)": "[{scope}] #{pr}: новий текст (нічого не опубліковано)",
    'Reply "post {n}" to post it, or "edit {n}: …" again.':
        'Відповідайте "post {n}", щоб опублікувати, або знову "edit {n}: …".',
    "[{scope}] #{pr}: drafted as a question (nothing posted)":
        "[{scope}] #{pr}: чернетка питання (нічого не опубліковано)",
    'Reply "post {n}" to post it.': 'Відповідайте "post {n}", щоб опублікувати.',
    "[{scope}] #{pr}: {kind} of {n} failed — {reason}": "[{scope}] #{pr}: збій ({kind} {n}) — {reason}",
    "(GitHub gets the English text.)": "(На GitHub піде англійський текст.)",
    '[{scope}] #{number}: a post of replies{named} was interrupted by a restart. Check the PR, then reply "post'
    ' replies …" with the ones GitHub does not show.':
        '[{scope}] #{number}: публікацію відповідей{named} перервав перезапуск. Перевірте PR, потім відповідайте'
        ' "post replies …" з тими, яких GitHub не показує.',
    '[{scope}] #{number}: a post was interrupted by a restart. Check the PR, then reply "post …" again with'
    ' "anyway": what landed is skipped.':
        '[{scope}] #{number}: публікацію перервав перезапуск. Перевірте PR, потім знову відповідайте "post …" з'
        ' "anyway": те, що вже пройшло, буде пропущено.',

    # grammar.py: check_command's answers and describe()
    'This is a review message: "post replies" works on a re-check message. Nothing was done.':
        'Це повідомлення рев\'ю: "post replies" працює в повідомленні повторної перевірки. Нічого не зроблено.',
    'This is a re-check message: reply "post replies 1 3". Nothing was done.':
        'Це повідомлення повторної перевірки: відповідайте "post replies 1 3". Нічого не зроблено.',
    'Edit one number at a time: "edit 2: …". Nothing was done.':
        'Редагуйте по одному номеру: "edit 2: …". Нічого не зроблено.',
    'Name a number: "text 2". Nothing was done.': 'Вкажіть номер: "text 2". Нічого не зроблено.',
    'Draft one number at a time: "draft 7". Nothing was done.':
        'Чернетки — по одному номеру: "draft 7". Нічого не зроблено.',
    'Name the replies: "post replies 1 3". Nothing was done.':
        'Вкажіть відповіді: "post replies 1 3". Нічого не зроблено.',
    "Nothing left to post: every confirmed finding is already posted at this head. Nothing was done.":
        "Публікувати нічого: усі підтверджені зауваження вже опубліковано на цьому коміті. Нічого не зроблено.",
    "Unknown number: {numbers}. Nothing was done.": "Невідомий номер: {numbers}. Нічого не зроблено.",
    "Unknown numbers: {numbers}. Nothing was done.": "Невідомі номери: {numbers}. Нічого не зроблено.",
    '#{n} was dropped: reply "draft {n}" first. Nothing was done.':
        '#{n} відкинуто: спершу відповідайте "draft {n}". Нічого не зроблено.',
    "#{n} is already posted at this head. Nothing was done.":
        "#{n} уже опубліковано на цьому коміті. Нічого не зроблено.",
    "#{n} is not a dropped number. Nothing was done.": "#{n} немає серед відкинутих. Нічого не зроблено.",
    '#{n} is drafted already: reply "text {n}". Nothing was done.':
        '#{n} уже має чернетку: відповідайте "text {n}". Нічого не зроблено.',
    "Nothing to post: name numbers or keep the Summary. Nothing was done.":
        "Публікувати нічого: вкажіть номери або залиште Summary. Нічого не зроблено.",
    "post {numbers} + {event} at {head}": "post {numbers} + {event} на {head}",
    ", no summary": ", без Summary",
    "{event} without a body at {head}": "{event} без тексту на {head}",
    "{event} with the Summary at {head}": "{event} із Summary на {head}",
    ", anyway": ", попри все (anyway)",
    "skip this head": "skip (пропустити цей коміт)",

    # replies.py: answers to replies and taps ("Reviewer mode is off on this daemon…" is in commands.py)
    "This repo is no longer reviewed; nothing was done.":
        "Рев'ю цього репозиторію більше не ведеться; нічого не зроблено.",
    "This line takes no replies; nothing was done.": "Цей рядок не приймає відповідей; нічого не зроблено.",
    'Only "review again" and "skip" work on this line; nothing was done.':
        'На цей рядок діють лише "review again" і "skip"; нічого не зроблено.',
    "claude is not logged in on the host: only exact replies work now; nothing was done.":
        "claude на хості не залогінений: зараз працюють лише точні команди; нічого не зроблено.",
    "claude is not logged in on the host: nothing was started.":
        "claude на хості не залогінений: нічого не запущено.",
    "The forge token is failing (see the dashboard): nothing posted.":
        "Токен GitHub не працює (див. дашборд): нічого не опубліковано.",
    "Already handled.": "Уже оброблено.",
    "Cancelled: nothing was done.": "Скасовано: нічого не зроблено.",
    "Expired: reply again; nothing was done.": "Час вийшов: відповідайте ще раз; нічого не зроблено.",
    "Understood: {command}. Correct?": "Я зрозумів так: {command}. Правильно?",
    "Yes": "Так",
    "No": "Ні",
    "Editing {n}: the new text comes in a few minutes; nothing is posted.":
        "Редагую {n}: новий текст буде за кілька хвилин; нічого не публікується.",
    "Drafting {n}: the comment comes in a few minutes; nothing is posted.":
        "Готую чернетку {n}: коментар буде за кілька хвилин; нічого не публікується.",
    "Checking before posting: {command}.": "Перевіряю перед публікацією: {command}.",
    "A review of #{number} at this head is already queued.": "Рев'ю #{number} на цьому коміті вже в черзі.",
    "#{number} is not on the board any more; nothing was done.": "#{number} уже немає на дошці; нічого не зроблено.",
    "Queued a review of #{number} at {head}.": "Рев'ю #{number} на {head} поставлено в чергу.",
    "Skipped #{number} at {head}: no automatic review of this head.":
        "Пропущено #{number} на {head}: автоматичного рев'ю цього коміту не буде.",
    "Internal error ({name}); nothing was done.": "Внутрішня помилка ({name}); нічого не зроблено.",
    "This reply is from {at}; send it again to act on it. Nothing was done.":
        "Ця відповідь від {at}; надішліть її ще раз, щоб її виконати. Нічого не зроблено.",
    "reply in thread {thread}, then resolve it": "відповідь у треді {thread}, потім тред закрити",
    "reply in thread {thread}, left unresolved": "відповідь у треді {thread}, тред лишається відкритим",
    "answer to review {review}": "відповідь на рев'ю {review}",
    "names no single thread or review: it cannot be posted": "не вказує на один тред чи рев'ю: опублікувати не можна",

    # report_edit.py: an item's note
    "moves to {line}": "зміщується на {line}",
    "outside the diff": "поза diff",

    # Reasons stored in English and shown in lines (runner.py, small_jobs.py, posting.py)
    "PR gone": "PR більше немає",
    "turn gone": "черга вже не ваша",
    "interrupted (daemon restarted)": "перервано (демон перезапустився)",
    "the session failed": "сесія завершилася помилкою",
    "no report written": "звіт не записано",
    "no replies plan written": "план відповідей не записано",
    "the session changed the review clone's git config": "сесія змінила git config клону для рев'ю",
    "draft unreadable": "чернетку не вдалося прочитати",
    "the review is outdated": "рев'ю застаріло",
    "usage limit reached": "вичерпано ліміт використання",
    "claude login failing": "вхід claude не працює",
    "the job's report file is missing": "файлу звіту задачі немає",
    "the session wrote no new text": "сесія не написала нового тексту",
    "the review session changed the clone's git config; posting waits until the job ends":
        "сесія рев'ю змінила git config клону; публікація чекає, доки задача завершиться",
    "the review session changed the clone's git config, so the threads command was not run":
        "сесія рев'ю змінила git config клону, тож команду тредів не запущено",
    "Prepare's threads snapshot in this job's copy is unreadable":
        "знімок тредів від Prepare у копії цієї задачі не читається",
    "the job copy's posting directory is not Cleave's": "тека публікації в копії задачі не належить Cleave",
}

NOUNS: dict[str, tuple[str, str, str]] = {
    "file": ("файл", "файли", "файлів"),
    "candidate": ("кандидат", "кандидати", "кандидатів"),
    "question": ("питання", "питання", "питань"),
    "comment": ("коментар", "коментарі", "коментарів"),
    "drafted reply": ("чернетка відповіді", "чернетки відповідей", "чернеток відповідей"),
    "more line": ("рядок", "рядки", "рядків"),
    "review": ("рев'ю", "рев'ю", "рев'ю"),
}
