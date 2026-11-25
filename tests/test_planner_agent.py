"""The weekly Planner's agent half: it proposes, code measures and checks (ADR-010, 5b2)."""

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from agentc import cli, evals, llm, planner, planner_agent, store
from agentc.llm import Llm
from agentc.owner import Choice, Decision
from agentc.watcher import Event
from tests.test_cli import EXAMPLE

WRITE = evals.PLANS[0]  # the TV and the PeoTV box set to write
BOX = {"device": "PeoTV box", "kind": "move_timer", "slot": 1, "time": "07:00", "off": None,
       "on": None, "days": None}  # fmt: skip


def use(name, args, n=1):
    return {"type": "tool_use", "id": f"t{n}", "name": name, "input": args}


class Script:
    """A stand-in model: each step's blocks in turn."""

    def __init__(self, *turns):
        self.turns, self.seen = list(turns), []

    def __call__(self, *, tier, system, messages, tools, on_text=None):
        self.seen.append({"messages": list(messages), "tools": [t["name"] for t in tools],
                          "tier": tier.model})  # fmt: skip
        return self.turns.pop(0), "tool_use", 2000, 200


def propose(items, summary="A quiet week.", n=9):
    return [use("propose_plan", {"summary": summary, "items": items}, n)]


def run(script, pe=WRITE):
    cfg, tools = evals.plan_case(pe, evals.settings())
    budget = replace(cfg.budget, run_usd=cfg.budget.plan_usd)
    brain = Llm(tools.app, 1, cfg.tz, cfg.tiers, budget, script, clock=lambda: evals.SUNDAY)
    return planner_agent.plan(tools, llm=brain, run_id="plan:test"), tools, cfg


def test_a_good_item_is_measured_by_code_and_kept():
    item = {**BOX, "why": "It idles from 06:00 and nobody uses it before 07:00."}
    script = Script([use("house_week", {}), use("what_if", BOX, 2)], propose([item]))
    p, _, _ = run(script)
    (i,) = p.items
    assert (p.done, p.summary, p.tools) == (True, "A quiet week.", ("house_week", "what_if"))
    assert (i.plug, i.change, i.before, i.after) == (
        "Plug-4", planner.Change("move_timer", slot=1, time="07:00"),
        "On at 06:00, every day", "On at 07:00, every day")  # fmt: skip
    assert i.saving.startswith("Saves about 0.02 kWh") and "on 0 of 13 days" in i.saving
    assert script.seen[0]["tier"] == "claude-sonnet-5-5"  # the mid tier


def test_a_blocked_item_goes_back_once_then_only_good_items_stay():
    bad = {**BOX, "device": "Desk 2", "why": "It runs late."}  # access advice
    good = {**BOX, "why": "It idles from 06:00 and nobody uses it before 07:00."}
    script = Script([use("what_if", BOX)], propose([bad, good]), propose([bad, good], n=10))
    p, _, _ = run(script)
    back = script.seen[2]["messages"][-1]["content"][0]
    assert back["is_error"] and back["content"].startswith("Blocked: item 1 (Desk 2): access:")
    assert [i.plug for i in p.items] == ["Plug-4"] and p.blocked[0].startswith("item 1 (Desk 2)")


def test_a_number_from_nowhere_is_blocked():
    made_up = {**BOX, "why": "It wastes 93 W every morning."}
    p, _, _ = run(Script(propose([made_up]), propose([made_up], n=10)))
    assert p.items == () and "why: not in the tool results: 93" in p.blocked[0]


def test_a_made_up_summary_gives_way_to_one_from_code():
    p, _, _ = run(Script(propose([], summary="You used 42 kWh."), propose([], "x 77", n=10)))
    assert (p.summary, p.items) == ("Nothing to change this week.", ())


def test_system_words_go_back_for_plain_ones():
    techy = {**BOX, "why": "Its access is write and it idles from 06:00."}
    script = Script(propose([techy], summary="The Planner found one."), propose([], n=10))
    p, _, _ = run(script)
    back = script.seen[1]["messages"][-1]["content"][0]["content"]
    assert "summary: 'Planner' is a system word" in back and "'access' is a system word" in back
    assert (p.summary, p.items) == ("A quiet week.", ())  # the plain retry


