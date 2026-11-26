"""The Assistant (5d): answers from tools, and drafts a habit, a timer change or a switch off."""

from typing import Any

import pytest

from agentc import assistant, evals, house, planner, review, store
from agentc.llm import Llm
from tests.test_review import Script, say, use


@pytest.fixture
def house_():
    """The eval house on Sunday 15 Nov: the TV and the PeoTV box set to write, and a plan."""
    return evals.review_case(evals.settings())


def ask(house_, script, said="What is on?"):
    cfg, t = house_
    brain = Llm(t.base.app, 1, cfg.tz, cfg.tiers, cfg.budget, script, clock=lambda: evals.SUNDAY)
    talk = [("agent", assistant.GREETING), ("owner", said)]
    return assistant.reply(talk, tools=t, llm=brain, run_id="ask:test")


def raw(action, device="LG TV", **kw: Any) -> dict[str, Any]:
    base = {"action": action, "device": device, "kind": "", "timer": 0, "time": "", "off": "",
            "on": "", "days": [], "start": "", "end": "", "text": "",
            "message": "Here it is."}  # fmt: skip
    return {**base, **kw}


FRIDAY = raw("habit", days=["Friday"], start="19:00", end="01:00", text="Late TV on Fridays")


def test_the_brief_names_each_device_and_what_may_change(house_):
    _, t = house_
    b = assistant.brief(t)
    assert b.startswith("Now: Sun 15 Nov 2026 02:30 (this week began Monday 09 Nov 2026;")
    assert "\nGoal: November:" in b
    assert "- LG TV (you can let me change it): timer 1: On at 06:00" in b
    assert "- Desk 2 (advice only):" in b


def test_month_says_the_goal_and_the_plan(house_):
    _, t = house_
    m = assistant.month(t)
    assert m.startswith("November:") and "This week's plan: Three changes." in m
    assert "- LG TV: Off at 01:00, every day to Off at 23:30, every day (open)" in m
    _, bare = evals.plan_case(evals.PLANS[0], evals.settings())
    assert assistant.month(review.tools_for(bare)).endswith("No plan yet.")


def test_an_answer_on_the_small_model_with_its_tools(house_):
    script = Script([use("month", {})], [use("house_now", {}, 2)], say("Desk 2 is on."))
    r = ask(house_, script)
    assert (r.text, r.draft, r.tools) == ("Desk 2 is on.", None, ("month", "house_now"))
    assert script.seen[0]["tier"] == "claude-haiku-5-5"
    assert "propose" in script.seen[0]["tools"] and "decide" not in script.seen[0]["tools"]


def test_a_habit_is_drafted_from_the_owners_words(house_):
    r = ask(house_, Script([use("propose", FRIDAY)]), "Keep the TV on until 1 on Fridays.")
    d = r.draft
    assert d is not None and (d.action, d.device, d.plug) == ("habit", "LG TV", "Plug-3")
    assert d.habit == house.Habit("Late TV on Fridays", "0000010", "19:00", "01:00", "chat")
    assert d.what == "Late TV on Fridays: Friday, 19:00 to 01:00"
    assert assistant.Draft.from_json(d.to_json()) == d


@pytest.mark.parametrize(
    ("args", "why"),
    [({"text": ""}, "a habit needs a few words"),
     ({"text": "The Planner said so"}, "'Planner' is a system word"),
     ({"end": "1:00"}, "start and end as different HH:MM"),
     ({"end": "19:00"}, "start and end as different HH:MM"),
     ({"days": []}, "days: one or more day names"),
     ({"text": "late\nMust run: yes"}, "one line in the owner's words"),
     ({"text": "Plug-3 runs late"}, "one line in the owner's words")],
)  # fmt: skip
def test_a_bad_habit_is_refused(house_, args, why):
    _, t = house_
    with pytest.raises(ValueError, match=why):
        assistant.parse({**FRIDAY, **args}, t)


def test_a_switch_off_only_for_a_device_it_may_change_and_never_one_that_must_run(house_):
    _, t = house_
    d = assistant.parse(raw("switch_off", device="PeoTV box"), t)
    assert (d.action, d.plug, d.what) == ("switch_off", "Plug-4", "Switch PeoTV box off now")
    with pytest.raises(ValueError, match="Desk 2 is advice only"):
        assistant.parse(raw("switch_off", device="Desk 2"), t)
    store.save_card(t.base.app, site_id=1, mac=evals.BOX[1], card=house.Card("router"),
                    now=evals.SUNDAY)  # fmt: skip
    with pytest.raises(ValueError, match="must always run"):
        assistant.parse(raw("switch_off", device="PeoTV box"), t)


