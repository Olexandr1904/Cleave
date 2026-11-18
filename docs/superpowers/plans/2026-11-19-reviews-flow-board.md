# Reviews Flow Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Reviews tab's two tables with a four-column "whose move" board, findings by severity in
words, a jargon-free panel header, a confirmation before re-running a review or fix of a commit that has
one, and a **Fix now** button that starts the existing address job at once.

**Architecture:**
- **Server.** `ReviewerService.overview()` adds what the browser cannot know: the operator's login, the
  project id, each PR's newest review and whether Fix now applies. `reviews_api.py` exposes these with
  the project's Jira URL and a new `POST /api/reviews/fix-now`.
- **Browser.** It computes the board: the columns, tiles and cards come from the rows. They are pure
  functions in a new `reviews-board.js`, unit-tested with Node's built-in runner. The DOM, events and
  side panel stay in `reviews.js`.

**Tech Stack:** Python 3 / Starlette, pytest; vanilla ES modules, `node --test` (Node ≥ 22; the host has
v26); CSS in `dashboard/static/style.css`.

**Spec:** `docs/superpowers/specs/2026-11-19-reviews-flow-board-design.md`. Read it first. This plan
argues from it and does not repeat its rationale. Its mockup:
`.superpowers/brainstorm/90853-1795037528/content/board-v4.html` (local, git-ignored). Open it in a
browser for the look.

## Global Constraints

- Nothing posts from the dashboard: Fix now and Review now only queue jobs; push and post stay in Telegram.
- Columns, left to right: **To review · To fix · With others · To merge**. For my turns the precedence is
  review/recheck → any other non-merge → merge. No turn of mine → With others.
- Severity colors: critical `#d03b3b`, major `#ec835a`, minor `#fab219`, question `#3987e5`; each mark has
  its word next to it, text in ink colors.
- The age is amber from **48 hours** since `last_change`.
- Avatars: `https://github.com/<login>.png?size=40` only when the PR URL starts with `https://github.com/`;
  the fallback is the login's first two letters, upper-case.
- **Cleave is a public repo**: no real logins, company repos, ticket keys or PR titles in committed files
  (code, tests, docs). Use `acme/app`, `ACME-…`, `octo`, `octo-reviewer`, `octo-dev`.
- **Python tests**: `$VENV_PY` is the main checkout's `.venv/bin/python` (export it once). From the checkout root, `timeout 300 $VENV_PY
  -m pytest <paths> -q`. The venv lives only in the main checkout; pytest may not exit after its summary,
  so `timeout` is there. Nine tests fail on untouched master for environment reasons (listed in the
  project memory); any other failure is real.
- **JS tests**: `node --test tests/js/`.
- **Commits**: one per task, on a feature branch (never on `master`); messages end with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

The five failure modes the spec implies that most likely bite in use, each pinned in its owning task:

1. **A repo whose poll fails, with an empty board.** The status line shows the reason, all four
   columns show their placeholders, and the tiles read 0 / `—`. Pinned in Task 3 (`boardTiles([])`)
   and verified in Task 4.
2. **Auto-refresh (every 5 s) while the confirmation dialog is open.** The dialog stays open and still
   queues the PR it was opened for. Verified in Task 4.
3. **A hash naming a repo that is no longer configured** (`#/reviews/old/repo`). The board shows all
   repos as if no filter were set. Pinned in Task 4 (`repoFilterOf`) and verified there.
4. **A PR title that is only its ticket key** (`ACME-12`). Stripping the key must not leave an empty
   title: keep the original. Pinned in Task 3 (`cardTitle`).
5. **More than two people holding the move.** The card shows two avatars and `+N`, and the tile counts
   each login once per PR. Pinned in Task 3 (`moveText`, `avatarsHtml`, `boardTiles`).

---

### Task 1: Service — newest review, login, project, Fix now rule, `address_now`

**Files:**
- Modify: `orchestrator/reviewer/service.py` (`overview` ≈502-525, `address_again` ≈464-483, module helpers
  near `_job_info` ≈140)
- Test: `tests/unit/test_reviewer_service.py`
- Also commit: the spec and this plan (`docs/superpowers/specs/2026-11-19-reviews-flow-board-design.md`,
  `docs/superpowers/plans/2026-11-19-reviews-flow-board.md`)