def test_a_change_that_cut_real_use_is_kept_only_to_talk_over():
    late = {**BOX, "slot": 2, "time": "00:00", "why": "It stays on until 01:00."}
    p, _, _ = run(Script([use("what_if", {**BOX, "slot": 2, "time": "00:00"})], propose([late])))
    (i,) = p.items
    assert not i.ready and "In use in the new off time on 1 of 13 days" in i.saving


def test_items_sent_as_text_are_read_and_odd_ones_refused():
    good = {**BOX, "why": "It idles from 06:00 and nobody uses it before 07:00."}
    text = json.dumps([good, "not an item"])
    p, _, _ = run(Script(propose(text), propose(text, n=10)))
    assert [i.plug for i in p.items] == ["Plug-4"] and p.blocked == (
        "item 2: an item is an object with device, kind and why",)  # fmt: skip
    for bad in ("[not json", {"device": "x"}):
        p, _, _ = run(Script(propose(bad), propose(bad, n=10)))
        assert p.items == () and p.blocked == ("items: not a list of changes",)


def test_an_unknown_device_is_named():
    p, _, _ = run(Script(propose([{**BOX, "device": "Kettle", "why": "x"}]),
                         propose([], n=10)))  # fmt: skip
    assert p.done and p.items == ()


def test_a_plan_over_3_items_keeps_3():
    why = "It idles."
    items = [{**BOX, "time": "09:30", "why": why}, {**BOX, "slot": 2, "time": "00:30", "why": why},
             {**BOX, "device": "LG TV", "time": "07:30", "why": why},
             {**BOX, "device": "LG TV", "slot": 2, "time": "00:00", "why": why}]  # fmt: skip
    p, _, _ = run(Script(propose(items), propose(items, n=10)))
    assert len(p.items) == 3 and "format: at most 3 items a week" in p.blocked


def test_wrong_value_types_are_refused_not_a_crash():
    odd = [{**BOX, "time": 700, "why": "x"}, {**BOX, "slot": [1], "why": "x"},
           {**BOX, "kind": "add_pair", "off": 200, "on": "07:00", "days": ["x"], "why": "x"},
           {**BOX, "kind": None, "why": "x"}, {**BOX, "why": None}]  # fmt: skip
    p, _, _ = run(Script(propose(odd), propose(odd, n=10)))
    assert p.done and p.items == () and len(p.blocked) == 5
    assert "why: one sentence in plain words is needed" in p.blocked[-1]


def test_a_slot_sent_as_text_is_read():
    item = {**BOX, "slot": "1", "why": "It idles from 06:00 and nobody uses it before 07:00."}
    p, _, _ = run(Script(propose([item])))
    assert [i.change.slot for i in p.items] == [1]


def test_the_same_change_or_the_same_timer_twice_is_kept_once():
    why = "It idles from 06:00 and nobody uses it before 07:00."
    twice = [{**BOX, "why": why}, {**BOX, "why": why}, {**BOX, "time": "09:00", "why": "It idles."}]
    p, _, _ = run(Script(propose(twice), propose(twice, n=10)))
    assert [i.change.time for i in p.items] == ["07:00"]
    assert sum("that timer has a change already" in b for b in p.blocked) == 2


def test_a_block_on_the_last_step_keeps_what_passed():
    good = {**BOX, "why": "It idles from 06:00 and nobody uses it before 07:00."}
    bad = {**BOX, "device": "Desk 2", "why": "x"}
    steps = [[use("house_week", {}, n)] for n in range(planner_agent.MAX_STEPS - 1)]
    p, _, _ = run(Script(*steps, propose([good, bad])))
    assert p.done and [i.plug for i in p.items] == ["Plug-4"]


def test_no_tool_call_gets_a_nudge_and_a_planner_that_never_proposes_is_not_done():
    words = [{"type": "text", "text": "Let me think."}]
    script = Script(*[words] + [[use("house_week", {}, n)] for n in range(9)])
    p, _, _ = run(script)
    assert script.seen[1]["messages"][-1]["content"] == "Use the tools, then call propose_plan."
    assert script.seen[-1]["tools"] == ["propose_plan"] and not p.done


