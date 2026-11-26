"""The weekly Planner: code chooses with CP-SAT, the model explains (ADR-003 revised, 5b4)."""

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from agentc import cli, evals, jobs, llm, notify, planner, planner_agent, store
from agentc.house import Card
from agentc.llm import Llm
from agentc.owner import Choice, Decision
from agentc.watcher import Event
from tests.test_cli import EXAMPLE

REACH, ON_TRACK, SHORT = evals.PLANS  # goal 3.7 (reachable), 50 (on track), 0.5 (out of reach)
CHOICES: dict[str, planner_agent.Choice] = {}
REAL_DECIDE = planner_agent.decide


@pytest.fixture(autouse=True)
def once(monkeypatch):
    """Code's choice for each eval house is worked out once: CP-SAT gives the same set."""
    real = planner_agent.decide

    def cached(t):
        box = t.base.cfg.devices.get(evals.BOX[0])
        key = f"{t.base.cfg.goal}|{box.access if box else '-'}|{sorted(t.base.cfg.plugs)}"
        if key not in CHOICES:
            CHOICES[key] = real(t)
        return CHOICES[key]

    monkeypatch.setattr(planner_agent, "decide", cached)


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


def propose(items, summary="A quiet week.", left_out=(), n=9):
    return [use("propose_plan", {"summary": summary, "items": items,
                                 "left_out": list(left_out)}, n)]  # fmt: skip


def run(script, pe=REACH):
    cfg, tools = evals.plan_case(pe, evals.settings())
    brain = Llm(tools.app, 1, cfg.tz, cfg.tiers, cfg.budget, script, clock=lambda: evals.SUNDAY)
    return planner_agent.plan(tools, llm=brain, run_id="plan:test"), tools, cfg


WHY = {"c2": "The 3D printer's Pi idles when it is not printing.",
       "c5": "It idles from 06:00 and nobody uses it before 09:30.",
       "c7": "It stays on until 01:00 but nobody uses it after 00:30."}  # fmt: skip


def all_items():
    return [{"id": k, "why": v} for k, v in WHY.items()]


# code's choice, the model's words


def test_code_chooses_and_the_model_explains_every_chosen_change():
    script = Script(propose(all_items(), "One change of yours closes the month's gap."))
    p, _, _ = run(script)
    assert (p.done, p.chosen, p.shortfall, p.blocked) == (True, ("c2", "c5", "c7"), "", ())
    printer, start, off = p.items
    assert (printer.change, printer.action, printer.ready) == (None, planner_agent.ACTION, True)
    assert printer.saving.startswith("Saves about 3.55 kWh") and printer.why == WHY["c2"]
    assert (start.before, start.after) == ("On at 06:00, every day", "On at 09:30, every day")
    assert off.change == planner.Change("move_timer", slot=2, time="00:30", from_time="01:00")
    brief = script.seen[0]["messages"][0]["content"]
    assert script.seen[0]["tier"] == "claude-sonnet-5-5" and "Chosen changes:" in brief


def test_on_track_with_nothing_writable_chooses_nothing():
    p, _, _ = run(Script(propose([], "On track this month.")), ON_TRACK)
    assert (p.chosen, p.items, p.summary) == ((), (), "On track this month.")


def test_out_of_reach_says_the_shortfall():
    p, _, _ = run(Script(propose([])), SHORT)
    assert p.shortfall.startswith("These changes leave about 6.25 kWh")
    assert p.shortfall.endswith("2.65 kgCO2e over the goal by 30 Nov.")


def test_the_gap_left_this_month_becomes_a_months_rate(monkeypatch):
    seen = []
    monkeypatch.setattr(planner, "choose", lambda c, gap_wh: seen.append(gap_wh) or [])  # noqa: ARG005
    _, tools = evals.plan_case(SHORT, evals.settings())
    choice = REAL_DECIDE(planner_agent.PlanTools(
        tools, planner_agent.week_start(evals.SUNDAY, tz=evals.TZ)))  # fmt: skip
    share = 15.896 / 30  # 15 Nov 02:30: about 16 of 30 days left
    assert abs(seen[0] * share - 9120) < 15  # 3.87 kgCO2e over is about 9.12 kWh
    assert "over the goal by 30 Nov." in choice.shortfall