**Interfaces:**
- Produces:
  - `overview()` repo dicts gain:
    - `"project_id": str` (`ctx.project_id`);
    - `"me": str` (`RepoState.me`);
    - `"reviews": dict[int, dict]`: per PR on the board, its newest job of kind `review`, at any head and
      in any state, as `_review_info`; a PR without a review is absent;
    - `"fixable": set[int]`.
  - `_review_info(job: Job, run: JobRun | None) -> dict` with exactly these keys: `id`, `state`, `head`
    (`head_sha[:9]`), `started_at`, `finished_at`, `duration_s` (via `_duration`), `cost_usd`,
    `message_sent`, `findings` (via `_findings`). A missing run counts as `JobRun(job.id)`, as
    `_job_info` does.
  - `_why_not_fixable(cfg: ReviewerConfig, row: BoardRow, held: frozenset[int]) -> str`: `""` when Fix
    now applies, else the first failing reason, checked in this order:
    1. `"no address_doc"`;
    2. `"not your PR"` (no `author` role);
    3. `"no thread turn"` (none of my actions in `ADDRESS_ACTIONS`);
    4. `"a ticket workspace holds it"` (`row.pipeline`);
    5. `"held by a comparison"` (`row.number in held`).

    `held` is `held_prs(board, self._compared_tickets(scope))`.
  - `ReviewerService.address_now(scope: str, number: int) -> Job`. Errors:
    - `LookupError` from `_row` when the PR is gone;
    - `ValueError("already queued")` when an address job of the PR is queued, running or deferred;
    - `ValueError(f"not fixable: {why}")`.

    Otherwise it does what `address_again` does: supersede the PR's `done`, `posted` and `stale` address
    jobs with reason `new address`, then add an address job keyed `f"{head}:{','.join(sorted threads)}"`.
    It saves **no** run (no instruction). `address_again` keeps its own behaviour and error messages.
    Factor the shared part into one private helper.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_reviewer_service.py`. Use the existing
  helpers `Rig`, `ctx`, `review_pr`, `finished_job`, `ADDR_CFG`, `CFG`, `spr`, `snap`, `det`, `Turn`, `A`,
  `RHEAD`, `T0`:
  - `test_overview_carries_me_project_and_the_newest_review`:
    - poll a review PR;
    - `review = finished_job(rig, tmp_path, "done", started_at=T0.isoformat(), finished_at=(T0 +
      timedelta(seconds=3610)).isoformat(), cost_usd=28.93)`;
    - then a newer `recheck` job on PR 1 via `rig.store.add_job`;
    - assert `repo["me"] == "octo"` and `repo["project_id"] == "acme"`;
    - assert `repo["reviews"][1]["id"] == review.id`, `set(repo["reviews"][1]) == {"id", "state", "head",
      "started_at", "finished_at", "duration_s", "cost_usd", "message_sent", "findings"}`, `["duration_s"]
      == 3610`, `["cost_usd"] == 28.93`;
    - assert `repo["jobs"][1].kind == "recheck"`: the row job is still the newest of any kind.
  - `test_overview_has_no_review_for_a_pr_without_one`: a polled PR with no jobs → `1 not in
    repo["reviews"]`.
  - `test_why_not_fixable` (parametrized over `cfg`, `roles`, `my_turns`, `pipeline`, `held` →
    expected). Rows: `dataclasses.replace` of a polled board row.
    - Fixable (`""`): `ADDR_CFG`, `["author"]`, an `answer_threads` turn.
    - Each failing condition alone gives its reason string, in the order listed under Interfaces.
  - `test_overview_fixable_follows_the_rule`: an `ADDR_CFG` context whose snapshot has `me="octo"` and PR 1
    authored by `octo` with an `answer_threads` turn (as in `test_address_again_supersedes_and_queues_
    with_the_instruction`) → `repo["fixable"] == {1}`. The same with `CFG` → `set()`.
  - `test_address_now_queues_without_an_instruction`: mirror
    `test_address_again_supersedes_and_queues_with_the_instruction` (done address superseded with `new
    address`, failed one untouched, new job `("address", A, f"{A}:T1,T2", "queued")`), then assert
    `rig.store.run(new.id) is None`.
  - `test_address_now_refuses`:
    - a second call → `ValueError` matching `already queued`;
    - number 99 → `LookupError`;
    - with `CFG` → `ValueError` matching `not fixable: no address_doc`, and no job added.

- [ ] **Step 2: Run them to verify they fail**

Run: `timeout 300 $VENV_PY -m pytest tests/unit/test_reviewer_service.py -q -k "overview_carries or no_review_for or why_not_fixable or fixable_follows or address_now"`
Expected: FAIL (`KeyError: 'me'`, `AttributeError: ... address_now`, `ImportError: _why_not_fixable`)

- [ ] **Step 3: Implement `_review_info`, `_why_not_fixable`, the new `overview()` keys, `address_now`
  and the shared address helper** in `orchestrator/reviewer/service.py`, per the Interfaces above.

- [ ] **Step 4: Run the service tests**

Run: `timeout 300 $VENV_PY -m pytest tests/unit/test_reviewer_service.py -q`
Expected: all pass, the `address_again` tests included.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/reviewer/service.py tests/unit/test_reviewer_service.py \
  docs/superpowers/specs/2026-11-19-reviews-flow-board-design.md docs/superpowers/plans/2026-11-19-reviews-flow-board.md
git commit -m "feat(reviewer): overview's newest review, login and Fix now rule; address_now"
```