def test_a_bad_tool_call_comes_back_as_an_error():
    script = Script([use("what_if", {**BOX, "kind": "swap"}), use("what_if", {"device": "x"}, 2),
                     use("nope", {}, 3)], propose([]))  # fmt: skip
    run(script)
    errors = [r["content"] for r in script.seen[1]["messages"][-1]["content"]]
    assert errors[0].startswith("Error: kind is one of") and errors[1].startswith("Error:")
    assert errors[2] == "Error: no tool named 'nope'"


def test_the_plan_is_saved_once_a_week():
    item = {**BOX, "why": "It idles from 06:00 and nobody uses it before 07:00."}
    p, tools, cfg = run(Script(propose([item])))
    plan = planner_agent.save(tools.app, p, site_id=1, cfg=cfg, now=evals.SUNDAY)
    assert plan == 1 and store.latest_plan(tools.app, site_id=1)[1] == "2026-11-09"  # pyright: ignore[reportOptionalSubscript]
    assert planner_agent.save(tools.app, p, site_id=1, cfg=cfg, now=evals.SUNDAY) is None
    assert planner_agent.save(tools.app, replace(p, done=False), site_id=1, cfg=cfg,
                              now=evals.SUNDAY + timedelta(days=7)) is None  # fmt: skip


def test_week_notes_hold_notes_answers_and_not_nows():
    _, tools = evals.plan_case(WRITE, evals.settings())
    app, mac = tools.app, evals.BOX[1]
    week = planner_agent.week_start(evals.SUNDAY, tz=evals.TZ)
    store.add_audit(app, site_id=1, at=week + timedelta(days=1), actor="agent", action="note",
                    ref="event:1", detail={"text": "The box idled all morning."})  # fmt: skip
    e = Event("left_on", mac, week + timedelta(days=2), {"minutes": 40, "peak_w": 2.0})
    store.add_event(app, site_id=1, event=e, dedupe="e")
    d = Decision("ask", "The box is on. Off?", (Choice("Off", "switch_off_now"),
                                                 Choice("Fine", "save_habit")))  # fmt: skip
    store.add_question(app, site_id=1, event_id=1, mac=mac, decision=d,
                       send_at=week + timedelta(days=2), now=week)  # fmt: skip
    store.answer_question(app, question_id=1, answer=1, now=week + timedelta(days=2))
    store.add_plan(
        app,
        site_id=1,
        week="2026-11-02",
        summary="s",
        now=week,
        items=[(mac, planner.Change("remove_timer", slot=2).to_json(), "w", "s", True)],
    )
    store.decide_item(app, item_id=1, status="not_now", now=week)
    text = planner_agent.PlanTools(tools, week).week_notes()
    assert text == ("Note: The box idled all morning.\n"
                    "Asked: The box is on. Off? Answer: Off\n"
                    'Not now: PeoTV box: {"kind": "remove_timer", "slot": 2}')  # fmt: skip


def test_an_empty_week_says_so_and_a_timers_switch_off_shows():
    _, tools = evals.plan_case(WRITE, evals.settings())
    store.plug_seen(tools.app, site_id=1, mac=evals.TV[1], name=evals.TV[0], ip="192.0.2.13",
                    timers_on=False, now=evals.SUNDAY)  # fmt: skip
    t = planner_agent.PlanTools(tools, planner_agent.week_start(evals.SUNDAY, tz=evals.TZ))
    assert t.week_notes() == "No notes, answers or Not nows this week."
    assert "every day (Timers switch off)" in t.house_week()


def test_options_show_the_furthest_ready_move_and_one_to_talk_over():
    _, tools = evals.plan_case(WRITE, evals.settings())
    t = planner_agent.PlanTools(tools, planner_agent.week_start(evals.SUNDAY, tz=evals.TZ))
    box = t.run("options", {"device": "PeoTV box"}).splitlines()
    assert box[0].startswith("Timer 1 (On at 06:00, every day): ready: move to 09:30. Saves")
    assert box[1].startswith("Timer 1 (On at 06:00, every day): to talk over: move to 10:00.")
    assert box[2].startswith("Timer 2 (Off at 01:00, every day): ready: move to 00:30.")
    tv = t.run("options", {"device": "LG TV"})
    assert "ready" not in tv and "to talk over: move to 07:30" in tv
    assert (
        t.run("options", {"device": "Desk 2"})
        == "The owner has not let you change Desk 2's timers."
    )


