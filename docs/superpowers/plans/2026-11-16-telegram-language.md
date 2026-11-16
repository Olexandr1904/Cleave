# Telegram Language Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A per-project setting, `telegram.language` (`en`, the default, or `uk`), that puts everything Cleave sends to Telegram for that project in that language: Cleave's own texts and the text models wrote. GitHub, the report files and the dashboard stay as they are.

**Architecture:** Cleave's own texts stay English templates in the code and go through `tr()`, which looks them up in a hand-written Ukrainian catalogue when the current Telegram language (a `ContextVar` set where a project's work starts) is `uk`. Text a model wrote is marked in the composed message; at the send boundary one light `claude -p` call (no tools, scrubbed environment) translates every marked span of the message, with code, paths, links and ids masked, and falls back to the original on any failure. A reviewer review or re-check message fixes its language and translations in the job's run before its first part goes out, so every resend is identical.

**Tech Stack:** Python 3.12 (`contextvars`, `string.Formatter`, `ast` in a test), PyYAML, the Claude Code CLI (`claude -p --model haiku`), python-telegram-bot, Starlette + vanilla JS dashboard, pytest/pytest-asyncio.

**Spec:** the operator's request of 2026-11-16 (no separate spec document). Its decisions are copied into Global Constraints; the calls this plan makes where it leaves a choice are under "Decisions".

## Decisions this plan makes (where the request leaves a choice)

- **Where it is set.** `telegram.language` in `project.yaml` and in `global.yaml`; `en` or `uk`, anything else is a `ConfigError` at load (as `os:` is). A project without it follows `global.yaml`'s; `global.yaml` without it is English.
- **Which language a message takes.** A ticket's messages: its project's. A reviewer message: the project of the PR's repo (the reviewer chat is shared by every repo). An answer to a reply: the anchored job's project; when none is known (reviewer off, store unreadable) the global language. Free-text and switch commands: the language of the projects whose `default_chat_id` is that chat when they agree, else the global one. Credential lines: global.
- **Cleave's own texts** get a hand-written Ukrainian catalogue keyed by the English template. A template missing from it stays English; a test fails the build for any missing, unused or malformed entry.
- **Model text** is translated by `haiku` in one call per message (60 s timeout). English never calls a model. The model never sees what it cannot change: code spans and blocks, URLs, paths, `file:line`, `#N`, ticket keys and SHAs are masked as `⟦n⟧`. A span whose markers or digits do not come back is shown in the original.
- **Dynamic details** Cleave composes at run time (a job's failure reason, an error first line): a detail the catalogue knows is shown in Ukrainian; any other is translated with the message's model text (`detail()`). Stored copies (job reasons, workspace `error`, `human_input_question`, post errors, dashboard) stay English.
- **Human-written text stays as written:** PR and ticket titles, quoted GitHub comment bodies, the operator's own words (edit instructions, won't-fix reasons), branch names.
- **`text N`, edit and draft results** in Ukrainian end with one line saying GitHub gets the English text.
- **Reply commands are unchanged.** Ukrainian hints quote the English commands verbatim (`"post 1 2 4 approve"`); the grammar's Ukrainian synonyms keep working. Button callback data is unchanged; labels are translated.
- **The intent parser** is asked to write its `reply` in the language (no extra call).
- **Dashboard:** the Settings view gets a "Telegram language" section (global and one row per loaded project). Saving rewrites only the `language:` line of the YAML's `telegram:` block (inserted when missing), checked by re-parsing, and applies to the running daemon at once. The **+ New Project** wizard's Notifications step gets a language select; the create route writes it into `project.yaml` once the setup agent has written the file. The dashboard's own UI stays English.

## Global Constraints