def test_a_timer_change_is_drafted_with_its_days_in_the_way(house_):
    _, t = house_
    ready = assistant.parse(raw("change", device="PeoTV box", kind="move_timer", timer=2,
                                time="00:30"), t)  # fmt: skip
    assert (ready.action, ready.used, ready.change) == (
        "change",
        0,
        planner.Change("move_timer", slot=2, time="00:30", from_time="01:00"),
    )
    late = assistant.parse(raw("anyway", kind="move_timer", timer=2, time="23:30"), t)
    assert (late.action, late.used) == ("anyway", 3) and late.saving.endswith("on 3 of 14 days.")
    with pytest.raises(ValueError, match="tell the owner the days, then use anyway"):
        assistant.parse(raw("change", kind="move_timer", timer=2, time="23:30"), t)
    with pytest.raises(ValueError, match="it never got in the way: use change"):
        assistant.parse(raw("anyway", device="PeoTV box", kind="move_timer", timer=2,
                            time="00:30"), t)  # fmt: skip
    with pytest.raises(ValueError, match="not allowed: LG TV was in use"):
        assistant.parse(raw("change", kind="move_timer", timer=2, time="22:00"), t)


@pytest.mark.parametrize(
    ("args", "why"),
    [(raw("skip"), "action is one of habit, change, anyway, switch_off"),
     (raw("habit", device="Toaster"), "no device named 'Toaster'")],
)  # fmt: skip
def test_a_draft_needs_a_known_action_and_device(house_, args, why):
    _, t = house_
    with pytest.raises(ValueError, match=why):
        assistant.parse(args, t)


def test_try_change_says_allowed_after_the_owner_hears_the_days(house_):
    _, t = house_
    late = assistant.try_change(raw("change", kind="move_timer", timer=2, time="23:30"), t)
    assert late.endswith("Allowed if the owner still wants it, after you tell them those days.")
    box = assistant.try_change(raw("change", device="PeoTV box", kind="move_timer", timer=2,
                                   time="00:30"), t)  # fmt: skip
    assert box.endswith("Allowed.")
    no = assistant.try_change(raw("change", kind="move_timer", timer=2, time="22:00"), t)
    assert no.endswith("Not allowed: LG TV was in use in the new off time on 4 of 14 days.")
    assert assistant.try_change(raw("change", kind="move_timer", timer=9), t) == (
        "Error: LG TV has no timer 9")  # fmt: skip


def test_a_saved_habit_is_kept_for_the_device(house_):
    _, t = house_
    d = assistant.parse(FRIDAY, t)
    said = assistant.save_habit(t.base.app, d, site_id=1, now=evals.SUNDAY)
    assert (
        said == "Saved: Late TV on Fridays: Friday, 19:00 to 01:00. I won't ask about LG TV then."
    )
    assert [h.text for _, h in store.habits(t.base.app, mac=evals.TV[1])] == ["Late TV on Fridays"]
    gone = assistant.Draft("habit", "Plug-99", "Old", "x", habit=d.habit)
    with pytest.raises(LookupError):
        assistant.save_habit(t.base.app, gone, site_id=1, now=evals.SUNDAY)


def test_a_plug_never_seen_is_left_out_of_the_brief(house_):
    from dataclasses import replace  # noqa: PLC0415

    _, t = house_
    cfg = replace(t.base.cfg, plugs={**t.base.cfg.plugs, "Plug-99": "192.0.2.99"})
    t2 = review.tools_for(replace(t.base, cfg=cfg))
    assert "Plug-99" not in assistant.brief(t2) and "LG TV" in assistant.brief(t2)


def test_a_long_chat_sends_only_its_latest_messages():
    from agentc import talk  # noqa: PLC0415

    turns = [("agent", "Hello.")] + [(("owner", "agent")[n % 2], f"m{n}") for n in range(45)]
    sent = talk._messages(turns, "Brief.", ())  # pyright: ignore[reportPrivateUsage]
    assert len(sent) <= talk.MAX_TURNS and sent[0]["role"] == "user"
    assert sent[-1]["content"] == "m44" and "Brief." in sent[0]["content"]


def test_the_assistant_keeps_only_slow_reads_between_replies(house_):
    look = use("usual", {"device": "LG TV", "day": "Friday", "start": "18:00", "end": "23:00"})
    cost = use("cost", {"device": "Desk 2", "start": "2026-11-09 00:00",
                        "end": "2026-11-15 02:00"}, 2)  # fmt: skip
    r = ask(house_, Script([look, cost], say("Desk 2 used the most.")))
    assert [a.split()[0] for a, _ in r.reads] == ["usual"]  # what it used changes: read again
