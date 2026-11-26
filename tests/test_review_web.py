"""The weekly review chat's pages: talk an item or the whole plan over, then tap (5c)."""

import pytest

from agentc import app as web
from agentc import notify, planner, planner_agent, review, store
from agentc.llm import OverBudget, Unreachable
from tests.test_actions_web import live  # noqa: F401 (the fixture)
from tests.test_app import KEY, NOW, client, events, speaks  # noqa: F401 (the fixtures)
from tests.test_plan_web import home  # noqa: F401 (the fixture)


def tok(scope):
    return notify.token(KEY, "review", scope)


def draft(db, scope, d):
    """Put a draft in the scope's chat, as a reply would."""
    chat = store.open_review(db, site_id=1, plan_id=1, item_id=None if scope == "all" else
                             int(scope), now=NOW)  # fmt: skip
    store.set_draft(db, chat_id=chat, draft=d.to_json())
    return chat


MOVE = planner.Change("move_timer", slot=2, time="07:30", from_time="06:00")


def test_the_plan_tab_links_each_item_and_the_whole_plan_chat(home):  # noqa: F811
    c, _, _, _ = home
    page = c.get("/plan").text
    assert page.count(">Talk it over</a>") == 3 and 'href="/plan/talk/3"' in page
    assert 'href="/plan/talk/all">Open the plan chat</a>' in page


def test_a_chat_opens_greeted_by_code_once(home):  # noqa: F811
    c, db, _, _ = home
    page = c.get("/plan/talk/3").text
    assert "Let&#39;s talk over Desk 2: No timer to Off at 23:00, on at 07:00, every day." in page
    assert "Talking over a plan item" in page and 'href="/plan"' in page
    assert 'hx-post="/plan/talk/3/say"' in page and f'value="{tok("3")}"' in page
    c.get("/plan/talk/3")
    assert len(store.turns(db, chat_id=1)) == 1
    whole = c.get("/plan/talk/all").text
    assert "this week&#39;s plan: 3 open items" in whole and "Talking over the whole plan" in whole


@pytest.mark.parametrize("scope", ["9", "x"])
def test_an_unknown_item_is_not_found(home, scope):  # noqa: F811
    c, _, _, _ = home
    assert c.get(f"/plan/talk/{scope}").status_code == 404


def test_no_chat_before_the_first_plan(client):  # noqa: F811
    c, _, _ = client
    assert c.get("/plan/talk/all").status_code == 404


def test_a_message_and_its_streamed_reply(home):  # noqa: F811
    c, db, _, _ = home
    c.get("/plan/talk/1")
    r = c.post("/plan/talk/1/say", data={"text": "Why 07:30?", "tok": tok("1")})
    assert "Why 07:30?" in r.text and f'sse-connect="/plan/talk/1/reply?tok={tok("1")}"' in r.text
    assert c.post("/plan/talk/1/say", data={"text": "Again", "tok": tok("1")}).status_code == 409
    web.app.dependency_overrides[web.stepper] = lambda: speaks(
        [{"type": "text", "text": "Nobody uses it."}]
    )
    got = events(c.get(f"/plan/talk/1/reply?tok={tok('1')}").text)
    assert got[0] == ("text", "Nobody uses it.") and "Nobody uses it.</div>" in got[-1][1]
    web.app.dependency_overrides[web.stepper] = lambda: None  # a reload: no model call
    assert "Nobody uses it." in events(c.get(f"/plan/talk/1/reply?tok={tok('1')}").text)[-1][1]
    assert [role for _, role, _ in store.turns(db, chat_id=1)] == ["agent", "owner", "agent"]