def test_a_zero_carbon_factor_means_no_gap():
    _, tools = evals.plan_case(SHORT, evals.settings())
    cfg = tools.cfg
    assert cfg.factor is not None
    zero = replace(tools, cfg=replace(cfg, factor=replace(cfg.factor, value=0.0)))
    t = planner_agent.PlanTools(zero, planner_agent.week_start(zero.now, tz=evals.TZ))
    assert REAL_DECIDE(t).shortfall == ""


def test_a_chosen_change_the_model_skipped_is_kept_in_codes_words():
    p, _, _ = run(Script(propose([{"id": "c5", "why": WHY["c5"]}])))
    assert [i.why for i in p.items] == [
        "3D printer idles when you are not using it.", WHY["c5"],
        "PeoTV box: Off at 01:00, every day to Off at 00:30, every day."]  # fmt: skip


def test_the_model_may_leave_a_change_out_with_a_reason():
    left = [{"id": "c2", "reason": "You said the Pi must stay on for remote prints."}]
    p, _, _ = run(Script(propose(all_items()[1:], left_out=left)))
    assert [i.plug for i in p.items] == [evals.BOX[0], evals.BOX[0]]
    assert p.blocked == (
        "c2 (3D printer): left out: You said the Pi must stay on for remote prints.",)  # fmt: skip
    assert p.shortfall.startswith("These changes leave about")  # without the printer, short


def shortfall(chosen, rate, share):
    cfg = evals.settings()
    return planner_agent.shortfall_text(cfg, evals.SUNDAY, chosen, rate=rate, share=share,
                                        last="30 Nov")  # fmt: skip


def test_the_shortfall_counts_only_the_time_left():
    printer = planner.Candidate("c2", "Plug-2", "m", "3D printer", None, "a", 3551, 2, "s", True,
                                "")  # fmt: skip
    half_hour = 0.5 / 24 / 30  # 30 Nov 23:30: half an hour of the month left
    left = shortfall([printer], round(100 / half_hour), half_hour)
    assert left == ("These changes leave about 0.10 kWh, LKR 5.0, 0.04 kgCO2e over the goal"
                    " by 30 Nov.")  # fmt: skip
    assert shortfall([], 10, 1.0) == ""  # 10 Wh: under 0.005 kgCO2e, not shown
    assert shortfall([], 1000, 0.5).startswith("Nothing here closes the gap: about 0.50 kWh")


def test_a_change_with_no_words_of_the_models_is_marked_plain():
    p, _, _ = run(Script(propose([])))
    assert p.plain == ("c2", "c5", "c7") and not evals._kept_all(p)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("items", "left", "reason"),
    [([{"id": "c5", "why": WHY["c5"]}] * 2, [], "item 2 (c5): listed twice"),
     ([{"id": "c5", "why": WHY["c5"]}], [{"id": "c5", "reason": "No."}], "in items too"),
     ([], [{"id": "c9", "reason": "No."}], "left out 1: only the ids in the brief"),
     ([], [{"id": "c2", "reason": "The Planner said so."}], "left out 1 (c2): reason: 'Planner'"),
     ([], [{"id": "c2", "reason": ""}], "left out 1 (c2): reason: one sentence"),
     ([], "[no", "left_out: not a list")],
)  # fmt: skip
def test_repeats_and_bad_reasons_go_back_and_the_change_stays(items, left, reason):
    def step(n):
        return [use("propose_plan", {"summary": "A week.", "items": items, "left_out": left}, n)]

    p, _, _ = run(Script(step(9), step(10)))
    assert any(reason in b for b in p.blocked), p.blocked
    assert "c5" in {i for i in p.chosen} and len(p.items) >= 2


