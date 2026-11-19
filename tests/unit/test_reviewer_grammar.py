# tests/unit/test_reviewer_grammar.py
from __future__ import annotations
import pytest
from orchestrator.reviewer.grammar import ReplyCommand as C, check_command, describe, parse_reply, retry_text
from orchestrator.reviewer.report import parse_report
from tests.unit.test_reviewer_report import HEAD, REPORT

@pytest.mark.parametrize("text, command", [
    ("пости 1 2 4 апрув", C("post", (1, 2, 4), "APPROVE")),
    ("пости 1 2 4, вопросы 3 4, ставь апрув", C("post", (1, 2, 3, 4), "APPROVE")),
    ("post 1, 2 and 4", C("post", (1, 2, 4))),
    ("постни коментарии 1 2 и вопросы 4", C("post", (1, 2, 4))),
    ("опублікуй #3.", C("post", (3,))),
    ("post 2 request changes", C("post", (2,), "REQUEST_CHANGES")),
    ("пости 1 реквест ченджес", C("post", (1,), "REQUEST_CHANGES")),
    ("Пости все подтверждённые апрув", C("post", (), "APPROVE", all_confirmed=True)),
    ("post all confirmed 4 no summary", C("post", (4,), all_confirmed=True, no_summary=True)),
    ("апрув", C("post", (), "APPROVE")),
    ("апрув без summary", C("post", (), "APPROVE", no_summary=True)),
    ("пости 1 апрув всё равно", C("post", (1,), "APPROVE", anyway=True)),
    ("текст 2", C("text", (2,))),
    ("измени 2: Короче, без вводной фразы", C("edit", (2,), instruction="Короче, без вводной фразы")),
    ("edit 3: mention the retry rule", C("edit", (3,), instruction="mention the retry rule")),
    ("черновик 7", C("draft", (7,))),
    ("пости ответы 1 3", C("post_replies", (1, 3))),
    ("post replies 1, 3", C("post_replies", (1, 3))),
    ("ревью заново", C("review_again")), ("review again", C("review_again")), ("пропусти", C("skip")),
    ("post 1 2.", C("post", (1, 2))), ("post 1 2!", C("post", (1, 2))),
    ("approve and", C("post", (), "APPROVE")),
])
def test_grammar_known(text, command):
    assert parse_reply(text) == command

@pytest.mark.parametrize("text", [
    "пости всё кроме 3", "пости 1 2?", "что с 3?", "posst 1 2", "пости", "post approve request changes",
    "измени 2", "текст", "черновик 7 8", "post replies 1 approve", "пости 1-3", "1 2 4", "approve 1 2", "",
    # More than one trailing dot or "!" is not a plain full stop.
    "post 1 2...", "post 1 2…", "post 1 2!!", "post 1 2.!",
    # The event-only form takes conjunctions only: no bare approve from "approve comments".
    "approve comments", "апрув и вопросы",
    # A negation, a look-alike letter from the other alphabet, or a glued number is not known.
    "не пости 1", "don't post 1", "пoсти 1", "аpprove", "пости1", "post #1#2",
])
def test_grammar_unknown_goes_to_llm(text):
    assert parse_reply(text) is None

def test_check_command():
    report = parse_report(REPORT, HEAD)  # items 1-6 (1-3 confirmed), dropped 7-9
    check = lambda cmd, **kw: check_command(cmd, kw.get("kind", "review"), report, [], kw.get("posted", set()),
                                            kw.get("drafted", set()))
    assert check(C("post", (), "APPROVE", all_confirmed=True)) == C("post", (1, 2, 3), "APPROVE")
    assert check(C("post", (12,))) == "Unknown number: 12. Nothing was done."
    assert check(C("post", (7,))) == '#7 was dropped: reply "draft 7" first. Nothing was done.'
    assert check(C("post", (7,)), drafted={7}) == C("post", (7,))
    assert check(C("post", (1, 2)), posted={2}) == "#2 is already posted at this head. Nothing was done."
    assert check(C("draft", (3,))) == "#3 is not a dropped number. Nothing was done."
    assert check(C("post", (), "REQUEST_CHANGES", no_summary=True)).startswith("Nothing to post")
    assert check(C("post_replies", (1,))).startswith("This is a review message")
    assert check_command(C("post", (1,)), "recheck", None, [{"n": 1, "body": "x"}], set(), set()).startswith(
        "This is a re-check message")