@pytest.mark.parametrize(
    ("brain", "said"),
    [(lambda: None, "No ANTHROPIC_API_KEY in .env"),
     (lambda: _raises(Unreachable("down")), "I can&#39;t reach the model now"),
     (lambda: _raises(OverBudget("month cap reached")), "model budget is used up"),
     (lambda: _raises(OverBudget("run cap reached")), "hit its cost cap"),
     (lambda: _raises(RuntimeError("bug")), "Something went wrong: bug")],
)  # fmt: skip
def test_a_reply_that_cannot_run_says_why(home, brain, said):  # noqa: F811
    c, db, _, _ = home
    c.post("/plan/talk/all/say", data={"text": "Hello", "tok": tok("all")})
    web.app.dependency_overrides[web.stepper] = brain
    assert said in events(c.get(f"/plan/talk/all/reply?tok={tok('all')}").text)[-1][1]
    assert store.turns(db, chat_id=1)[-1][1] == "owner"


def _raises(err):
    def send(**_):
        raise err

    return send


def test_forms_need_their_token_and_words(home):  # noqa: F811
    c, _, _, _ = home
    assert c.post("/plan/talk/1/say", data={"text": "x", "tok": "forged"}).status_code == 403
    assert c.post("/plan/talk/1/say", data={"text": " ", "tok": tok("1")}).status_code == 422
    assert c.post("/plan/talk/1/act", data={"tok": tok("2")}).status_code == 403


def test_a_drafted_change_is_done_only_on_the_tap(home):  # noqa: F811
    c, db, _, plug = home
    d = review.Draft("change", 1, "Plug-1", "Desk 2", MOVE, "On at 06:00 to On at 07:30",
                     "Saves about 0.1 kWh. In use then on 0 of 14 days.")  # fmt: skip
    chat = draft(db, "1", d)
    page = c.get("/plan/talk/1").text
    assert "On at 06:00 to On at 07:30" in page and ">Do it</button>" in page
    assert plug.timers[2]["Time"] == "06:00"  # nothing yet
    r = c.post("/plan/talk/1/act", data={"tok": tok("1")})
    assert "Done: Desk 2&#x27;s timer 2 now runs at 07:30 (was 06:00)." in r.text
    assert plug.timers[2]["Time"] == "07:30"
    assert store.plan_item(db, site_id=1, item_id=1).status == "done"  # pyright: ignore[reportOptionalMemberAccess]
    assert store.chat(db, chat_id=chat).draft is None  # pyright: ignore[reportOptionalMemberAccess]
    assert c.post("/plan/talk/1/act", data={"tok": tok("1")}).status_code == 409


def test_do_it_anyway_shows_its_warning(home):  # noqa: F811
    c, db, _, _ = home
    d = review.Draft("anyway", 3, "Plug-1", "Desk 2", planner.Change(
        "add_pair", off="23:00", on="07:00", days="every_day"), "No timer to Off at 23:00",
        "Saves about 0.4 kWh. In use in the new off time on 2 of 14 days.")  # fmt: skip
    draft(db, "3", d)
    page = c.get("/plan/talk/3").text
    assert ">Do it anyway</button>" in page and "would have got in the way" in page
    assert "now switches off at 23:00" in c.post("/plan/talk/3/act", data={"tok": tok("3")}).text


def test_keep_it_closes_the_item_with_the_owners_reason_for_next_week(home):  # noqa: F811
    c, db, _, _ = home
    d = review.Draft("keep", 3, "Plug-1", "Desk 2", None, "No timer to Off at 23:00", "",
                     "We game late on Fridays.")  # fmt: skip
    draft(db, "all", d)
    assert ">Keep it as it is</button>" in c.get("/plan/talk/all").text
    r = c.post("/plan/talk/all/act", data={"tok": tok("all")})
    assert "Kept as it is. Your reason goes into next week&#x27;s plan." in r.text
    assert store.plan_item(db, site_id=1, item_id=3).status == "not_now"  # pyright: ignore[reportOptionalMemberAccess]
    item = store.plan_item(db, site_id=1, item_id=3)
    assert item is not None
    assert store.reasons(db, mac="AA", since=NOW) == [(item.change, "We game late on Fridays.")]