def test_the_model_cannot_add_a_change():
    added = [*all_items(), {"id": "c9", "why": "A new pair."}]
    script = Script(propose(added), propose(added, n=10))
    p, _, _ = run(script)
    back = script.seen[1]["messages"][-1]["content"][0]["content"]
    assert "item 4: only the ids in the brief" in back and len(p.items) == 3


@pytest.mark.parametrize(
    ("why", "reason"),
    [("It wastes 93 W.", "why: not in the brief or a tool: 93"),
     ("Its access is write.", "'access' is a system word"),
     (None, "why: one sentence in plain words is needed")],
)  # fmt: skip
def test_bad_words_go_back_once_then_codes_words_stand_in(why, reason):
    items = [{"id": "c5", "why": why}]
    p, _, _ = run(Script(propose(items), propose(items, n=10)))
    assert any(reason in b for b in p.blocked) and len(p.items) == 3


@pytest.mark.parametrize(
    ("summary", "reason"),
    [("You used 42 kWh.", "not in the brief"), ("The Planner chose 3.", "system word")],
)
def test_a_bad_summary_gives_way_to_codes(summary, reason):
    p, _, _ = run(Script(propose(all_items(), summary), propose(all_items(), summary, n=10)))
    assert p.summary == "3 changes to look at this week." and reason in p.blocked[0]


def test_lists_sent_as_text_are_read_and_nonsense_is_refused():
    p, _, _ = run(Script(propose(json.dumps(all_items()))))
    assert p.blocked == () and [i.why for i in p.items] == list(WHY.values())
    p, _, _ = run(Script(propose("[no"), propose({"x": 1}, n=10)))
    assert "items: not a list" in p.blocked


def test_no_tool_call_gets_a_nudge_and_a_planner_that_never_proposes_is_not_done():
    words = [{"type": "text", "text": "Let me think."}]
    script = Script(words, *[[use("week_notes", {}, n)] for n in range(5)])
    p, _, _ = run(script)
    assert script.seen[1]["messages"][-1]["content"] == "Call propose_plan."
    assert script.seen[-1]["tools"] == ["propose_plan"] and not p.done
    assert p.chosen == ("c2", "c5", "c7")


def test_a_block_on_the_last_step_keeps_what_passed():
    steps = [[use("week_notes", {}, n)] for n in range(planner_agent.MAX_STEPS - 1)]
    p, _, _ = run(Script(*steps, propose([{"id": "c5", "why": "It wastes 93 W."}])))
    assert p.done and len(p.items) == 3


def test_a_bad_tool_call_comes_back_as_an_error():
    script = Script([use("usual", {"device": "PeoTV box"}), use("nope", {}, 2),
                     use("week_notes", {"week": 1}, 3)], propose([]))  # fmt: skip
    run(script)
    errors = [r["content"] for r in script.seen[1]["messages"][-1]["content"]]
    assert errors[0].startswith("Error:") and errors[1] == "Error: no tool named 'nope'"
    assert errors[2].startswith("Error:") and "week" in errors[2]


def test_the_plan_is_saved_once_a_week_with_its_actions():
    p, tools, cfg = run(Script(propose(all_items())))
    assert planner_agent.save(tools.app, p, site_id=1, cfg=cfg, now=evals.SUNDAY) == 1
    assert planner_agent.save(tools.app, p, site_id=1, cfg=cfg, now=evals.SUNDAY) is None
    first = store.plan_items(tools.app, plan_id=1)[0]
    assert json.loads(first.change) == {"kind": "action", "do": planner_agent.ACTION}
    later = evals.SUNDAY + timedelta(days=7)
    assert planner_agent.save(tools.app, replace(p, done=False), site_id=1, cfg=cfg,
                              now=later) is None  # fmt: skip


# candidates


def test_candidates_cover_moves_pairs_and_actions():
    _, tools = evals.plan_case(REACH, evals.settings())
    t = planner_agent.PlanTools(tools, planner_agent.week_start(evals.SUNDAY, tz=evals.TZ))
    got = {(c.device, c.change.kind if c.change else "action", c.ready, c.cost)
           for c in t.candidates()}  # fmt: skip
    assert ("PeoTV box", "move_timer", True, 0) in got and ("LG TV", "move_timer", False, 1) in got
    assert ("3D printer", "action", True, 2) in got and ("Desk 2", "action", True, 2) in got
    assert all(kind != "add_pair" for _, kind, _, _ in got)  # it has timers: moves, no pair


