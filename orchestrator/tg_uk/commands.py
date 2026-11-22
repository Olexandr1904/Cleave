"""Ukrainian for the Telegram command layer (integrations/telegram) and the credential lines (health)."""
from __future__ import annotations

TEXTS: dict[str, str] = {
    # integrations/telegram/command_handler.py
    "Reply not handled: the reviewer's store could not be read; nothing was done.":
        "Відповідь не оброблено: не вдалося прочитати сховище рев'юера; нічого не зроблено.",
    "Reviewer mode is off on this daemon: nothing was done.":
        "Режим рев'юера на цьому демоні вимкнено: нічого не зроблено.",
    "Reply to the reviewer's message to act on it; nothing was done.":
        "Щоб це спрацювало, надішліть це як відповідь (Reply) на повідомлення рев'юера; нічого не зроблено.",
    "⚙️ Processing... ({preview})": "⚙️ Обробляю... ({preview})",
    "I didn't understand that. I can do: status, analyze, approve, reject, retry, set_mode, reviewed.":
        "Не зрозумів. Я вмію: status, analyze, approve, reject, retry, set_mode, reviewed.",
    "No active workspace found for {ticket_id}.": "Активного воркспейсу для {ticket_id} не знайдено.",
    "Reviews are not enabled for {scope} (reviewer.enabled in YAML)":
        "Рев'ю для {scope} не ввімкнені (reviewer.enabled у YAML)",
    "Switched to {mode} mode.": "Перемкнув у режим {mode}.",
    "No workspaces awaiting approval.": "Немає воркспейсів, що чекають на схвалення.",
    "Multiple workspaces awaiting approval: {tickets}. Please specify which one.":
        "На схвалення чекають кілька воркспейсів: {tickets}. Уточніть, який саме.",
    "Approved {ticket_id}. Moving to {state}.": "{ticket_id} схвалено. Переходжу до {state}.",
    "Multiple workspaces awaiting approval: {tickets}. Please specify which one to reject.":
        "На схвалення чекають кілька воркспейсів: {tickets}. Уточніть, який відхилити.",
    "Rejected {ticket_id}. Marked as FAILED.": "{ticket_id} відхилено. Позначено як FAILED.",
    "Please specify ticket IDs to analyze.": "Вкажіть ID тікетів для аналізу.",
    "Analyze is not available — no orchestrator callback configured.":
        "Аналіз недоступний — колбек оркестратора не налаштовано.",
    "Analyze failed: {error}": "Аналіз не вдався: {error}",
    "Queued {n} ticket(s) for analysis: {tickets}": "Поставив у чергу на аналіз, тікетів: {n} — {tickets}",
    "Could not queue:": "Не вдалося поставити в чергу:",
    "No tickets were queued.": "Жодного тікета не поставлено в чергу.",
    "No workspaces in PR_REVIEW state.": "Немає воркспейсів у стані PR_REVIEW.",
    "Multiple PRs in review: {ids}. Specify: 'reviewed TICKET-ID'":
        "На рев'ю кілька PR: {ids}. Уточніть: 'reviewed TICKET-ID'",
    "Workspace {ticket_id} not found in PR_REVIEW.": "Воркспейс {ticket_id} не знайдено в PR_REVIEW.",
    "Got it. Fetching PR comments for {ticket_id} now.": "Прийнято. Забираю коментарі PR для {ticket_id}.",
    "Please specify a ticket ID to retry.": "Вкажіть ID тікета, який треба перезапустити.",
    "Several tickets end in {number}: {keys}. Send the full key.":
        "На {number} закінчуються кілька тікетів: {keys}. Надішліть повний ключ.",
    "Unknown stage '{stage}'. Valid stages: {valid}": "Невідомий етап '{stage}'. Допустимі етапи: {valid}",
    "Retrying {ticket_id} from {state}.": "Перезапускаю {ticket_id} з {state}.",
    "{recent} Your new message was NOT added — that input is already being processed. "
    "If the pipeline blocks again with a new question, you can answer it then, "
    "or use `retry <ticket>` to re-run the stage.":
        "{recent} Ваше нове повідомлення НЕ додано — ця відповідь уже обробляється. Якщо пайплайн знову "
        "зупиниться з новим питанням, відповісте тоді, або перезапустіть етап через `retry <ticket>`.",
    "No blocked workspaces are waiting for input.": "Немає заблокованих воркспейсів, що чекають на відповідь.",
    "No blocked workspace found for {ticket_id}.": "Заблокованого воркспейсу для {ticket_id} не знайдено.",
    "Multiple blocked workspaces: {tickets}. Please prefix your answer with the ticket ID.":
        "Заблоковано кілька воркспейсів: {tickets}. Почніть відповідь з ID тікета.",
    "Empty input — nothing to resume with.": "Порожня відповідь — нема з чим продовжувати.",
    "Got it. Resuming {ticket_id} from {state} with your input.":
        "Прийнято. Продовжую {ticket_id} з {state} з вашою відповіддю.",
    "{ticket} (resumed from {state}, {seconds}s ago)": "{ticket} (продовжено з {state} {seconds} с тому)",
    "Already started: {tickets}.": "Уже запущено: {tickets}.",
    "FIX (matched: {token!r}). Dev-agent will re-engage on {file_line}.":
        "FIX (розпізнано: {token!r}). Dev-agent знову візьметься за {file_line}.",
    "WON'T FIX (matched: {token!r}). Posting on GitHub: \"{reason}\".":
        "WON'T FIX (розпізнано: {token!r}). Публікую на GitHub: \"{reason}\".",
    "✓ Recognized as {label}. {decision}": "✓ Розпізнано як {label}. {decision}",
    "Show {n} unanswered": "Показати без відповіді ({n})",
    "{n} comment(s) remaining.": "Залишилось коментарів: {n}.",
    "All decisions in for {ticket_id}. Executing now.": "Усі рішення для {ticket_id} отримано. Виконую.",
    "Got it. Fetching PR comments now.": "Прийнято. Забираю коментарі PR.",
    "Skipped {stage}. Advanced to {state}.": "Пропустив {stage}. Перейшов до {state}.",
    "Retrying {state}.": "Перезапускаю {state}.",
    "Resuming from {state} with your input.": "Продовжую з {state} з вашою відповіддю.",
    "No tickets have unanswered PR comments.": "Немає тікетів з коментарями PR без відповіді.",
    "Resent {n} unanswered comment(s).": "Повторно надіслав коментарів без відповіді: {n}.",
    "Hint limit reached (3/3) for @{author} on {file_line}. "
    "Reply \"fix\" or \"won't fix: <reason>\" to close this comment.":
        "Ліміт підказок вичерпано (3/3) для коментаря @{author} у {file_line}. "
        "Відповідайте \"fix\" або \"won't fix: <reason>\", щоб закрити цей коментар.",
    "Recognized as hint (round {round}/3). Re-checking @{author}'s comment on {file_line} with your context.":
        "Розпізнано як підказку (раунд {round}/3). Ще раз перевіряю коментар @{author} у {file_line} "
        "з урахуванням вашого контексту.",
    "No workspace awaiting approval for {ticket_id}.": "Для {ticket_id} немає воркспейсу, що чекає на схвалення.",
    "Approved. Moving to {state}.": "Схвалено. Переходжу до {state}.",
    "Rejected. Marked as FAILED.": "Відхилено. Позначено як FAILED.",
    "Rejected. Back to {state}.": "Відхилено. Повертаю до {state}.",
    "No workspace in PR_REVIEW for {ticket_id}.": "Для {ticket_id} немає воркспейсу в PR_REVIEW.",
    "Retrying from {state}.": "Перезапускаю з {state}.",
    "Failed to clear Gradle cache: {error}": "Не вдалося очистити кеш Gradle: {error}",
    "Cleared {mb:.0f} MB of Gradle transforms cache. Retrying from {state}.":
        "Очищено {mb:.0f} МБ кешу Gradle transforms. Перезапускаю з {state}.",
    "No pending comments for {ticket_id}.": "Для {ticket_id} немає коментарів, що чекають на рішення.",
    "✓ Recognized as FIX (matched: 'Fix' button). Dev-agent will re-engage on {file_line}.":
        "✓ Розпізнано як FIX (натиснуто кнопку). Dev-agent знову візьметься за {file_line}.",
    "✓ Recognized as WON'T FIX (matched: 'Won't Fix' button). "
    "Posting on GitHub: \"operator decision\" on {file_line}.":
        "✓ Розпізнано як WON'T FIX (натиснуто кнопку). Публікую на GitHub \"operator decision\" "
        "для {file_line}.",
    "Comment not found.": "Коментар не знайдено.",
    "Unknown action: {action}": "Невідома дія: {action}",
    # integrations/telegram/switch_commands.py
    '"{repo}" exists in several projects: {scopes} — use project/repo':
        '"{repo}" є в кількох проєктах: {scopes} — вкажіть project/repo',
    'Unknown repo "{repo}". Known: {scopes}': 'Невідомий репозиторій "{repo}". Відомі: {scopes}',
    "running": "увімкнено",
    "paused": "на паузі",
    "Tickets": "Тікети",
    "Reviews": "Рев'ю",
    "no repos": "немає репозиторіїв",
    "no repos with reviews": "немає репозиторіїв з рев'ю",
    " (global)": " (глобально)",
    # integrations/telegram/handlers/status.py
    "Code Check": "Перевірка коду",
    "Plan Review": "Рев'ю плану",
    "Code Review": "Код-рев'ю",
    "Awaiting Approval": "Чекає на схвалення",
    "Manual Control": "Ручне керування",
    "PR Review": "Рев'ю PR",
    "Pushing": "Пуш",
    "{s}s": "{s} с",
    "{m}m": "{m} хв",
    "{d}d {h}h": "{d} д {h} год",
    "{h}h {m}m": "{h} год {m} хв",
    "Cleave Status": "Стан Cleave",
    "Mode: {mode}": "Режим: {mode}",
    "Uptime: {duration}": "Працює: {duration}",
    "Last Jira poll: {duration} ago": "Останнє опитування Jira: {duration} тому",
    "Active ({n}):": "Активні ({n}):",
    ", awaiting approval": ", чекає на схвалення",
    ", blocked": ", заблоковано",
    " (iteration {n}/{max})": " (ітерація {n}/{max})",
    "  (none)": "  (немає)",
    "Recent ({n}):": "Нещодавні ({n}):",
    "merged": "змерджено",
    "Recent: none": "Нещодавні: немає",
    "Stage: {stage}": "Етап: {stage}",
    "Branch: {branch}": "Гілка: {branch}",
    "Iterations:": "Ітерації:",
    "Blocked on: {reason}": "Заблоковано на: {reason}",
    "Last error: {error}": "Остання помилка: {error}",
    "Last error: none": "Остання помилка: немає",
    # integrations/telegram/intent_parser.py
    "I didn't understand that. I can do: status checks, analyze tickets, approve/reject steps, "
    "switch modes, unanswered comment recall.":
        "Не зрозумів. Я вмію: показати статус, проаналізувати тікети, схвалити чи відхилити крок, "
        "перемкнути режим, нагадати про коментарі без відповіді.",
    "I'm having trouble understanding right now. Try again in a moment.":
        "Зараз не можу розібрати повідомлення. Спробуйте трохи згодом.",
    # health/credentials.py
    "Log in again on the host in a GUI terminal.": "Увійдіть знову на хості в GUI-терміналі.",
    "claude login": "вхід у claude",
    "Add the directory with claude (/opt/homebrew/bin) to the daemon's PATH and restart it.":
        "Додайте каталог з claude (/opt/homebrew/bin) до PATH демона і перезапустіть його.",
    "Network or service unavailable; next check in 15 min.":
        "Мережа чи сервіс недоступні; наступна перевірка через 15 хв.",
    "Create a new token, put it in .env as {env_var}, restart the daemon.":
        "Створіть новий токен, запишіть його в .env як {env_var} і перезапустіть демон.",
    "Create a new token, set it in {field}, restart the daemon.":
        "Створіть новий токен, вкажіть його в {field} і перезапустіть демон.",
    "Check the token's access to {target}; "
    "a classic GitHub token may need SSO authorization for the organization.":
        "Перевірте доступ токена до {target}; класичному токену GitHub може знадобитися SSO-авторизація "
        "для організації.",
    "Check the token's access to {target}.": "Перевірте доступ токена до {target}.",
    "⚠️ {label}: invalid or expired. {fix}": "⚠️ {label}: недійсний або прострочений. {fix}",
    "⚠️ {label}: no access (HTTP {status}). {fix}": "⚠️ {label}: немає доступу (HTTP {status}). {fix}",
    "✅ {label}: working again.": "✅ {label}: знову працює.",
    "⏳ The claude login on the host expires {when}. {fix}": "⏳ Вхід у claude на хості спливає {when}. {fix}",
}

NOUNS: dict[str, tuple[str, str, str]] = {
}
