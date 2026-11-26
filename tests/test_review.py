"""The weekly review chat's agent: it explains, weighs another time, and drafts (5c)."""

from typing import Any

import pytest

from agentc import evals, planner, planner_agent, review, store
from agentc.devices.base import WEEK
from agentc.llm import Llm

REACH = evals.PLANS[0]  # the TV and the PeoTV box set to write, on at 06:00, off at 01:00
TV_MOVE = evals.TV_MOVE


@pytest.fixture
def house():
    """The eval house on Sunday 15 Nov with a plan: the box's start moved (ready), the TV's
    off moved earlier (only to talk over), and the printer's wall switch (an action)."""
    return evals.review_case(evals.settings())


def use(name, args, n=1):
    return {"type": "tool_use", "id": f"t{n}", "name": name, "input": args}


def say(text):
    return [{"type": "text", "text": text}]


class Script:
    def __init__(self, *turns, stop="end_turn"):
        self.turns, self.seen, self.stop = list(turns), [], stop

    def __call__(self, *, tier, system, messages, tools, on_text=None):
        self.seen.append({"tier": tier.model, "tools": [t["name"] for t in tools],
                          "messages": list(messages)})  # fmt: skip
        blocks = self.turns.pop(0)
        for b in blocks:
            if b["type"] == "text" and on_text:
                on_text(b["text"])
        return blocks, self.stop, 1000, 100


def run(house, script, item: int | None = 2, talk=None):
    cfg, t = house
    brain = Llm(t.base.app, 1, cfg.tz, cfg.tiers, cfg.budget, script, clock=lambda: evals.SUNDAY)
    scope = review.Scope(1, item)
    talk = talk or [("agent", review.greeting(t, scope)), ("owner", "Why this one?")]
    events = []
    r = review.reply(talk, scope, tools=t, llm=brain, run_id="review:test",
                     on_event=lambda kind, text: events.append((kind, text)))  # fmt: skip
    return r, script, events


def change(
    device="LG TV", kind="move_timer", timer: int = 2, time="", off="", on="", days=()
) -> dict[str, object]:
    return {
        "device": device,
        "kind": kind,
        "timer": timer,
        "time": time,
        "off": off,
        "on": on,
        "days": list(days),
    }


def propose(action, item=2, reason="", message="Here it is.", **kw):
    return [use("propose", {"action": action, "item": item, **change(**kw), "reason": reason,
                            "message": message}, 9)]  # fmt: skip


# what it starts from


def test_the_greetings_and_the_brief_come_from_code(house):
    _, t = house
    assert review.greeting(t, review.Scope(1, 2)) == (
        "Let's talk over LG TV: Off at 01:00, every day to Off at 23:30, every day. Saves about"
        " 0.06 kWh. In use in the new off time on 3 of 14 days. What would you like to know?"
    )
    assert review.greeting(t, review.Scope(1, 3)).startswith(
        "Let's talk over 3D printer: Switch it off at the wall")  # fmt: skip
    assert review.greeting(t, review.Scope(1, None)).startswith(
        "Let's talk over this week's plan: 3 open items.")  # fmt: skip
    one, whole = review.brief(t, review.Scope(1, 2)), review.brief(t, review.Scope(1, None))
    assert "Item 2: LG TV:" in one and "Item 1:" not in one and "(only to talk over; open)" in one
    assert "LG TV now: timer 1: On at 06:00" in one and "Goal: November:" in one
    assert "Item 1: PeoTV box" in whole and "Item 3: 3D printer" in whole


def test_one_item_talks_on_the_small_model_the_whole_plan_on_the_mid_one(house):
    _, s1, _ = run(house, Script(say("It idles late.")))
    _, s2, _ = run(house, Script(say("It idles late.")), item=None)
    assert (s1.seen[0]["tier"], s2.seen[0]["tier"]) == ("claude-haiku-5-5", "claude-sonnet-5-5")


# answers


def test_an_answer_is_checked_for_made_up_numbers_and_said_again(house):
    r, script, events = run(house, Script(say("It saves 42 kWh."), say("It idles late.")))
    assert r.text == "It idles late." and r.draft is None
    assert "numbers not in the brief" in script.seen[1]["messages"][-1]["content"]
    assert ("step", "checking its numbers") in events


def test_system_words_are_said_again_too(house):
    r, script, _ = run(house, Script(say("The Planner chose it."), say("It idles late.")))
    assert r.text == "It idles late." and "system word" in script.seen[1]["messages"][-1]["content"]


@pytest.mark.parametrize("stop", ["end_turn", "refusal"])
def test_an_empty_or_refused_reply_asks_again(house, stop):
    blocks = [] if stop == "end_turn" else say("No.")
    r, _, _ = run(house, Script(blocks, stop=stop))
    assert r.text == review.LOST


def test_a_chat_that_never_finishes_says_so(house):
    r, script, _ = run(house, Script(*[say("It saves 42 kWh.")] * review.MAX_STEPS))
    assert r.text.startswith("Sorry, I could not finish") and script.seen[-1]["tools"] == [
        "propose"
    ]