def test_a_write_device_with_no_timer_change_gets_the_wall_switch(monkeypatch):
    _, tools = evals.plan_case(REACH, evals.settings())
    store.plug_seen(
        tools.app,
        site_id=1,
        mac=evals.TV[1],
        name=evals.TV[0],
        ip="192.0.2.13",
        timers_on=False,
        now=evals.SUNDAY,
    )  # its timers do not fire: no change
    monkeypatch.setattr(planner, "idle_wh", lambda *a, **k: (5000, 14))  # noqa: ARG005
    t = planner_agent.PlanTools(tools, planner_agent.week_start(evals.SUNDAY, tz=evals.TZ))
    tv = [c.change for c in t.candidates() if c.device == "LG TV"]
    assert tv == [None]  # an action, for the owner


def test_a_must_run_device_gets_no_candidate():
    _, tools = evals.plan_case(REACH, evals.settings())
    store.save_card(tools.app, site_id=1, mac=evals.PRINTER[1], card=Card("router"),
                    now=evals.SUNDAY)  # fmt: skip
    store.save_timers(tools.app, site_id=1, mac=evals.BOX[1], timers=[], now=evals.SUNDAY)
    t = planner_agent.PlanTools(tools, planner_agent.week_start(evals.SUNDAY, tz=evals.TZ))
    got = [(c.device, c.change.kind if c.change else "") for c in t.candidates()]
    assert "3D printer" not in [d for d, _ in got]
    assert [k for d, k in got if d == "PeoTV box"] == ["add_pair"]  # no timers: a pair


def test_an_action_said_not_now_to_or_seen_too_short_is_not_offered(monkeypatch):
    _, tools = evals.plan_case(REACH, evals.settings())
    t = planner_agent.PlanTools(tools, planner_agent.week_start(evals.SUNDAY, tz=evals.TZ))
    mac = evals.PRINTER[1]
    store.add_plan(tools.app, site_id=1, week="2026-11-02", summary="s", now=evals.SUNDAY,
                   items=[(mac, planner_agent.action_json(planner_agent.ACTION), "w", "s",
                           True, "", "")])  # fmt: skip
    store.decide_item(tools.app, item_id=1, status="not_now", now=evals.SUNDAY)
    assert t._action(evals.PRINTER[0], mac) is None  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(planner, "idle_wh", lambda *a, **k: (5000, 6))  # noqa: ARG005
    assert t._action(evals.DESK[0], evals.DESK[1]) is None  # pyright: ignore[reportPrivateUsage]


def test_no_pair_where_every_night_off_cut_use(monkeypatch):
    _, tools = evals.plan_case(REACH, evals.settings())
    store.save_timers(tools.app, site_id=1, mac=evals.BOX[1], timers=[], now=evals.SUNDAY)
    t = planner_agent.PlanTools(tools, planner_agent.week_start(evals.SUNDAY, tz=evals.TZ))
    monkeypatch.setattr(planner, "ready", lambda r: False)
    assert t._pair(evals.BOX[0], evals.BOX[1]) == []  # pyright: ignore[reportPrivateUsage]


# notes and the week


def test_week_notes_hold_notes_answers_and_not_nows():
    _, tools = evals.plan_case(REACH, evals.settings())
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
    store.add_plan(app, site_id=1, week="2026-11-02", summary="s", now=week,
                   items=[(mac, planner.Change("remove_timer", slot=2).to_json(), "w", "s",
                           True, "", "")])  # fmt: skip
    store.decide_item(app, item_id=1, status="not_now", now=week)
    text = planner_agent.PlanTools(tools, week).week_notes()
    assert text == ("Note: The box idled all morning.\n"
                    "Asked: The box is on. Off? Answer: Off\n"
                    'Not now: PeoTV box: {"kind": "remove_timer", "slot": 2}')  # fmt: skip