def test_options_say_when_nothing_helps_or_there_are_no_timers():
    _, tools = evals.plan_case(WRITE, evals.settings())
    t = planner_agent.PlanTools(tools, planner_agent.week_start(evals.SUNDAY, tz=evals.TZ))
    flat = [planner.Timer(1, True, 0, "19:00", 0, "1111111", True, 1, 1)]  # on in the evening
    store.save_timers(tools.app, site_id=1, mac=evals.TV[1], timers=flat, now=evals.SUNDAY)
    assert "no move saves energy without cutting use" in t.run("options", {"device": "LG TV"})
    store.save_timers(tools.app, site_id=1, mac=evals.TV[1], timers=[], now=evals.SUNDAY)
    assert t.run("options", {"device": "LG TV"}) == "LG TV has no timers set at a clock time."


def test_the_week_starts_on_monday_local():
    assert planner_agent.week_start(evals.SUNDAY, tz=evals.TZ).isoformat() == (
        "2026-11-09T00:00:00+05:30")  # fmt: skip


# agentc plan


@pytest.fixture
def home(tmp_path):
    """The example config, with an empty energy.db beside it; the command makes app.db."""
    path = tmp_path / "config.toml"
    path.write_text(EXAMPLE.read_text())
    sqlite3.connect(tmp_path / "energy.db").execute(
        "CREATE TABLE readings (ts_utc, plug, power_w, total_kwh, error)"
    )
    return path


def test_agentc_plan_needs_a_key(home, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cli.main(["--config", str(home), "plan"]) == 1
    assert capsys.readouterr().out == cli.NO_KEY + "\n"


def test_agentc_plan_shows_the_plan_and_saves_nothing(home, capsys):
    code = cli.main(["--config", str(home), "plan"], step=Script(propose([], "A quiet week.")))
    out = capsys.readouterr().out
    assert code == 0 and out.startswith("A quiet week.\n") and "Cost: USD" in out
    db = store.connect(home.parent / "app.db")
    assert db.execute("SELECT COUNT(*) FROM plans").fetchone() == (0,)


def test_agentc_plan_says_when_the_model_cannot_go_on(home, capsys):
    def gone(**kw):
        raise llm.Unreachable("no internet")

    assert cli.main(["--config", str(home), "plan"], step=gone) == 1
    assert "The Planner stopped: no internet" in capsys.readouterr().out


def test_agentc_plan_says_when_it_did_not_finish(home, capsys):
    stuck = Script(*[[use("house_week", {}, n)] for n in range(10)])
    assert cli.main(["--config", str(home), "plan"], step=stuck) == 1
    assert "The Planner did not finish a plan." in capsys.readouterr().out


def test_agentc_plan_prints_each_item_and_what_was_left_out(home, capsys, monkeypatch):
    item = planner_agent.Item("Plug-1", "AA", planner.Change("move_timer", slot=1, time="07:00"),
                              "It idles.", "Saves about 0.02 kWh a month.",
                              "On at 06:00, every day", "On at 07:00, every day")  # fmt: skip
    done = planner_agent.Plan("One change.", (item,), ("what_if",), ("item 2 (TV): used",))
    monkeypatch.setattr(planner_agent, "plan", lambda tools, llm, run_id: done)
    assert cli.main(["--config", str(home), "plan"], step=Script()) == 0
    out = capsys.readouterr().out
    assert "1. Desk 2: It idles.\n   Now: On at 06:00, every day\n   After: On at 07:00" in out
    assert "Left out: item 2 (TV): used" in out and "Tools: what_if." in out


def test_any_error_measuring_an_item_is_a_reason_not_a_crash(monkeypatch):
    def broken(self, plug, mac, c):
        raise ValueError("bad readings")

    monkeypatch.setattr(planner_agent.PlanTools, "result", broken)
    item = {**BOX, "why": "It idles."}
    p, _, _ = run(Script(propose([item]), propose([item], n=10)))
    assert p.done and p.blocked == ("item 1 (PeoTV box): format: bad readings",)