---

### Task 2: API — `me`, `jira_url`, `review`, `can_fix`, `POST /api/reviews/fix-now`

**Files:**
- Modify: `dashboard/reviews_api.py`, `dashboard/web.py:516-517`
- Test: `tests/unit/test_dashboard_reviews.py`

**Interfaces:**
- Consumes: Task 1's `overview()` keys and `address_now`.
- Produces:
  - **Function**: `build_review_routes(service: Any, get_projects: Callable[[], dict] = dict) ->
    list[Route]`. `web.py` passes `lambda: projects or {}`, as it does for the switch routes.
  - **Helper**: `_jira_url(projects: dict, project_id: str) -> str` returns the project's
    `config.tracker.jira.url` without a trailing `/` when `tracker.provider == "jira"` and the URL is set,
    else `""`. It mirrors `web.py`'s `_build_external_links` and uses `getattr` for missing attributes.
  - **Repo JSON** gains `"me"` and `"jira_url"`.
  - **PR JSON** gains:
    - `"review"`: `repo["reviews"].get(number)`, or `None`;
    - `"can_fix"`: `number in repo["fixable"]`.
  - **Route** `POST /api/reviews/fix-now`, body `{scope, number}`, with the same `_target` guard
    (403/400):
    - 200: `{"job": _job(job)}`;
    - 404: `LookupError`;
    - 409: `ValueError`, with `{"error": str(e)}`.
  - **JS**: `dashboard/static/js/api.js` gains `export async function fixNow(scope, number)` →
    `postReviewAction('/api/reviews/fix-now', scope, number)`.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_dashboard_reviews.py`:
  - **Fixture**: `PR_KEYS` gains `"review"` and `"can_fix"`. `FakeService.overview()` gains
    `"project_id": "acme"`, `"me": "octo"`, `"reviews": {1: REVIEW_INFO}` (a module constant with the
    nine keys of Task 1) and `"fixable": {2}`. `FakeService.address_now` records
    `("address_now", scope, number)`, raises `self.address_now_error` when set, else returns an
    address `Job`.
  - `test_get_reviews_groups_and_trims`:
    - `repo["me"] == "octo"` and `repo["jira_url"] == ""`;
    - the PR numbered 1 has `review == REVIEW_INFO` and `can_fix is False`;
    - the PR numbered 3 has `review is None`;
    - the PR numbered 2 has `can_fix is True`.
  - `test_jira_url_from_the_project_tracker` (parametrized):
    - a `projects={"acme": SimpleNamespace(config=SimpleNamespace(tracker=SimpleNamespace(provider="jira",
      jira=SimpleNamespace(url="https://acme.atlassian.net/"))))}` passed to `create_app` →
      `"https://acme.atlassian.net"`;
    - provider `"linear"` → `""`;
    - `tracker=None` → `""`;
    - no project `acme` → `""`.
  - `test_fix_now_codes`:
    - 200 with `{"job": {"id": …, "kind": "address", "state": "queued", "head": HEAD[:9]}}` and the
      call recorded;
    - `LookupError` → 404;
    - `ValueError("already queued")` → 409 with that error;
    - `ValueError("not fixable: not your PR")` → 409 with that error;
    - `{}` → 400, without reaching the service.
  - **Existing tests**:
    - add `"/api/reviews/fix-now"` to the `path` parameters of `test_bad_body_is_400` and
      `test_cross_site_action_is_refused`;
    - `test_no_posting_route` expects `("POST", "/api/reviews/fix-now")` too and `len(routes) == 5`;
      update its docstring: Fix now queues, it does not post.

- [ ] **Step 2: Run them to verify they fail**

Run: `timeout 300 $VENV_PY -m pytest tests/unit/test_dashboard_reviews.py -q`
Expected: FAIL on the new keys, the jira URL and the missing route.

- [ ] **Step 3: Implement** the Interfaces in `dashboard/reviews_api.py`, `dashboard/web.py` and
  `dashboard/static/js/api.js`.

- [ ] **Step 4: Run the dashboard and service tests**

Run: `timeout 300 $VENV_PY -m pytest tests/unit/test_dashboard_reviews.py tests/unit/test_reviewer_service.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add dashboard/reviews_api.py dashboard/web.py dashboard/static/js/api.js tests/unit/test_dashboard_reviews.py
git commit -m "feat(dashboard): reviews API carries me, jira_url, review and can_fix; fix-now route"
```

---

### Task 3: Board model — pure functions in `reviews-board.js`

**Files:**
- Create: `dashboard/static/js/reviews-board.js`. It imports only `esc` from `./helpers.js` and touches
  no DOM, so Node can import it.
- Create: `tests/js/reviews-board.test.mjs`, using `node:test` and `node:assert/strict`. Set
  `process.env.TZ = 'UTC'` before the first `Date` use. Use a synthetic seven-PR fixture shaped like an
  `/api/reviews` repo (below).

**Interfaces:**
- Consumes: the PR JSON of Task 2.
- Produces. Every export takes plain data; `now` is milliseconds since the epoch.
  - **`COLUMNS`**, in order:
    - `{key: 'review', title: 'To review', hint: 'your review or re-check', empty: 'Nothing to review'}`
    - `{key: 'fix', title: 'To fix', hint: 'your move on your PR', empty: 'Nothing to fix'}`
    - `{key: 'others', title: 'With others', hint: 'no move of yours', empty: "Nobody else's move"}`
    - `{key: 'merge', title: 'To merge', hint: 'your merge', empty: 'Nothing to merge'}`
  - `columnOf(pr) -> 'review'|'fix'|'others'|'merge'`
  - `isMine(pr) -> boolean`: `roles` includes `author`.
  - `actionWords(action: string) -> string`: the spec's table (`recheck` → `re-check`, `resolve_conflicts` →
    `resolve conflicts`, …); else `_` → space.
  - `cardTitle(title: string, ticketKey: string) -> string`: drop a leading `ticketKey` plus a following
    `:` or `-` and spaces; if that leaves nothing, or the title does not start with the key, return
    `title` unchanged.
  - **`reportAtHead(pr) -> boolean`**: `pr.review` is set, `pr.review.head === pr.head`, and its `state`
    is one of `done`, `posted`, `stale`.
  - **`reviewPending(pr) -> string`**: `''`, or the `queued`/`running`/`deferred` state of `pr.review` at
    the head.
  - **`fixState(pr) -> 'none'|'pending'|'again'`**, from `pr.job` when its kind is `address`:
    - `queued`, `running` or `deferred` → `'pending'`;
    - `done`, `posted` or `stale` → `'again'`;
    - else `'none'`.
  - `shortAge(iso: string, now: number) -> {text: string, old: boolean}`:
    - the text is `Nm` under 1 h, `Nh` under 24 h, else `Nd`;
    - `old` from 48 h.
  - `fmtWhen(iso) -> string`: local time as `D Mon HH:MM`, for example `18 Nov 10:34`.
  - `fmtDuration(s) -> string`: `1h 0m` from 3600 s, `12m` from 60 s, else `Ns`.
  - **`boardTiles(prs, now) -> {needYou, critical, waiting, longest}`**: each tile is `{value, sub,
    alert}`, with the spec's values and sub-lines:
    - `critical` sorts PRs by critical, descending, shows at most two, then `…`;
    - `waiting` counts each login once per PR; with one login the sub-line is `all on <login>`, else
      `<login> <n> · …` by count, descending.
  - `findingsScale(prs) -> number`: the largest `critical+major+minor+question` over the PRs whose review
    is `done`/`posted`. Minimum 1.
  - `ticketLinkHtml(key, jiraUrl) -> string`: `<a href="<jiraUrl>/browse/<key>" target="_blank" rel="noopener">`;
    a `<span>` when there is no `jiraUrl`; `''` when there is no key.
  - `avatarsHtml(logins: string[], prUrl: string) -> string`: at most two avatars, then `+N`.
    - Each avatar is a span with the initials; on GitHub it also holds `<img src="https://github.com/<login>.png?size=40"
      alt="" loading="lazy">`, which a delegated `error` listener removes (Task 4). Never inline `onerror`.
  - **`moveText(pr, column, me) -> {logins: string[], text: string}`**:
    - my columns: `logins` is `[me]`; the text is my actions' words joined by `, `;
    - `review` with a `done` review at the head → `post the review from Telegram`;
    - `review` with a `done` re-check row job → `post the replies from Telegram`;
    - `fix` with a `done` address row job → `fix ready: push from Telegram`, or `replies ready: post from
      Telegram` when `job.fix` is false;
    - `others`: the distinct other logins; the text is `<login>: <words>` joined by `; `, or `nobody's
      turn`.
  - `findingsHtml(pr, scale) -> string`: the spec's Findings block; `''` when the PR has no
    `done`/`posted` review with findings.
  - `jobLineHtml(pr, now) -> string`: the spec's Job line from `pr.job`; `''` when nothing is happening.
  - **`buttonsHtml(scope, pr, where: 'card'|'panel') -> string`**: the spec's Card buttons, Fix now and
    panel rules. Each button carries `data-review-action` with one of `review-now`, `review-again`,
    `self-review`, `self-review-again`, `fix-now`, `fix-again`, `skip`, plus `data-scope` and
    `data-number`.
    - Disabled pending buttons read `Queued`/`Running`/`Deferred` for a review, and
      `Queued`/`Fixing`/`Deferred` for a fix.
    - On a card of my PR: only the fix button, and only when `can_fix`.
    - No GitHub button anywhere.
  - **`againConfirm(pr, action) -> {title, bodyHtml, label}`** for `review-again`, `self-review-again` and
    `fix-again`:
    - review: `Run a self-review of #N again at the same commit?` / `Review #N again at the same
      commit?`, with the body `The last one finished <fmtWhen>, took <fmtDuration> and cost $X.XX
      (API-equivalent). The new report replaces it.`; a missing value drops its clause;
    - fix: `Run the fix of #N again?`, with the body `The fix from <fmtWhen(job.started_at)> is replaced;
      push it from Telegram first if you want it.`;
    - label: `Run self-review` / `Review again` / `Run again`.
  - **`cardHtml(scope, pr, view) -> string`**, where `view = {me, jiraUrl, repoLabel, scale, now}`. It
    returns `<article class="reviews-card" tabindex="0" data-review-row="<scope>#<n>" data-fk="row:<scope>#<n>"
    data-scope data-number>` with:
    - the header line: `YOUR PR`/`REVIEWING`, the `#n` link (`target="_blank" rel="noopener"`), the
      ticket link, the flags `large`/`draft`/`pipeline`, and `repoLabel` when non-empty;
    - the title, in a `title` attribute too;
    - `findingsHtml`, `jobLineHtml`, the bottom line (`avatarsHtml` + `moveText.text` + `shortAge`,
      amber when `old`) and `buttonsHtml(…, 'card')`.

    Every interpolated value goes through `esc`.