- English is the default and today's behaviour: with no setting, every message is byte-identical to before.
- GitHub stays as it is: posted reviews and replies, the job copy, the report files and the replies plan never hold translated text.
- Reply commands keep working unchanged (`post 1 2`, `approve`, `text N`, `edit N: …`, `review again`, `skip`, `tickets start`, …). Numbers, `file:line` anchors, code spans, PR/ticket ids and URLs survive translation exactly.
- A translation failure never blocks or loses a message: the original text is sent.
- The runner's message parts are identical on every attempt (`runner._deliver`, `may_still_save`).
- Ukrainian reads naturally; emoji and markers the code or the operator relies on are kept.
- No employer or company names anywhere (`acme` and placeholders in tests and docs).
- Tests never start the real `claude`, never reach GitHub, Jira or Telegram (fakes, `tmp_path`, respx).
- Python 3.10 syntax floor (`ruff target-version = "py310"`).
- Suite: `PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/unit tests/integration -q -p no:cacheprovider`; the 9 known failures (`test_not_a_git_dir`, 3× `test_main.py::TestMain`, 3× `test_e2e_dry_run.py`, `test_build_jql_ands_multiple_trigger_labels`, `test_bad_payload_returns_400`) are not regressions.

## Review Focus

- **A resend after the language changed** (the operator switches a project to `uk` while a review message is half out): the parts still out must match the first part's language. Pinned by `test_deliver_keeps_the_language_of_the_parts_already_out` (Task 4).
- **A model that mangles a command or a path** (`post 1 2` becomes `опублікуй 1 2`, `app/Foo.kt:41` loses its line): the span falls back to the original. Pinned by `test_masked_spans_come_back_verbatim` and `test_a_span_missing_a_marker_keeps_the_original` (Task 3).
- **A Ukrainian template that drops a quoted command, an emoji or a placeholder:** caught before shipping. Pinned by `test_catalogue_keeps_quotes_emoji_digits_and_placeholders` (Task 2).
- **Translated text reaching GitHub** through an edit made after a Ukrainian `text N`: the edit session reads the job copy only, never the Telegram text. Pinned by `test_edit_prompt_never_holds_translated_text` (Task 4).
- **A hostile report with hundreds of findings:** one call stays bounded. Pinned by `test_translation_input_is_capped` (Task 3).

---

### Task 1: The setting

**Files:**
- Modify: `config/schemas.py` (`TelegramConfig`), `config/config_loader.py` (validation in `load_global_config` and `_load_project_config`)
- Create: `orchestrator/tg_i18n.py` (resolution helpers here; the rest in Task 2)
- Test: `tests/unit/test_telegram_language_config.py`

**Interfaces:**
- Produces: `TelegramConfig.language: str = ""`; `TELEGRAM_LANGUAGES = ("en", "uk")` in `config/schemas.py`; `project_language(global_config, projects, project_id: str) -> str` and `chat_language(global_config, projects, chat_id: str) -> str` in `orchestrator/tg_i18n.py` (always one of `TELEGRAM_LANGUAGES`).

- [ ] **Step 1:** Tests: `language: uk` in project.yaml loads as `"uk"`; `language: de` raises `ConfigError` naming `telegram.language`; `project_language` returns the project's, else the global's, else `"en"`, and `"en"`/global for an unknown or empty project id; `chat_language` returns the agreeing projects' language and the global one when they disagree or none matches.
- [ ] **Step 2:** Run, see them fail. **Step 3:** Implement. **Step 4:** Run, see them pass. **Step 5:** Commit `feat(config): telegram.language per project and global`.

### Task 2: Language context and the catalogue

**Files:**
- Modify: `orchestrator/tg_i18n.py`
- Create: `orchestrator/tg_uk/__init__.py` (merges the area modules), `orchestrator/tg_uk/reviewer.py`, `orchestrator/tg_uk/pipeline.py`, `orchestrator/tg_uk/commands.py` (each `TEXTS: dict[str, str]`, `NOUNS: dict[str, tuple[str, str, str]]`)
- Test: `tests/unit/test_tg_i18n.py`

**Interfaces (`orchestrator/tg_i18n.py`):**
- `telegram_language(language: str)` context manager; `current_language() -> str`.
- `tr(text: str, /, **values) -> str` — Ukrainian template when the language is `uk` and the catalogue has it, then `str.format(**values)` only when values are given.
- `N_(text: str) -> str` — identity; marks a text stored in English and passed to `tr()` where Telegram gets it.
- `count(n: int, singular: str, plural: str = "") -> str` — `"3 files"`; Ukrainian `"3 файли"` from `NOUNS[singular]` (one/few/many by Ukrainian rules).
- `model_text(text: str) -> str` — wraps in `…` (unchanged when empty); `marked_texts(message) -> list[str]` (in order, each once); `fill(message, translations: Mapping[str, str]) -> str` (each span replaced by its translation or itself, markers gone).
- `detail(text: str) -> str` — `tr(text)` when the catalogue knows `text`, else `model_text(text)`.