def test_check_command_refusals():
    report = parse_report(REPORT, HEAD)  # items 1-6, dropped 7-9
    check = lambda cmd, drafted=frozenset(): check_command(cmd, "review", report, [], set(), set(drafted))
    assert check(C("post", (12, 13))) == "Unknown numbers: 12 13. Nothing was done."
    assert check(C("draft", (7,)), drafted={7}) == '#7 is drafted already: reply "text 7". Nothing was done.'
    assert check(C("draft", (7,))) == C("draft", (7,))
    assert check(C("text", (8,))) == '#8 was dropped: reply "draft 8" first. Nothing was done.'
    assert check(C("edit", (7,), instruction="shorter")) == (
        '#7 was dropped: reply "draft 7" first. Nothing was done.')
    edit_one = 'Edit one number at a time: "edit 2: …". Nothing was done.'
    assert check(C("edit", (1, 2), instruction="shorter")) == edit_one
    assert check(C("edit", (2,), instruction="  ")) == edit_one
    # Shapes only the LLM can produce: the wrong count of numbers.
    assert check(C("text", ())) == 'Name a number: "text 2". Nothing was done.'
    draft_one = 'Draft one number at a time: "draft 7". Nothing was done.'
    assert check(C("draft", ())) == draft_one and check(C("draft", (7, 8))) == draft_one
    assert check_command(C("post_replies", ()), "recheck", None, [{"n": 1, "body": "x"}], set(), set()) == (
        'Name the replies: "post replies 1 3". Nothing was done.')
    assert check_command(C("post_replies", (4,)), "recheck", None, [{"n": 1, "body": "x"}], set(), set()) == (
        "Unknown number: 4. Nothing was done.")

def test_all_confirmed_leaves_out_the_posted_ones():
    # Final review M3: "post all confirmed" is the confirmed numbers not posted yet; named ones stay refused.
    report = parse_report(REPORT, HEAD)  # 1-3 confirmed
    check = lambda cmd, posted: check_command(cmd, "review", report, [], posted, set())
    assert check(C("post", (), "APPROVE", all_confirmed=True), {1}) == C("post", (2, 3), "APPROVE")
    assert check(C("post", (4,), all_confirmed=True), {1, 2, 3}) == C("post", (4,))
    nothing_left = "Nothing left to post: every confirmed finding is already posted at this head. Nothing was done."
    assert check(C("post", (), all_confirmed=True), {1, 2, 3}) == nothing_left
    assert check(C("post", (), "APPROVE", all_confirmed=True), {1, 2, 3}) == nothing_left
    assert check(C("post", (1,), all_confirmed=True), {1}) == "#1 is already posted at this head. Nothing was done."
    assert check(C("post", (1, 4)), {1}) == "#1 is already posted at this head. Nothing was done."

def test_text_on_a_recheck():
    # A re-check message shows each reply's gist only: "text N" sends the drafted reply in full (final review F2).
    plan = [{"n": 1, "thread_id": "T1", "body": "Fixed.", "resolve": True}]
    recheck = lambda cmd: check_command(cmd, "recheck", None, plan, set(), set())
    assert recheck(C("text", (1,))) == C("text", (1,))
    assert recheck(C("text", (9,))) == "Unknown number: 9. Nothing was done."
    for cmd in (C("post", (1,)), C("edit", (1,), instruction="shorter"), C("draft", (1,))):
        assert recheck(cmd) == 'This is a re-check message: reply "post replies 1 3". Nothing was done.'

def test_describe_and_retry():
    assert describe(C("post", (1, 2, 4), "APPROVE"), HEAD) == "post 1 2 4 + APPROVE at 26c4929c5"
    assert describe(C("post", (), "APPROVE", no_summary=True), HEAD) == "APPROVE without a body at 26c4929c5"
    assert retry_text((3, 4), "APPROVE") == "post 3 4 approve" and retry_text((3,), "COMMENT") == "post 3"


@pytest.mark.parametrize("event, flags", [
    ("REQUEST_CHANGES", {"no_summary": True}), ("COMMENT", {"anyway": True}),
    ("APPROVE", {"no_summary": True, "anyway": True}),
])
def test_retry_text_keeps_the_flags(event, flags):
    # Task 9 fix round 1: the retry of a partial post is the command it was, for the rest.
    text = retry_text((2, 5), event, **flags)
    assert parse_reply(text) == C("post", (2, 5), event, **flags)


# --- Address jobs ---