def test_each_thing_is_read_once_and_kept_for_the_next_reply(house):
    look = use("usual", {"device": "LG TV", "day": "Friday", "start": "22:00", "end": "02:00"})
    r, script, events = run(house, Script([look], [{**look, "id": "t2"}], say("It idles late.")))
    assert (
        r.tools == ("usual",)
        and len(r.reads) == 1
        and ("step", "looking at its usual week") in events
    )
    assert "You read this already" in script.seen[2]["messages"][-1]["content"][0]["content"]


# trying another time


def test_try_change_replays_and_checks_a_time(house):
    _, t = house
    maybe = review.try_change(change(time="23:30"), t, review.Scope(1, 2))
    assert maybe == (
        "Off at 01:00, every day to Off at 23:30, every day: Saves about 0.06 kWh,"
        " LKR 3.0, 0.03 kgCO2e a month. In use in the new off time on 3 of 14 days."
        " Allowed only as do it anyway, after the owner hears the days. (item 2)"
    )
    late = review.try_change(change(time="22:00"), t, review.Scope(1, 2))
    assert late.endswith("Not allowed: LG TV was in use in the new off time on 4 of 14 days.")
    box = review.try_change(change(device="PeoTV box", time="00:30"), t, review.Scope(1, 1))
    assert box.endswith("on 0 of 13 days. Allowed. (item 1)")


@pytest.mark.parametrize(
    ("args", "why"),
    [(change(device="Toaster"), "Error: no device named 'Toaster'"),
     (change(device="PeoTV box"), "Error: PeoTV box has no item in this chat"),
     (change(timer=7), "Error: LG TV has no timer 7"),
     ({**change(), "kind": "remove_timer"}, "Error: kind is move_timer or add_pair"),
     ({"device": "LG TV"}, "Error: 'kind'")],
)  # fmt: skip
def test_try_change_says_what_is_wrong(house, args, why):
    _, t = house
    assert review.try_change(args, t, review.Scope(1, 2)) == why


def test_a_new_pair_can_be_tried(house):
    _, t = house
    weekdays = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
    got = review.try_change(change(kind="add_pair", timer=0, off="23:30", on="07:00",
                                   days=weekdays), t, review.Scope(1, None))  # fmt: skip
    assert got.startswith("No timer to Off at 23:30, on at 07:00, Monday to Friday: ")


# drafts


def test_a_ready_change_is_drafted_after_a_try(house):
    box: dict[str, Any] = {"device": "PeoTV box", "time": "00:30"}
    script = Script([use("try_change", change(**box))],
                    propose("change", item=1, message="Off at 00:30.", **box))  # fmt: skip
    r, _, events = run(house, script, item=1)
    d = r.draft
    assert d is not None and (d.action, d.item, d.plug, d.ready) == ("change", 1, "Plug-4", True)
    assert d.change == planner.Change("move_timer", slot=2, time="00:30", from_time="01:00")
    assert r.text == "Off at 00:30." and ("step", "trying that change") in events
    assert review.Draft.from_json(d.to_json()) == d


def test_a_change_that_would_get_in_the_way_is_not_drafted_as_ready(house):
    r, script, _ = run(house, Script(propose("change", time="23:30"), say("Another time?")))
    assert r.draft is None
    out = script.seen[1]["messages"][-1]["content"][0]
    assert out["is_error"] and "use anyway, after the owner agrees" in out["content"]


def test_do_it_anyway_only_for_an_item_to_talk_over(house):
    r, _, _ = run(house, Script(propose("anyway", message="Off at 23:30 every day then.")))
    assert r.draft is not None and (r.draft.action, r.draft.ready) == ("anyway", False)
    assert r.draft.change == TV_MOVE
    script = Script(propose("anyway", item=1, device="PeoTV box", timer=1),
                    say("It is ready: tap Do it on the plan."))  # fmt: skip
    r, script, _ = run(house, script, item=None)
    out = script.seen[1]["messages"][-1]["content"][0]["content"]
    assert r.draft is None and "this item is ready" in out


def test_keep_it_needs_the_owners_reason_in_their_words(house):
    r, _, _ = run(house, Script(propose("keep", reason="We watch late at weekends.")))
    assert r.draft is not None and (r.draft.action, r.draft.reason) == (
        "keep", "We watch late at weekends.")  # fmt: skip
    assert (
        r.draft.change is None
        and r.draft.what == "Off at 01:00, every day to Off at 23:30, every day"
    )
    r, _, _ = run(house, Script(propose("keep", item=3, device="3D printer",
                                        reason="It prints overnight.")), item=None)  # fmt: skip
    assert r.draft is not None and r.draft.what == planner_agent.ACTION