- [ ] **Step 1:** Tests: `tr` English/Ukrainian/missing; `count` for 1, 2, 5, 11, 21, 22, 25; markers round-trip through `fill`; `detail`. A catalogue test that AST-scans `orchestrator/`, `integrations/`, `health/`, `dashboard/`, `main.py` for `tr`/`N_`/`count` calls: every literal is in the catalogue, every catalogue key is used, `tr` never takes an f-string. `test_catalogue_keeps_quotes_emoji_digits_and_placeholders`: each value keeps every `"…"` span, emoji, digit run and `{placeholder}` of its key.
- [ ] **Steps 2–4:** fail, implement, pass. **Step 5:** Commit `feat(telegram): language context and Ukrainian catalogue`.

### Task 3: Model-text translation

**Files:**
- Create: `orchestrator/tg_translate.py`
- Modify: `integrations/telegram/telegram_adapter.py` (`send_message` and `edit_message` localize the text; `set_translator`)
- Test: `tests/unit/test_tg_translate.py`, `tests/unit/test_telegram_adapter.py`

**Interfaces:**
- `class Translator(llm, *, work_dir: Path, model: str = "haiku", timeout: int = 60, environ=os.environ)`; `async translations(texts: Sequence[str], language: str) -> dict[str, str]` (only the spans translated; `{}` for English, no texts, or a failed call; never raises).
- `async localize(text: str, translator: Translator | None) -> str` — `fill(text, await translator.translations(marked_texts(text), current_language()))`, or `fill(text, {})` without a translator.
- One `execute_in_workspace` call: `model="haiku"`, `max_turns=1`, `env=session_env(environ)`, `extra_args=["--restricted", "--tools", "", "--strict-mcp-config"]`, `cwd` a 0700 directory. The input is a JSON array of masked spans; the answer must be a JSON array of the same length. Input capped at 40 000 characters (later spans stay as they are).

- [ ] **Step 1:** Tests with a fake llm: English makes no call; the prompt is one call per message; `test_masked_spans_come_back_verbatim`; `test_a_span_missing_a_marker_keeps_the_original`; a span whose digits change keeps the original; a non-JSON answer, a wrong length or a raised error gives `{}`; `test_translation_input_is_capped`; the adapter sends the filled text and emits the filled preview.
- [ ] **Steps 2–4.** **Step 5:** Commit `feat(telegram): translate model-written text at send time`.

### Task 4: Reviewer messages

**Files:**
- Modify: `orchestrator/reviewer/{messages,lines,grammar,replies,posting,runner,small_jobs,service,chat,models}.py`, `orchestrator/tg_uk/reviewer.py`
- Test: `tests/unit/reviewer/test_reviewer_language.py` (new) plus existing reviewer tests unchanged

**Interfaces:**
- `JobRun.language: str = ""`, `JobRun.translations: dict = {}` — fixed before the first part goes out; `""` renders English (runs from before this change).
- `ReviewerChat.translator: Translator | None = None`; `async ReviewerChat.translations(texts) -> dict[str, str]`; `async ReviewerChat.localize(text) -> str`.
- `language_of: Callable[[str], str]` keyword (project id → language, default English) on `ReviewerService`, `JobRunner`, `SmallJobRunner`, `ReviewerReplies`.
- `review_message`/`recheck_message` mark model text with `model_text()` (finding titles, `tg_lines`, the event reason, dropped reasons, reply gists).

- [ ] **Step 1:** Tests: a `uk` review message has the Ukrainian frame, the literal hint commands and translated finding lines; `test_deliver_keeps_the_language_of_the_parts_already_out`; a failed translation still sends (originals); a resend renders the same parts; `text N` and edit results are translated and carry the GitHub note; `test_edit_prompt_never_holds_translated_text`; a poll line and a `not understood` answer are Ukrainian; English output unchanged.
- [ ] **Steps 2–4.** **Step 5:** Commit `feat(reviewer): Telegram messages in the project's language`.

