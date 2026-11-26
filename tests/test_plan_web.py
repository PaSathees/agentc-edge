"""The Plan tab: Do it, Done and Not now on each item, and putting a timer back (5b5)."""

from dataclasses import replace

import pytest

from agentc import app as web
from agentc import notify, planner, planner_agent, store
from agentc.devices.tasmota import TasmotaPlug
from tests.test_actions_web import live  # noqa: F401 (the fixture)
from tests.test_app import KEY, NOW, client  # noqa: F401 (the fixture)

MOVE = planner.Change("move_timer", slot=2, time="07:30", from_time="06:00")
PAIR = planner.Change("add_pair", off="23:00", on="07:00", days="every_day")


def plan(db, *items, week="2026-11-02"):
    return store.add_plan(db, site_id=1, week=week, summary="Two changes and one for you.",
                          items=list(items), now=NOW)  # fmt: skip


def move(ready=True):
    return ("AA", MOVE.to_json(), "Nobody uses it before 07:30.", "Saves about 0.08 kWh.",
            ready, "On at 06:00, every day", "On at 07:30, every day")  # fmt: skip


ACTION = ("AA", planner_agent.action_json(planner_agent.ACTION), "It idles at 2 W.",
          "Saves about 0.95 kWh.", True, "", planner_agent.ACTION)  # fmt: skip
TALK = ("AA", PAIR.to_json(), "It is in use late on 2 days.", "Saves about 0.4 kWh.", False,
        "No timer", "Off at 23:00, on at 07:00, every day")  # fmt: skip


@pytest.fixture
def home(live):  # noqa: F811 (the fixture)
    """Desk 2 with the owner's own timer 2 (on at 06:00), as on the plug and as stored; a
    plan with a ready move, an action and one to talk over."""
    c, db, said, plug = live
    plug.timers[2].update(Enable=1, Time="06:00", Days="1111111", Repeat=1, Action=1)
    store.save_timers(db, site_id=1, mac="AA", now=NOW,
                      timers=TasmotaPlug(plug.ip).timers().timers)  # fmt: skip
    plan(db, move(), ACTION, TALK)
    return c, db, said, plug


def tap(c, item, does, tok=None):
    return c.post(f"/plan/{item}/{does}", data={"tok": tok or notify.token(KEY, "plan", item,
                                                                           does)})  # fmt: skip


def page(c):
    return " ".join(c.get("/plan").text.split())


def test_the_plan_shows_each_item_with_only_the_buttons_it_allows(home):
    c, _, _, _ = home
    p = page(c)
    assert "Two changes and one for you." in p and "Made Mon 02 Nov 07:00" in p
    assert "On at 06:00, every day → <b>On at 07:30, every day</b>" in p
    assert p.count(">Do it</button>") == 1 and p.count(">Done</button>") == 1
    assert p.count(">Not now</button>") == 3 and 'class="tag to-talk-over"' in p
    assert "only to talk over" in p and '<a href="/plan" aria-current="page">Plan</a>' in p
    assert "This week's plan: 3 of 3 open." in c.get("/").text


def test_do_it_moves_the_timer_and_it_can_be_put_back_once(home):
    c, db, _, plug = home
    r = tap(c, 1, "do")
    assert "Done: Desk 2&#39;s timer 2 now runs at 07:30 (was 06:00)." in r.text
    assert plug.timers[2]["Time"] == "07:30"
    assert store.plan_item(db, site_id=1, item_id=1).status == "done"  # pyright: ignore[reportOptionalMemberAccess]
    p = page(c)
    assert 'Desk 2: timer 2 moved to 07:30 <span class="muted">· undo until Tue 03 Nov 07:00' in p
    ((wid, *_),) = store.timer_undoable(db, site_id=1, since=NOW)
    url, tok = f"/plan/undo/{wid}", notify.token(KEY, "plan_undo", wid)
    assert "timers are back as they were" in c.post(url, data={"tok": tok}).text
    assert plug.timers[2]["Time"] == "06:00"
    assert c.post(url, data={"tok": tok}).status_code == 409
    assert c.post(url, data={"tok": "forged"}).status_code == 403
    assert "This week's plan: 2 of 3 open." in c.get("/").text


def test_done_and_not_now_close_an_item_and_a_second_tap_changes_nothing(home):
    c, db, _, _ = home
    assert "Marked done." in tap(c, 2, "done").text
    assert "Not now: it will not come back for 30 days." in tap(c, 3, "not_now").text
    assert "Nothing changed: this item is not now already." in tap(c, 3, "do").text
    assert [i.status for i in store.plan_items(db, plan_id=1)] == ["open", "done", "not_now"]
    assert page(c).count("</button>") == 2  # Do it and Not now, on the one still open