**Fixture** (synthetic, in the test file). All PRs are on `https://github.com/acme-org/acme-app/pull/<n>`,
`head` `aaaaaaaaa`. The `now` is `2026-11-19T12:00:00Z`.

| PR | Roles | Turns | Review | `last_change` |
|---|---|---|---|---|
| #11 | author | `merge` | — | 3 h ago |
| #12 | author | `resolve_conflicts`, `request_review` | done, at the head, critical 4 / minor 1 / question 2 | 1 day ago |
| #13 | author | `request_review` | done, at the head, critical 15 / minor 3 / question 7 | 2 days ago |
| #14 | author | other: `octo-reviewer`, `recheck` | — | 2 days 1 hour ago |
| #15, #16 | reviewer | other: `octo-reviewer`, `answer_threads` | — | 1 day ago |
| #17 | reviewer | other: `octo-reviewer`, `merge` | — | 1 day ago |

- [ ] **Step 1: Write the failing tests** in `tests/js/reviews-board.test.mjs`:
  - `columnOf`:
    - `['review']` → review; `['recheck','merge']` → review;
    - `['resolve_conflicts','request_review']` → fix; `['merge','answer_threads']` → fix;
      `['brand_new_action']` → fix;
    - `['merge']` → merge;
    - `[]`, with or without other turns → others;
    - the fixture lands as review [], fix [12, 13], others [14, 15, 16, 17], merge [11].
  - `cardTitle`:
    - `('ACME-12: Area > Fix it','ACME-12')` → `'Area > Fix it'`;
    - `('ACME-12 - Fix','ACME-12')` → `'Fix'`;
    - `('Fix ACME-12','ACME-12')` unchanged;
    - `('ACME-12','ACME-12')` → `'ACME-12'`;
    - `('CI: x','')` → `'CI: x'`.
  - `actionWords`: `recheck` → `re-check`; `resolve_conflicts` → `resolve conflicts`; `brand_new_thing`
    → `brand new thing`.
  - `reportAtHead`: `done`, `posted`, `stale` at the head → true; `superseded` → false; another head →
    false; `review: null` → false.
  - `boardTiles` on the fixture:
    - `needYou` = `{value: 3, sub: '2 to fix · 1 to merge'}`;
    - `critical` = `{value: 19, sub: 'self-reviews: 15 in #13 · 4 in #12', alert: true}`;
    - `waiting` = `{value: 4, sub: 'all on octo-reviewer'}`;
    - `longest` = `{value: '2d', sub: '#14 · re-check'}`.
  - `boardTiles([])` → values `0`, `0`, `0`, `'—'`; `critical.sub === 'no self-reviews'`;
    `critical.alert === false`.
  - `boardTiles` with #15 held by `octo-reviewer` **and** `octo-dev`, and #16 by `octo-dev` only →
    `waiting.sub === 'octo-reviewer 3 · octo-dev 2'`.
  - `moveText(pr, 'others', 'octo')` with three other logins → `logins.length === 3`;
    `moveText(#12, 'fix', 'octo')` → `{logins: ['octo'], text: 'resolve conflicts, request review'}`; `avatarsHtml` of them contains two
    avatars and `+1`.
  - `avatarsHtml(['octo'], 'https://gitlab.com/x')` contains `>OC<` and no `<img`; with a GitHub URL it
    contains `https://github.com/octo.png?size=40` and no `onerror`.
  - `buttonsHtml`:
    - #12, card → `''`; with `can_fix: true` → contains `Fix now` and `data-review-action="fix-now"`;
      with `job: {kind:'address', state:'done'}` → `Fix again…` / `fix-again`;
    - #13, panel → `Self-review again…` / `self-review-again`;
    - a reviewer PR with a `posted` review at the head → `Review again…`; `large: true`, no review →
      `Review anyway`;
    - none contains `GitHub`.
  - `againConfirm`:
    - #13 with `review` `{finished_at:'2026-11-18T10:34:00Z', duration_s:3610, cost_usd:28.93}` → title
      `Run a self-review of #13 again at the same commit?`, a body containing `18 Nov 10:34`, `1h 0m` and
      `$28.93`, label `Run self-review`;
    - without `cost_usd`, the body has no `$`.
  - `shortAge`: 3 h → `{text:'3h', old:false}`; 47 h → `{text:'1d', old:false}`; 48 h → `{text:'2d',
    old:true}`.
  - `cardHtml` of a PR titled `<script>` contains `&lt;script&gt;`, and no raw `<script>`.