@pytest.mark.parametrize("text, command", [
    ("push", C("push")), ("Пуш.", C("push")), ("commit and push", C("push")), ("запушь", C("push")),
    ("закоммить и запушь", C("push")), ("diff", C("diff")), ("дифф", C("diff")), ("патч", C("diff")),
    ("again", C("again")), ("заново: не трогай 2", C("again", instruction="не трогай 2")),
    ("Again: leave 2 as it is", C("again", instruction="leave 2 as it is")),
    ("переделай: shorter replies\nand no emoji", C("again", instruction="shorter replies\nand no emoji")),
    ("заново ревью", C("review_again")),
])
def test_address_grammar(text, command):
    assert parse_reply(text) == command

@pytest.mark.parametrize("text", ["push 1 2", "push anyway", "diff 3", "again 2", "commit", "again:"])
def test_address_grammar_unknown(text):
    assert parse_reply(text) is None

def test_check_command_on_an_address_message():
    replies = [{"n": 1, "thread_id": "T1", "body": "x"}]
    for cmd in (C("push"), C("diff"), C("again"), C("text", (1,)), C("post_replies", (1,))):
        assert check_command(cmd, "address", None, replies, set(), set()) == cmd
    for cmd in (C("post", (1,)), C("edit", (1,), instruction="x"), C("draft", (1,))):
        assert check_command(cmd, "address", None, replies, set(), set()) == (
            'This is a threads message: reply "push", "post replies 1 3", "text 2", "diff" or "again: …". '
            "Nothing was done.")
    assert check_command(C("post_replies", (2,)), "address", None, replies, set(), set()) == \
        "Unknown number: 2. Nothing was done."
    assert check_command(C("post_replies", (1,)), "address", None, replies, {1}, set()) == \
        "#1 is already posted at this head. Nothing was done."
    for kind in ("review", "recheck"):
        for action in ("push", "diff", "again"):
            assert check_command(C(action), kind, None, replies, set(), set()) == \
                f'"{action}" works on a fix message of your own PR (threads or self-review findings). Nothing was done.'

def test_describe_address_commands():
    assert [describe(C(a), HEAD) for a in ("push", "diff", "again")] == ["push", "diff", "again"]
    assert describe(C("again", instruction="leave 2"), HEAD) == 'again: "leave 2"'

def test_not_understood_on_an_address_message():
    from orchestrator.reviewer.lines import not_understood_line
    assert not_understood_line("address") == (
        'Not understood; nothing was done. Reply like "push", "post replies 1 3", "text 2", "diff" or "again: …".')


# --- Findings jobs ---

@pytest.mark.parametrize("text", ["fix", "Fix", "FIX.", "fix findings", "Fix Findings", "пофикси", "почини", "фикс",
                                  "пофікси", "полагодь"])
def test_fix_grammar(text):
    assert parse_reply(text) == C("fix")


@pytest.mark.parametrize("text", ["fix 2", "fix and push", "please fix", "fix findings 3", "fix: leave 2"])
def test_fix_is_the_whole_reply_only(text):
    assert parse_reply(text) is None


FINDINGS_REFUSED = 'This is a findings message: reply "push", "diff" or "again: …". Nothing was done.'


def test_check_command_on_a_findings_message():
    for cmd in (C("push"), C("diff"), C("again"), C("again", instruction="leave 3")):
        assert check_command(cmd, "findings", None, [], set(), set()) == cmd
    for cmd in (C("post", (1,)), C("post", (), "APPROVE"), C("text", (1,)), C("post_replies", (1,)),
                C("edit", (1,), instruction="x"), C("draft", (1,))):
        assert check_command(cmd, "findings", None, [], set(), set()) == FINDINGS_REFUSED


def test_fix_only_on_a_review_message():
    report = parse_report(REPORT, HEAD)
    assert check_command(C("fix"), "review", report, [], set(), set()) == C("fix")
    for kind in ("recheck", "address", "findings"):
        assert check_command(C("fix"), kind, None, [], set(), set()) == (
            '"fix" works on the message of a self-review of your own PR. Nothing was done.')


def test_push_diff_and_again_on_a_review_message_name_both_fix_messages():
    report = parse_report(REPORT, HEAD)
    for action in ("push", "diff", "again"):
        assert check_command(C(action), "review", report, [], set(), set()) == (
            f'"{action}" works on a fix message of your own PR (threads or self-review findings). Nothing was done.')


def test_describe_fix():
    assert describe(C("fix"), HEAD) == "fix"


def test_not_understood_on_a_findings_message():
    from orchestrator.reviewer.lines import not_understood_line
    assert not_understood_line("findings") == 'Not understood; nothing was done. Reply like "push", "diff" or "again: …".'