@pytest.mark.parametrize(
    ("item", "does", "tok", "code"),
    [(1, "do", "forged", 403), (1, "maybe", None, 403), (9, "do", None, 404),
     (3, "do", None, 409), (2, "do", None, 409), (1, "done", None, 409)],
)  # fmt: skip
def test_each_tap_needs_its_token_a_real_item_and_a_button_it_allows(home, item, does, tok,
                                                                     code):  # fmt: skip
    c, _, _, _ = home
    assert tap(c, item, does, tok).status_code == code


def test_in_shadow_mode_do_it_is_recorded_and_the_item_stays_open(home):
    c, db, _, plug = home
    store.set_shadow(db, site_id=1, on=True, now=NOW)
    assert "Recorded: move Desk 2&#39;s timer 2 to 07:30." in tap(c, 1, "do").text
    assert plug.timers[2]["Time"] == "06:00"
    assert store.plan_item(db, site_id=1, item_id=1).status == "open"  # pyright: ignore[reportOptionalMemberAccess]


def test_a_failed_write_keeps_the_item_open_to_try_again(home):
    c, db, _, plug = home
    plug.offline = True
    web.app.dependency_overrides[web.connect] = lambda: lambda ip: TasmotaPlug(ip, timeout_s=0.3)
    assert "Could not move Desk 2" in tap(c, 1, "do").text
    assert store.plan_item(db, site_id=1, item_id=1).status == "open"  # pyright: ignore[reportOptionalMemberAccess]


def test_a_ready_pair_is_set_in_free_slots(home):
    c, db, _, plug = home
    plan(db, (*TALK[:4], True, *TALK[5:]), week="2026-11-09")  # the older items expire
    assert "now switches off at 23:00, on at 07:00, every day" in tap(c, 4, "do").text
    assert plug.timers[9]["Time"] == "07:00" and plug.timers[10]["Time"] == "23:00"
    assert 'off at 23:00, on at 07:00 <span class="muted">· undo until' in page(c)
    assert [i.status for i in store.plan_items(db, plan_id=1)] == ["expired"] * 3


def test_a_change_that_cannot_be_sent_or_a_plug_gone_from_config(home):
    c, db, _, _ = home
    gone = ("AA", planner.Change("remove_timer", slot=2).to_json(), "w", "s", True, "", "")
    plan(db, gone, week="2026-11-09")
    r = tap(c, 4, "do")
    assert (r.status_code, r.json()["detail"]) == (409, "This change cannot be sent from here yet.")
    cfg = web.app.dependency_overrides[web.settings]()
    web.app.dependency_overrides[web.settings] = lambda: replace(cfg, plugs={})
    assert "Not sent: that plug is not in config.toml any more." in tap(c, 4, "do").text
    assert ">Do it</button>" not in page(c)  # a change Do it cannot send: no button


def test_a_message_on_the_page_must_come_from_the_hub(home):
    c, _, _, _ = home
    assert "Pay here" not in c.get("/plan?said=Pay%20here&sig=forged").text


def test_a_move_after_the_owner_changed_the_timer_by_hand_is_refused(home):
    c, db, _, plug = home
    plug.timers[2]["Time"] = "06:15"  # by hand, and the daily read stored it
    store.save_timers(db, site_id=1, mac="AA", now=NOW,
                      timers=TasmotaPlug(plug.ip).timers().timers)  # fmt: skip
    gone = ("AA", planner.Change("move_timer", slot=2, time="07:30", from_time="06:00").to_json(),
            "w", "s", True, "", "")  # fmt: skip
    plan(db, gone, week="2026-11-09")
    assert "changed since the plan was made" in tap(c, 4, "do").text
    assert plug.timers[2]["Time"] == "06:15"


def test_an_undo_after_a_change_by_hand_puts_nothing_back(home):
    c, db, _, plug = home
    tap(c, 1, "do")
    plug.timers[2]["Time"] = "08:00"  # by hand, after AgentC moved it
    ((wid, *_),) = store.timer_undoable(db, site_id=1, since=NOW)
    r = c.post(f"/plan/undo/{wid}", data={"tok": notify.token(KEY, "plan_undo", wid)})
    assert "were changed on the plug since, so nothing was put back" in r.text
    assert plug.timers[2]["Time"] == "08:00"


def test_before_the_first_plan_and_for_an_empty_one(client):  # noqa: F811 (the fixture)
    c, db, _ = client
    assert "No plan yet. The first one comes on Sunday at 18:00" in page(c)
    assert "This week's plan" not in c.get("/").text
    plan(db)
    assert "Nothing to change this week." in page(c)


def test_a_timer_written_without_its_old_timers_cannot_be_put_back(home):
    c, db, _, _ = home
    store.add_audit(db, site_id=1, at=NOW, actor="code", action="write", ref="plug:Plug-1",
                    detail={"does": "move_timer", "slot": "2", "time": "07:30"})  # fmt: skip
    assert "Timer changes, last day" not in page(c)