@pytest.mark.parametrize(
    ("args", "why"),
    [({"action": "keep", "reason": ""}, "keep needs the owner's reason"),
     ({"action": "keep", "reason": "The Planner said so."}, "'Planner' is a system word"),
     ({"action": "skip"}, "action is one of change, anyway, keep"),
     ({"action": "keep", "item": 9, "reason": "No."}, "item 9 is not an open item of this chat"),
     ({"action": "anyway", "message": "Saves 42 kWh."}, "numbers not in"),
     ({"action": "change", "time": "00:30", "device": "PeoTV box", "timer": 1},
      "item 2 is about LG TV, not PeoTV box"),
     ({"action": "anyway", "item": 2}, None)],
)  # fmt: skip
def test_a_bad_draft_goes_back_with_why(house, args, why):
    action, rest = args["action"], {k: v for k, v in args.items() if k != "action"}
    r, script, _ = run(house, Script(propose(action, **rest), say("Let me ask again.")))
    if why is None:
        assert r.draft is not None
        return
    assert r.draft is None and why in script.seen[1]["messages"][-1]["content"][0]["content"]


def test_a_closed_item_cannot_be_drafted(house):
    _, t = house
    store.decide_item(t.base.app, item_id=2, status="not_now", now=evals.SUNDAY)
    with pytest.raises(ValueError, match="not an open item"):
        review.parse({"action": "anyway", "item": 2}, t, review.Scope(1, 2))


def test_a_draft_for_a_device_whose_timers_do_not_fire_or_are_gone_is_refused(house):
    _, t = house
    store.plug_seen(t.base.app, site_id=1, mac=evals.TV[1], name=evals.TV[0], ip="192.0.2.13",
                    timers_on=False, now=evals.SUNDAY)  # fmt: skip
    with pytest.raises(ValueError, match="not allowed: .*Timers switch off"):
        review.parse({"action": "anyway", "item": 2}, t, review.Scope(1, 2))
    store.save_timers(t.base.app, site_id=1, mac=evals.TV[1], now=evals.SUNDAY, timers=[])
    with pytest.raises(ValueError, match="has no timer 2"):
        review.parse({"action": "change", "item": 2, **change(time="00:30")}, t,
                     review.Scope(1, 2))  # fmt: skip


def test_what_was_read_earlier_in_the_chat_comes_with_the_brief(house):
    cfg, t = house
    script = Script(say("It idles late."))
    brain = Llm(t.base.app, 1, cfg.tz, cfg.tiers, cfg.budget, script, clock=lambda: evals.SUNDAY)
    talk = [("agent", "Hello."), ("owner", "Why?"), ("agent", "It idles."), ("owner", "When?")]
    review.reply(talk, review.Scope(1, 2), tools=t, llm=brain, run_id="r",
                 earlier=[('usual {"device": "LG TV"}', "Friday: 2 W at 23:00")])  # fmt: skip
    first = script.seen[0]["messages"][0]["content"]
    assert "What you read earlier in this chat (still true):" in first and "Friday: 2 W" in first
    assert [m["role"] for m in script.seen[0]["messages"]] == ["user", "assistant", "user"]


def test_a_change_said_not_now_to_cannot_be_drafted_again(house):
    _, t = house
    refused = planner.Change("move_timer", slot=2, time="00:30", from_time="01:00").to_json()
    t.base.app.execute("INSERT INTO plan_items (plan_id, site_id, plug_mac, change, why,"
                       " saving, status, decided) VALUES (1, 1, ?, ?, 'w', 's', 'not_now', ?)",
                       (evals.BOX[1], refused, evals.SUNDAY.isoformat()))  # fmt: skip
    got = review.try_change(change(device="PeoTV box", time="00:30"), t, review.Scope(1, 1))
    assert "Not allowed: the owner said Not now to this" in got


def test_anyway_only_for_a_change_a_tap_can_send(house):
    _, t = house
    t.base.app.execute("UPDATE plan_items SET change = ? WHERE id = 2",
                       (planner.Change("remove_timer", slot=2).to_json(),))  # fmt: skip
    with pytest.raises(ValueError, match="cannot be done from the chat yet"):
        review.parse({"action": "anyway", "item": 2}, t, review.Scope(1, 2))


def test_a_pair_on_any_days_can_be_tried_and_drafted(house):
    _, t = house
    sun_thu = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday")
    got = review.try_change(change(kind="add_pair", timer=0, off="23:30", on="07:00",
                                   days=sun_thu), t, review.Scope(1, None))  # fmt: skip
    assert got.startswith("No timer to Off at 23:30, on at 07:00, Sunday to Thursday: ")
    bad = review.try_change(change(kind="add_pair", timer=0, off="23:30", on="07:00", days=()),
                            t, review.Scope(1, None))  # fmt: skip
    assert bad == "Error: days: one or more day names, Monday to Sunday"
    every = review._days(list(reversed(WEEK)))  # pyright: ignore[reportPrivateUsage]
    assert (every, review._days(["Saturday", "Sunday"])) == ("every_day", "weekends")  # pyright: ignore[reportPrivateUsage]