def test_an_empty_week_says_so_and_a_timers_switch_off_shows():
    _, tools = evals.plan_case(REACH, evals.settings())
    store.plug_seen(tools.app, site_id=1, mac=evals.TV[1], name=evals.TV[0], ip="192.0.2.13",
                    timers_on=False, now=evals.SUNDAY)  # fmt: skip
    t = planner_agent.PlanTools(tools, planner_agent.week_start(evals.SUNDAY, tz=evals.TZ))
    assert t.week_notes() == "No notes, answers or Not nows this week."
    assert "every day (Timers switch off)" in t.house_week()


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
    stuck = Script(*[[use("week_notes", {}, n)] for n in range(planner_agent.MAX_STEPS)])
    assert cli.main(["--config", str(home), "plan"], step=stuck) == 1
    assert "The Planner did not finish a plan." in capsys.readouterr().out


def test_agentc_plan_prints_timer_changes_actions_and_what_was_left_out(home, capsys,
                                                                        monkeypatch):  # fmt: skip
    move = planner_agent.Item("Plug-1", "AA", planner.Change("move_timer", slot=1, time="07:00"),
                              "It idles.", "Saves about 0.02 kWh a month.",
                              "On at 06:00, every day", "On at 07:00, every day",
                              False)  # fmt: skip
    act = planner_agent.Item("Plug-2", "BB", None, "It idles.", "Saves about 3 kWh.", "",
                             planner_agent.ACTION, True, planner_agent.ACTION)  # fmt: skip
    done = planner_agent.Plan("Two changes.", (move, act), ("usual",), ("c3 (TV): left out: x",))
    monkeypatch.setattr(planner_agent, "plan", lambda tools, llm, run_id: done)
    assert cli.main(["--config", str(home), "plan"], step=Script()) == 0
    out = capsys.readouterr().out
    assert "1. Desk 2 (only to talk over): It idles.\n   Now: On at 06:00, every day" in out
    assert f"2. 3D printer (ready): It idles.\n   You: {planner_agent.ACTION}" in out
    assert "Left out: c3 (TV): left out: x" in out and "Tools: usual." in out


# the weekly job: Sunday 18:00, or on goal drift; one message with a link (5b5)

EVENING = evals.SUNDAY.replace(hour=18, minute=30)  # Sunday 15 Nov, after 18:00


class Phone:
    def __init__(self, fail=False):
        self.got, self.fail = [], fail

    def __call__(self, m):
        if self.fail:
            raise notify.NotSent("ntfy is down")
        self.got.append(m)


def plan_pass(script, pe=REACH, sender=None, now=EVENING):
    cfg, tools = evals.plan_case(pe, evals.settings())
    brain = Llm(tools.app, 1, cfg.tz, cfg.tiers, cfg.budget, script, clock=lambda: now)
    m = jobs.plan_pass(tools.app, tools.energy, cfg, brain, site_id=1, now=now, sender=sender)
    return m, tools


def test_the_weekly_plan_is_saved_and_sent_with_a_link():
    phone = Phone()
    m, tools = plan_pass(
        Script(propose(all_items())), sender=jobs.Sender(phone, "https://hub", "k")
    )
    assert m is not None and phone.got == [m] and m.quiet
    assert (m.title, m.text) == ("This week's plan: 3 changes", "A quiet week.")
    assert m.buttons == (notify.Button("Open the plan", "https://hub/plan", post=False),)
    assert store.has_plan(tools.app, site_id=1, week="2026-11-16")
    assert not jobs._plan_due(tools.app, 1, EVENING, evals.TZ)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("phone", [None, Phone(fail=True)])
def test_no_message_without_a_phone_or_when_it_fails_but_the_plan_is_kept(phone):
    sender = jobs.Sender(phone, "https://hub", "k") if phone else None
    m, tools = plan_pass(Script(propose(all_items())), sender=sender)
    assert m is None and store.has_plan(tools.app, site_id=1, week="2026-11-16")