- [ ] **Step 2: Run them to verify they fail**

Run: `node --test tests/js/`
Expected: FAIL (`Cannot find module …/reviews-board.js`)

- [ ] **Step 3: Implement `dashboard/static/js/reviews-board.js`** with the exports above. Port the
  label logic from today's `reviews.js` (`JOB_LABEL`, `findingsText`, `actionsHtml`'s rules) rather than
  re-inventing it.

- [ ] **Step 4: Run them to verify they pass**

Run: `node --test tests/js/`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add dashboard/static/js/reviews-board.js tests/js/reviews-board.test.mjs
git commit -m "feat(dashboard): reviews board model: columns, tiles, cards, buttons"
```

---

### Task 4: Board view — `reviews.js`, route, CSS, docs

**Files:**
- Modify: `dashboard/static/js/reviews.js`, list part (≈1-260 and `onClick`, `runAction`, `mount`).
  Replace `VIEW_HTML`, `chipsHtml`, `matches`, `rowHtml`, `jobSubHtml`, `repoLineHtml`, `repoHtml` and
  `paintList`. Keep the panel code for Task 5.
- Modify: `dashboard/static/js/app.js`: `showReviews(fromHash, repo = null)`; `routeFromHash` passes
  the `arg`.
- Modify: `dashboard/static/style.css`: board classes; delete the `.reviews-table`, `.reviews-chip` and
  row-button rules no longer used.
- Modify: `docs/dashboard.md`, "Reviews view":
  - replace "### My turn / Waiting on others" with "### Board": columns, tiles, card, keys;
  - the buttons table: Review now / anyway / again…, Skip, Fix now / again…, with the confirmation, and
    no GitHub ↗;
  - add `fix-now` to the routes it names;
  - remove the filter chips and the row-hover text.

**Interfaces:**
- Consumes: Task 3's exports; `fixNow` from Task 2; `showConfirmDialog` from `./actions.js`.
- Produces:
  - `export function setReviewsRepo(scope: string | null)`: the repo filter.
  - `export function repoFilterOf(arg: string | null, repos: {scope}[]) -> string | null`: `arg` when it
    names a listed scope, else `null`.
  - The card markup keeps `data-review-row`, `data-fk`, `data-scope` and `data-number`, so `markRows`,
    `openRow`, `moveFocus`, `redraw` and `onKey` keep working unchanged.

**Layout:**
- `VIEW_HTML` holds, in order:
  1. `#switch-panel-container`;
  2. `.reviews-status-lines`: one line per repo, with the dot, scope, status word when not `ok`, `synced
     <timeAgo(last_poll_at)>` and the job count; the reason line under it; the search input at the
     right of the first line;
  3. `.reviews-repos`: the chips, only when there is more than one repo;
  4. `.reviews-tiles`;
  5. `.reviews-board`, four `.reviews-col` sections from `COLUMNS`, with a header (title, count, hint)
     and the cards or the dashed `empty` placeholder;
  6. the hint, the panel, the status line.