def test_a_closed_item_or_a_gone_plug_changes_nothing(home):  # noqa: F811
    c, db, _, plug = home
    d = review.Draft("change", 1, "Plug-1", "Desk 2", MOVE, "x", "on 0 of 14 days")
    draft(db, "1", d)
    store.decide_item(db, item_id=1, status="not_now", now=NOW)
    assert "Nothing changed: that item is closed already." in c.post(
        "/plan/talk/1/act", data={"tok": tok("1")}).text  # fmt: skip
    draft(db, "all", replace_plug(d))
    assert "Not sent: that plug is not in config.toml" in c.post(
        "/plan/talk/all/act", data={"tok": tok("all")}).text  # fmt: skip
    assert plug.timers[2]["Time"] == "06:00"


def replace_plug(d):
    return review.Draft("anyway", 3, "Plug-99", d.device, d.change, d.what, d.saving)


def test_start_again_closes_the_chat(home):  # noqa: F811
    c, db, _, _ = home
    c.get("/plan/talk/1")
    r = c.post("/plan/talk/1/restart", data={"tok": tok("1")}, follow_redirects=False)
    assert r.status_code == 303 and store.chat(db, chat_id=1).status == "closed"  # pyright: ignore[reportOptionalMemberAccess]
    c.get("/plan/talk/1")
    assert store.open_review(db, site_id=1, plan_id=1, item_id=1, now=NOW) == 2


def test_week_notes_carry_the_reason_to_the_next_plan(home):  # noqa: F811
    _, db, _, _ = home
    store.decide_item(db, item_id=1, status="not_now", now=NOW, reason="I wake at 6.")
    from agentc import config  # noqa: PLC0415

    cfg = web.app.dependency_overrides[web.settings]()
    from agentc.agent_tools import Tools  # noqa: PLC0415

    t = planner_agent.PlanTools(Tools(db, None, cfg, 1, NOW), NOW)  # pyright: ignore[reportArgumentType]
    assert f"Not now: Desk 2: {MOVE.to_json()} (you said: I wake at 6.)" in t.week_notes()
    assert config.device(cfg, "Plug-1").name == "Desk 2"


def test_a_third_reply_at_once_is_told_to_wait(home):  # noqa: F811
    from agentc.web import chat  # noqa: PLC0415

    c, _, _, _ = home
    c.post("/plan/talk/1/say", data={"text": "Hello", "tok": tok("1")})
    chat.AT_ONCE.acquire()
    chat.AT_ONCE.acquire()
    try:
        said = events(c.get(f"/plan/talk/1/reply?tok={tok('1')}").text)[-1][1]
    finally:
        chat.AT_ONCE.release()
        chat.AT_ONCE.release()
    assert "Busy with another reply" in said


def test_a_tap_waits_for_a_reply_in_progress(home):  # noqa: F811
    from agentc.web import chat  # noqa: PLC0415

    c, db, _, _ = home
    d = review.Draft("change", 1, "Plug-1", "Desk 2", MOVE, "x", "s")
    chat_id = draft(db, "1", d)
    chat.RUNS[chat_id] = chat.Run()
    try:
        assert c.post("/plan/talk/1/act", data={"tok": tok("1")}).status_code == 409
    finally:
        del chat.RUNS[chat_id]


def test_a_streamed_draft_shows_its_own_button(home):  # noqa: F811
    c, _, _, _ = home
    c.get("/plan/talk/3")
    c.post("/plan/talk/3/say", data={"text": "Leave it, I game late.", "tok": tok("3")})
    keep = {"action": "keep", "item": 3, "device": "Desk 2", "kind": "move_timer", "timer": 0,
            "time": "", "off": "", "on": "", "days": [], "reason": "I game late.",
            "message": "I will leave it."}  # fmt: skip
    web.app.dependency_overrides[web.stepper] = lambda: speaks(
        [{"type": "tool_use", "id": "p", "name": "propose", "input": keep}])  # fmt: skip
    done = events(c.get(f"/plan/talk/3/reply?tok={tok('3')}").text)[-1][1]
    assert ">Keep it as it is</button>" in done and "Your reason: I game late." in done
