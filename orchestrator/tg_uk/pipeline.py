"""Ukrainian for the ticket pipeline's notifications (orchestrator, orchestrator/pipeline, dashboard actions)."""
from __future__ import annotations

TEXTS: dict[str, str] = {
    # Buttons
    "Approve": "Схвалити",
    "Reject": "Відхилити",
    "Retry": "Повторити",
    "Retry Now": "Повторити зараз",
    "🧹 Clear cache & retry": "🧹 Очистити кеш і повторити",
    "Fix": "Виправити",
    "Won't Fix": "Не виправляти",
    "Review Complete": "Рев'ю завершено",
    "Show {count} unanswered": "Показати без відповіді ({count})",

    # orchestrator/notify.py
    "Quota exhausted at {stage}, deferred until {when} UTC. Other tickets hitting the same quota will defer "
    "silently.":
        "Ліміт використання вичерпано на етапі {stage}, тікет відкладено до {when} UTC. Інші тікети, що впруться "
        "в той самий ліміт, відкладуться без повідомлень.",
    "Agent hit max-turns limit at {stage}, deferred until {when} UTC for retry. If this recurs the agent task may "
    "be too complex — consider raising `max_turns` or splitting the work.":
        "Агент вичерпав ліміт ходів (max-turns) на етапі {stage}; повтор відкладено до {when} UTC. Якщо це "
        "повториться, задача для агента, мабуть, надто складна — збільште `max_turns` або розбийте роботу на частини.",
    "Transient agent failure at {stage}, deferred until {when} UTC. Reason: {reason}":
        "Тимчасовий збій агента на етапі {stage}, відкладено до {when} UTC. Причина: {reason}",
    "no detail captured": "подробиць немає",
    "FAILED at {stage}.\n⚠️ Architecture mismatch (x86-64 aapt2 on non-x86 host).":
        "ЗБІЙ на етапі {stage}.\n⚠️ Невідповідність архітектури (x86-64 aapt2 на хості не x86).",
    "FAILED at {stage}. Error: {error}.\nDetected Gradle cache corruption — tap below to clear it.":
        "ЗБІЙ на етапі {stage}. Помилка: {error}.\nВиявлено пошкоджений кеш Gradle — натисніть кнопку нижче, щоб "
        "очистити його.",
    "FAILED at {stage}.\n\nReason: {reason}\n\nOptions:\n- Tap Retry to re-run from {stage}\n- Send \"retry "
    "{ticket_id} from dev\" to restart from an earlier stage":
        "ЗБІЙ на етапі {stage}.\n\nПричина: {reason}\n\nЩо можна зробити:\n- Натисніть «Повторити», щоб "
        "перезапустити з етапу {stage}\n- Надішліть \"retry {ticket_id} from dev\", щоб почати з ранішого етапу",
    "Rerun started from dashboard.\nBranch: {branch}\nReason: {reason}":
        "Перезапуск із дашборда.\nГілка: {branch}\nПричина: {reason}",
    "Stage: {stage} — verification failed": "Етап: {stage} — перевірка не пройшла",
    "Verification failed: {reason}": "Перевірка не пройшла: {reason}",
    "↩️ Reply with your answer or additional context, or:\n  Reply \"skip\" — advance past this stage to the next "
    "one\n  Reply \"retry\" — re-run this stage":
        "↩️ Дайте відповідь на це повідомлення або додайте контекст, або:\n  Відповідь \"skip\" — пропустити цей "
        "етап і перейти до наступного\n  Відповідь \"retry\" — запустити цей етап ще раз",

    # orchestrator/gradle_remediation.py
    "Gradle picked the x86-64 build of aapt2 but the host can't run it.\nPipeline can't auto-fix this. To "
    "unblock, choose one:\n1. Install x86-64 emulation:\n   sudo apt install qemu-user-static binfmt-support\n2. "
    "Force AGP to use the aarch64 aapt2 build by adding to ~/.gradle/gradle.properties:\n   "
    "org.gradle.jvmargs=-Dos.arch=aarch64\n3. Run the build on an x86-64 host.\nAfter fixing the host, click "
    "Retry.":
        "Gradle вибрав x86-64-збірку aapt2, але хост не може її запустити.\nPipeline не виправить це "
        "автоматично. Щоб розблокувати, виберіть один із варіантів:\n1. Встановіть емуляцію x86-64:\n   sudo apt "
        "install qemu-user-static binfmt-support\n2. Змусьте AGP брати aarch64-збірку aapt2, додавши в "
        "~/.gradle/gradle.properties:\n   org.gradle.jvmargs=-Dos.arch=aarch64\n3. Запустіть збірку на хості "
        "x86-64.\nКоли виправите хост, натисніть «Повторити».",

    # orchestrator/escalation.py
    "The agent ran {iterations} times without completing this stage. Last output is shown above — it may say PASS "
    "but something is still blocking progress.":
        "Спроб агента: {iterations}, а етап так і не завершено. Останній вивід — вище: там може бути PASS, але "
        "щось усе ще блокує роботу.",
    "The agent ran {iterations} times but did not produce any result (each attempt timed out, crashed, or was "
    "interrupted before writing output) — there is no agent output to show.":
        "Спроб агента: {iterations}, і жодна не дала результату (кожна завершилась тайм-аутом, падінням або "
        "була перервана до запису виводу) — показати нічого.",
    "Options:\n  Reply with context — give the agent more information and resume\n  Reply \"skip\" — advance past "
    "this stage to the next one\n  Reply \"retry\" — re-run this stage (resets the attempt counter)\n  Send "
    "\"retry {ticket_id} from {stage_id}\" — reset counter and run again\n  Send \"retry {ticket_id} from dev\" — "
    "restart the dev agent from scratch":
        "Що можна зробити:\n  Відповісти з контекстом — дати агенту більше інформації й продовжити\n  Відповідь "
        "\"skip\" — пропустити цей етап і перейти до наступного\n  Відповідь \"retry\" — запустити етап ще раз "
        "(лічильник спроб скидається)\n  Надіслати \"retry {ticket_id} from {stage_id}\" — скинути лічильник і "
        "запустити знову\n  Надіслати \"retry {ticket_id} from dev\" — перезапустити dev-агента з нуля",
    "Stage: {stage} — stuck after {iterations}/{cap} attempts":
        "Етап: {stage} — застряг після {iterations}/{cap} спроб",
    "Stage: {stage} — rejected {iterations} of {cap} times":
        "Етап: {stage} — відхилено {iterations} з {cap} разів",
    "Nothing has changed since this verdict: the ticket stopped on it.\n\nOptions:\n  Reply with context — "
    "{fix_stage} reworks it with your input, then it is checked again\n  Reply \"retry\" — the same, without "
    "your input\n  Reply \"skip\" — accept the work as it is and go on to the next stage":
        "Після цього вердикту нічого не змінювалося: тікет зупинився на ньому.\n\nЩо можна зробити:\n  "
        "Відповідь із контекстом — {fix_stage} переробить роботу з вашими вказівками, потім її перевірять знову\n"
        "  Відповідь \"retry\" — те саме, без ваших вказівок\n  Відповідь \"skip\" — прийняти роботу як є і "
        "перейти до наступного етапу",
    "Stage: {stage}": "Етап: {stage}",
    "Options:\n  Reply \"skip\" — advance past this stage to the next one\n  Reply \"retry\" — re-run this stage\n  "
    "Send \"retry {ticket_id}\" to retry from this stage\n  Send \"retry {ticket_id} from dev\" to restart from an "
    "earlier stage":
        "Що можна зробити:\n  Відповідь \"skip\" — пропустити цей етап і перейти до наступного\n  Відповідь "
        "\"retry\" — запустити цей етап ще раз\n  Надішліть \"retry {ticket_id}\", щоб повторити з цього етапу\n  "
        "Надішліть \"retry {ticket_id} from dev\", щоб почати з ранішого етапу",

    # orchestrator/escalation_view.py
    "🔁 (still pending) ": "🔁 (досі чекає) ",
    "Valid": "Слушне",
    "Not valid": "Неслушне",
    "PR #{pr_number} — Comment by @{author} on {file}:{line}":
        "PR #{pr_number} — коментар @{author} до {file}:{line}",
    "Suggestion:": "Пропозиція:",
    "Agent assessment:": "Оцінка агента:",
    "Tap a button below, or reply to this message with:\n  - fix — re-engage dev-agent\n  - won't fix: <reason> — "
    "post the reason on GitHub and resolve\n  - free text — re-investigate with your hint\n":
        "Натисніть кнопку нижче або дайте відповідь на це повідомлення:\n  - fix — знову залучити dev-агента\n  - "
        "won't fix: <причина> — опублікувати причину на GitHub і закрити тред\n  - довільний текст — перевірити ще "
        "раз із вашою підказкою\n",

    # orchestrator/pipeline/driver.py
    "Review complete.": "Рев'ю завершено.",
    "{count} comment(s) processed.": "Оброблено коментарів: {count}.",
    "No PR comments found.": "Коментарів до PR немає.",
    "N/A": "немає",
    "Plan review complete.": "Рев'ю плану завершено.",
    "BA decision:": "Рішення BA:",
    "Plan reviewer:": "Рев'юер плану:",
    "Approve = start coding. Reject = back to analysis.":
        "Схвалити — почати кодування. Відхилити — назад до аналізу.",
    "Awaiting approval at {gate_state}": "Чекає схвалення на етапі {gate_state}",

    # orchestrator/pipeline/action_stage.py
    "PR opened: {pr_url}\n\nReview the diff and merge when ready. The pipeline will wait.\n\nIf there are review "
    "comments, Cleave will escalate them one by one for your decision (Fix or Won't Fix). Reply to any escalation "
    "message to provide context.\n\nWhen done: tap Review Complete or reply to this message.":
        "PR відкрито: {pr_url}\n\nПерегляньте диф і змерджте, коли будете готові. Pipeline чекатиме.\n\nЯкщо "
        "з'являться коментарі рев'ю, Cleave надсилатиме їх по одному на ваше рішення («Виправити» або «Не "
        "виправляти»). Щоб додати контекст, дайте відповідь на будь-яке з цих повідомлень.\n\nКоли закінчите: "
        "натисніть «Рев'ю завершено» або дайте відповідь на це повідомлення.",
    "Draft PR opened: {pr_url}\n\nThis ticket is compared with the skill flow: Cleave answers no review comments on "
    "it, and makes the tracker writes once you pick the better PR.":
        "Чернетку PR відкрито: {pr_url}\n\nЦей тікет порівнюється зі skill flow: Cleave не відповідає на коментарі "
        "рев'ю до нього, а записи в трекер зробить, щойно ви оберете кращий PR.",

    # orchestrator/pipeline/agent_stage.py
    "Android SDK not installed — build not verified": "Android SDK не встановлено — збірку не перевірено",
    "JDK not installed — tests not run": "JDK не встановлено — тести не запускались",
    "Quality gates could not run locally": "Quality gates не вдалося запустити локально",
    "QA passed but with warnings:": "QA пройдено, але з попередженнями:",
    "CI on GitHub will be the authoritative gate.": "Остаточною перевіркою буде CI на GitHub.",

    # orchestrator/pipeline/actions/finalize.py
    "(no PR)": "(PR немає)",
    "PR ready for human merge: {pr_url}": "PR готовий до ручного мерджу: {pr_url}",
    "Pipeline complete.\n\nPR ready for merge: {pr_url}":
        "Pipeline завершено.\n\nPR готовий до мерджу: {pr_url}",
    "PR opened: {pr_url}\n\nReviewer mode takes it from here: its turns and review threads come to the reviewer chat.":
        "PR відкрито: {pr_url}\n\nДалі ним займається режим рев'юера: його черги й треди рев'ю приходять у чат рев'юера.",
    "Tracker: moved to {status}.": "Трекер: переведено в {status}.",
    "Tracker: not moved to {status}: {reason}.": "Трекер: не переведено в {status}: {reason}.",

    # orchestrator/pipeline/actions/push_and_open_pr.py, fetch_pr_comments.py
    "Dev-agent failed to apply the fix for comment #{cid} twice.\n\nFile: {file}:{line}\n\nOptions:\n- Reply "
    "\"fix\" to retry once more\n- Reply \"won't fix: <reason>\" to close the comment without fixing":
        "Dev-агент двічі не зміг застосувати виправлення до коментаря #{cid}.\n\nФайл: {file}:{line}\n\nЩо можна "
        "зробити:\n- Відповідь \"fix\" — спробувати ще раз\n- Відповідь \"won't fix: <reason>\" — закрити коментар "
        "без виправлення",
    "Re-investigation failed for @{author}'s comment on {file}:{line}\n\nThe agent was unable to re-classify this "
    "comment after your hint. Options:\n- Reply \"fix\" to send the dev-agent in anyway\n- Reply \"won't fix: "
    "<reason>\" to close the comment on GitHub":
        "Не вдалося повторно розібрати коментар @{author} до {file}:{line}\n\nАгент не зміг перекласифікувати "
        "цей коментар після вашої підказки. Що можна зробити:\n- Відповідь \"fix\" — усе одно відправити "
        "dev-агента\n- Відповідь \"won't fix: <reason>\" — закрити коментар на GitHub",
    "{count} comment(s) marked Skip — still open on the PR:":
        "Позначені як Skip коментарі ({count}) досі відкриті в PR:",
    "  • @{author} on {file}:{line}": "  • @{author} — {file}:{line}",
    "Approve → mark DONE, leave comments open on GitHub for manual follow-up.\nReject → reopen for dev-agent to "
    "address the comments.":
        "Схвалити → позначити DONE, а коментарі лишити відкритими на GitHub, щоб розібрати вручну.\nВідхилити → "
        "повернути dev-агенту, щоб він опрацював коментарі.",
    "PR #{pr_number} — Auto-processed {count} comment(s):":
        "PR #{pr_number} — автоматично оброблені коментарі ({count}):",
    "✅ FIX: {reason} ({file}:{line})": "✅ Виправляємо: {reason} ({file}:{line})",
    "❌ REJECT: {body} — {reason}": "❌ Відхилено: {body} — {reason}",
    "Waiting for your decisions on {count} escalated comment(s).":
        "Чекаю на ваші рішення щодо ескальованих коментарів: {count}.",
}

NOUNS: dict[str, tuple[str, str, str]] = {
}