- Cards from all repos the filter shows are merged into the four columns. Each column is sorted by
  `last_change`, oldest first. `repoLabel` is the repo id (the part after `/`) only when there is more
  than one repo.
- **Search** hides the cards that do not match `#n`, title or ticket key. The column counts follow; the
  tiles do not.
- **Repo chips**: `All` plus each scope with its card count. A click sets `location.hash` to
  `#/reviews/<scope>`, or `#/reviews`; the existing `hashchange` router calls `showReviews(true, arg)` →
  `setReviewsRepo(arg)` → `renderReviews()`.
- **Clicks** (`onClick` → `runAction`):
  - `review-now` and `self-review` → `reviewNow`;
  - `skip` → `skipReview`;
  - `fix-now` → `fixNow`, with the status text `Queued a fix of #N at <head>`;
  - `review-again`, `self-review-again` and `fix-again` → `showConfirmDialog(...againConfirm(pr,
    action))` first, and the confirm runs the matching call;
  - **Avatars**: one delegated `error` listener on the view, in the capture phase, removes a failed
    `img` inside `.reviews-avatar`.
- **CSS**:
  - four equal columns (`grid-template-columns: repeat(4, minmax(0, 1fr))`), a dashed placeholder,
    cards with a 2-line title clamp;
  - the severity colors from the Global Constraints; amber `#e3b341` for an old age;
  - card buttons visible on `:hover`, `:focus-within`, `.open`, and always under `(hover: none)`, as the
    row buttons were;
  - at `max-width: 760px`, one column, stacked in the same order.