### Task 5: Ticket pipeline messages

**Files:**
- Modify: `orchestrator/{orchestrator,notify,escalation,escalation_view,gradle_remediation}.py`, `orchestrator/pipeline/{driver,action_stage,agent_stage}.py`, `orchestrator/pipeline/actions/{fetch_pr_comments,finalize,push_and_open_pr}.py`, `dashboard/actions.py` (rerun notice), `orchestrator/tg_uk/pipeline.py`
- Test: `tests/unit/test_pipeline_language.py`

**Interfaces:** `Orchestrator.advance_workspace` runs under `telegram_language(project_language(...))`; model text (escalation reasons, agent output excerpts, comment assessments, gate verdicts) and dynamic details are marked; state keeps the English text.

- [ ] **Step 1:** Tests: a `uk` escalation has the Ukrainian frame with literal `"skip"`/`"retry"` and a marked reason, while `human_input_question` stays the English reason; gate summary buttons are `Схвалити`/`Відхилити` with unchanged callback data; English unchanged.
- [ ] **Steps 2–4.** **Step 5:** Commit `feat(pipeline): ticket notifications in the project's language`.

### Task 6: Telegram commands and credential lines

**Files:**
- Modify: `integrations/telegram/{command_handler,switch_commands,intent_parser}.py`, `integrations/telegram/handlers/*.py`, `health/credentials.py`, `orchestrator/tg_uk/commands.py`
- Test: `tests/unit/test_commands_language.py`

**Interfaces:** `CommandHandler(..., language_of: Callable[[str], str] | None = None, chat_language: Callable[[str], str] | None = None)`; `CredentialHealth(..., language: Callable[[], str] = lambda: "en")`; `IntentParser.parse(message, context)` adds a reply-language line to its prompt when the current language is not English.

- [ ] **Step 1:** Tests: a switch status and an approve answer in `uk`; a reply to an escalation answered in the ticket's project's language; a credential line in the global language while the dashboard's `fix` stays English; the intent prompt names Ukrainian only in `uk`.
- [ ] **Steps 2–4.** **Step 5:** Commit `feat(telegram): commands and credential lines in the configured language`.

### Task 7: Wiring

**Files:** Modify `main.py`. Test: `tests/unit/test_main_language.py`.

- [ ] Build one `Translator(ClaudeCodeAdapter(), work_dir=<reviewer data>/translate)`; hand it to the adapter (`set_translator`) and the reviewer chat; pass `language_of`/`chat_language`/`language` built from the live `global_config` and `projects`. Test that `_reviewer_chat` carries the translator. Commit `feat: wire the Telegram language`.

### Task 8: Dashboard

**Files:**
- Create: `dashboard/telegram_language.py` (`set_yaml_language(path: Path, language: str) -> None`, routes)
- Modify: `dashboard/web.py`, `dashboard/static/js/settings.js`, `dashboard/static/js/project-wizard.js`, `dashboard/project_create.py`, `dashboard/project_create_payload.py`, `agents/project-setup-agent.md`
- Test: `tests/unit/test_dashboard_telegram_language.py`

**Interfaces:** `GET /api/settings/telegram-language` → `{"options": ["en", "uk"], "global": "en", "projects": [{"id", "name", "language", "effective"}]}`; `PUT` with `{"project_id": "<id>" | null, "language": "en" | "uk"}` → 200 `{"language"}`, 400 bad language, 404 unknown project; same cross-site guard as the reviews routes.

- [ ] **Step 1:** Tests: `set_yaml_language` replaces an existing line, inserts into a `telegram:` block, appends a block when there is none, keeps comments and `${VAR}` references, and refuses a flow-style block; the PUT updates the file and the in-memory config; the create route writes the wizard's language after the setup agent.
- [ ] **Steps 2–4.** **Step 5:** Commit `feat(dashboard): edit the Telegram language`.

### Task 9: Docs

- [ ] `docs/telegram.md` (a "Language" section; correct "Replies are in English"), `docs/dashboard.md` (Settings and wizard), `docs/features/configuration-cascade.md`, `docs/setup-guide.md`, `config-live.example/*.yaml`, the grammar's docstring. Commit `docs: Telegram language`.