def test_an_empty_plan_is_kept_and_sends_nothing():
    phone = Phone()
    m, tools = plan_pass(Script(propose([], "On track.")), ON_TRACK,
                         sender=jobs.Sender(phone, "https://hub", "k"))  # fmt: skip
    assert m is None and phone.got == [] and store.has_plan(tools.app, site_id=1, week="2026-11-16")


@pytest.mark.parametrize("err", [llm.OverBudget("cap"), llm.Unreachable("down")])
def test_a_run_that_cannot_reach_the_model_waits_and_keeps_nothing(err):
    def broken(**_):
        raise err

    m, tools = plan_pass(broken)
    assert m is None and not store.has_plan(tools.app, site_id=1, week="2026-11-16")
    assert store.last_run(tools.app, site_id=1, job="plan") == EVENING


def test_the_plan_is_due_on_sunday_evening_or_on_goal_drift_and_retries_after_6_hours():
    _, tools = evals.plan_case(REACH, evals.settings())
    app = tools.app

    def due(at):
        return jobs._plan_due(app, 1, at, evals.TZ)  # pyright: ignore[reportPrivateUsage]

    assert not due(evals.SUNDAY) and due(EVENING)  # 02:30, then 18:30
    wednesday = evals.SUNDAY + timedelta(days=3)  # a new week, no plan yet
    assert not due(wednesday)
    e = Event("goal_drift", None, wednesday.replace(hour=0), {"projected_kg": 6.2, "goal_kg": 5.5})
    store.add_event(app, site_id=1, event=e, dedupe="drift")
    assert due(wednesday)
    store.set_last_run(app, site_id=1, job="plan", at=wednesday)  # a run that did not finish
    assert not due(wednesday + timedelta(hours=5)) and due(wednesday + timedelta(hours=6))


def test_one_plan_a_week_and_at_most_3_runs_for_it():
    _, tools = evals.plan_case(REACH, evals.settings())
    app = tools.app

    def due(at):
        return jobs._plan_due(app, 1, at, evals.TZ)  # pyright: ignore[reportPrivateUsage]

    assert planner_agent.plan_week(EVENING, tz=evals.TZ) == "2026-11-16"  # the coming week
    store.add_plan(app, site_id=1, week="2026-11-16", summary="s", items=[], now=EVENING)
    monday = EVENING + timedelta(hours=12)
    e = Event("goal_drift", None, monday.replace(hour=0), {"projected_kg": 6.2, "goal_kg": 5.5})
    store.add_event(app, site_id=1, event=e, dedupe="drift")
    assert not due(monday)  # drift on Monday: this week has its plan from Sunday
    later = EVENING + timedelta(days=7)  # the next Sunday: a run that never finishes
    for n in range(3):
        assert due(later + n * jobs.PLAN_RETRY)
        store.set_setting(app, site_id=1, key="plan_tries:2026-11-23", value=str(n + 1))
        store.set_last_run(app, site_id=1, job="plan", at=later + n * jobs.PLAN_RETRY)
    assert due(later + 2 * jobs.PLAN_RETRY + timedelta(hours=7)) is False  # 3 runs: it stops


def test_the_jobs_run_the_planner_only_with_a_model(monkeypatch):
    cfg, tools = evals.plan_case(REACH, evals.settings())
    ran = []
    monkeypatch.setattr(jobs, "watch_pass", lambda *a, **k: [])  # noqa: ARG005
    monkeypatch.setattr(jobs, "look_pass", lambda *a, **k: [])  # noqa: ARG005
    monkeypatch.setattr(jobs, "plan_pass", lambda *a, **k: ran.append(k["now"]) or "sent")  # noqa: ARG005
    jobs.run_due(tools.app, tools.energy, cfg, site_id=1, now=EVENING)
    assert ran == []  # no key, no model: the plan waits
    _, _, sent = jobs.run_due(tools.app, tools.energy, cfg, site_id=1, now=EVENING, llm="brain")  # pyright: ignore[reportArgumentType]
    assert ran == [EVENING] and sent == ["sent"]