- [ ] **Step 1: Write the failing test** for `repoFilterOf` in `tests/js/reviews-board.test.mjs`, or in
  a sibling `tests/js/reviews-filter.test.mjs` if `reviews.js` cannot be imported in Node. In that case
  put `repoFilterOf` in `reviews-board.js` and re-export it.
  - `('acme/app', [{scope:'acme/app'}])` → `'acme/app'`;
  - `('old/repo', [{scope:'acme/app'}])` → `null`;
  - `(null, …)` → `null`.

- [ ] **Step 2: Run it to verify it fails**

Run: `node --test tests/js/`
Expected: FAIL (`repoFilterOf` is not exported)

- [ ] **Step 3: Implement** the view, the route, the CSS and the docs, per the Layout above.

- [ ] **Step 4: Run all tests**

Run: `node --test tests/js/ && timeout 300 $VENV_PY -m pytest tests/unit/test_dashboard_reviews.py tests/unit/test_reviewer_service.py -q`
Expected: all pass.

- [ ] **Step 5: Start a preview dashboard of this checkout on a copy of the live data.** It needs no
  daemon restart. Save this throwaway script **outside the repo**, for example in the session
  scratchpad as `preview_reviews.py`:

```python
"""Throwaway: this checkout's dashboard on a copy of the live reviewer.db, read-only to the original."""
import argparse, asyncio, sqlite3, tempfile
from pathlib import Path
from types import SimpleNamespace
import uvicorn
from config.schemas import ProcessPartConfig, ReviewerConfig
from dashboard.event_store import EventStore
from dashboard.events import EventBus
from dashboard.web import create_app
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.service import RepoContext, ReviewerService
from orchestrator.reviewer.store import ReviewerStore
from orchestrator.switches import Switches

p = argparse.ArgumentParser()
for name in ("--project", "--repo", "--base", "--doc", "--address-doc", "--jira"):
    p.add_argument(name, required=True)
p.add_argument("--port", type=int, default=8091)
a = p.parse_args()

async def main():
    tmp = Path(tempfile.mkdtemp())
    live = Path.home() / "CleaveData" / "reviewer" / "reviewer.db"
    sqlite3.connect(f"file:{live}?mode=ro", uri=True).backup(sqlite3.connect(tmp / "reviewer.db"))
    conn = connect(tmp / "reviewer.db")
    cfg = ReviewerConfig(enabled=True, base_branch=a.base,
                         process=ProcessPartConfig(doc=a.doc, address_doc=a.address_doc))
    service = ReviewerService([RepoContext(a.project, a.repo, cfg, None, None, None)], ReviewerStore(conn),
                              Switches(conn), lambda key: False, None, lambda project, repo: set())
    store = EventStore(str(tmp / "events.db"))
    await store.initialize()
    tracker = SimpleNamespace(provider="jira", jira=SimpleNamespace(url=a.jira))
    app = create_app(EventBus(), store, reviewer=service,
                     projects={a.project: SimpleNamespace(config=SimpleNamespace(tracker=tracker))})
    await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=a.port)).serve()

asyncio.run(main())
```

  Run it from the checkout root, with the values from `config-live/projects/<project>/project.yaml`
  (`tracker.jira.url`) and the repo YAML's `reviewer:` section. Use `run_in_background`:

```bash
PYTHONPATH=. $VENV_PY <scratchpad>/preview_reviews.py \
  --project <project> --repo <repo> --base <base_branch> --doc <process.doc> \
  --address-doc <process.address_doc> --jira <tracker.jira.url>
```

  Expected: Uvicorn serves on `http://127.0.0.1:8091`. It needs no other service. Skip, Review now and
  Fix now act on the copy only, and no job runs, because the preview has no runner.

- [ ] **Step 6: Verify the board in the preview.** Take screenshots with
  `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --force-dark-mode
  --window-size=1440,1400 --virtual-time-budget=4000 --screenshot=<scratchpad>/board.png
  "http://127.0.0.1:8091/#/reviews"`, and again at `--window-size=760,2400`. Read each image and check:
  - the live PRs land in the columns the spec's table names, and the tiles read as in the spec;
  - titles keep their full text, ticket keys link to `<jira>/browse/<key>`, `#n` links to GitHub;
  - the avatars load, or show initials;
  - no `Report ready` text and no raw action code (`resolve_conflicts`) anywhere;
  - at 760 px the columns stack;
  - with the panel open at 1440 px, the four columns fit the remaining width and no card overflows it;
  - `#/reviews/old/repo` shows the whole board (Review Focus 3).

  Then, in a real browser tab:
  - `j`/`k` walk the cards column by column; `Enter` opens the panel; `Esc` closes it; `/` focuses search;
  - typing a ticket key filters the cards and the column counts, not the tiles;
  - on a card with **Review again…**, click it, wait over 5 s (auto-refresh), and the dialog is still
    open; **Cancel** queues nothing (Review Focus 2).

  For Review Focus 1, change `--repo` to an id that has no board: the status line shows its state, the
  four placeholders show, and the tiles read 0 / `—`.

- [ ] **Step 7: Commit**

```bash
git add dashboard/static/js/reviews.js dashboard/static/js/reviews-board.js dashboard/static/js/app.js \
  dashboard/static/style.css docs/dashboard.md tests/js/
git commit -m "feat(dashboard): Reviews tab as a whose-move board"
```

---

### Task 5: Side panel header in the card's language

**Files:**
- Modify: `dashboard/static/js/reviews.js`: `panelHeadHtml`. Delete `turnChips`, `stateHtml` and
  `actionsHtml` once nothing uses them; `stateDot` stays for the posts and jobs tables.
- Modify: `dashboard/static/style.css` (panel header).
- Modify: `docs/dashboard.md`, "### PR detail view", the header part: the moves in words, the job line,
  the Jira link, Self-review / again…, Fix now / again…, no GitHub ↗, and the confirmation text.

**Interfaces:**
- Consumes: Task 3's `cardTitle`, `ticketLinkHtml`, `moveText`, `avatarsHtml`, `jobLineHtml`,
  `buttonsHtml(scope, pr, 'panel')`; Task 4's click handling (the panel's buttons use the same
  `data-review-action` values).
- Produces: `panelHeadHtml(target, pr)`. The "not on the board" branch is unchanged. It renders:
  - row 1: `← Back`, the `#n` link, `ticketLinkHtml`, `<n> files`, the flags;
  - row 2: the title, unclamped;
  - row 3: the avatars and the move text, the job line, a spacer, then `buttonsHtml(…, 'panel')`.

- [ ] **Step 1: Make the change** per the Interfaces. The pure parts are already covered by Task 3's
  tests.

- [ ] **Step 2: Verify in the preview** (Task 4, Step 5). Open each kind of PR and check its panel
  header:
  - **your PR with a self-review**: the header shows **Self-review again…**, and clicking it shows the
    dialog with the last run's time, duration and cost;
  - **your PR without one**: **Self-review**;
  - **another PR**: Review now / again… and Skip, as on its card;
  - every header: no `● Report ready`, no turn code, no GitHub ↗, and a Jira link on the ticket key;
  - the report, replies, posts and jobs below are unchanged.

- [ ] **Step 3: Run all tests**

Run: `node --test tests/js/ && timeout 300 $VENV_PY -m pytest tests --ignore=tests/e2e -q > <scratchpad>/pytest.log 2>&1; tail -5 <scratchpad>/pytest.log`
Expected: node passes. Pytest shows only the nine known environment failures.

- [ ] **Step 4: Commit and stop the preview server**

```bash
git add dashboard/static/js/reviews.js dashboard/static/style.css docs/dashboard.md
git commit -m "feat(dashboard): PR panel header speaks the board's language; Self-review and Fix now"
```

---

## After the branch

The live dashboard is served by the daemon. Static files are read from disk, but the API changes need a
**daemon restart** after the merge (`deploy/README.md`, "Start, stop, restart"). The restart is the
operator's call: ask before doing it.
