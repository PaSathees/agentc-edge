"""The housemate in the jobs (3c2): code gates first, then a look, then the owner's limits."""

from dataclasses import replace
from datetime import UTC, timedelta

import pytest

from agentc import jobs, store
from agentc.config import Budget
from agentc.house import Habit
from agentc.llm import Llm, Unreachable
from agentc.owner import Choice, Decision
from agentc.policy import Policy
from agentc.watcher import Event
from tests.fake_tasmota import FakePlug
from tests.test_jobs import CFG, MONDAY, NOW, app, energy  # noqa: F401 (app is a fixture)


class Decides:
    """A stand-in model that decides at once. Raises `error` on the calls in `on` (from 1)."""

    def __init__(self, action="ask", error=None, on=None):
        self.action, self.error, self.on, self.calls = action, error, on, 0

    def __call__(self, *, tier, system, messages, tools, on_text=None):
        self.calls += 1
        if self.error and (self.on is None or self.calls in self.on):
            raise self.error
        pairs = (("Off", "switch_off_now"), ("Normal", "save_habit"), ("Later", "remind_later"),
                 ("Leave it", "nothing")) if self.action in ("ask", "propose") else ()  # fmt: skip
        choices = [{"label": a, "does": b, "off": None, "on": None, "days": None}
                   for a, b in pairs[:3]]  # fmt: skip
        text = "Still on. Switch it off?" if choices else "Normal for this device."
        args = {"action": self.action, "text": text, "choices": choices}
        call = {"type": "tool_use", "id": "t1", "name": "decide", "input": args}
        return [call], "tool_use", 900, 90


LEFT_ON = {"minutes": 40, "peak_w": 5.0}


def signal(app, kind="left_on", mac="AA", at=MONDAY + timedelta(hours=1), facts=None, key=None):  # noqa: F811
    e = Event(kind, mac, at, LEFT_ON if facts is None else facts)
    store.add_event(app, site_id=1, event=e, dedupe=key or f"{kind}|{mac}|{at}")


def look(app, db, model, *, now=NOW, cfg=CFG, month_usd=3.0):  # noqa: F811
    llm = Llm(app, 1, cfg.tz, cfg.tiers, Budget(month_usd, 0.05), model, clock=lambda: now)
    return jobs.look_pass(app, db, cfg, llm, site_id=1, now=now)


def statuses(app):  # noqa: F811
    return [s for (s,) in app.execute("SELECT status FROM events ORDER BY id")]


def actions(app):  # noqa: F811
    return [a for (a,) in app.execute("SELECT action FROM audit ORDER BY id")]


def act(out):
    return out.decision.action if out.decision else None


def small(tmp_path):
    return energy(tmp_path, lambda t: 5.0)  # Plug-6 at 5 W all night


# a look, and where its question goes


def test_a_small_load_at_night_is_asked_about_at_07_00(app, tmp_path):  # noqa: F811 (FR-36)
    signal(app)
    (out,) = look(app, small(tmp_path), Decides())
    assert (out.device, act(out), out.question_id) == ("Plug-6", "ask", 1)
    assert out.why == "reaches you at Mon 07:00"
    (q,) = store.questions(app, site_id=1)
    assert q.send_at == (MONDAY + timedelta(hours=7)).astimezone(UTC)
    assert (statuses(app), actions(app)) == (["done"], ["ask"])


def test_a_big_load_at_night_is_asked_about_at_once(app, tmp_path):  # noqa: F811 (FR-36)
    signal(app, facts={"minutes": 40, "peak_w": 1500.0})
    (out,) = look(app, energy(tmp_path, lambda t: 1500.0), Decides())
    assert out.why == "reaches you now"


def test_a_device_can_be_set_to_wake_the_owner_for_less(app, tmp_path):  # noqa: F811
    signal(app)
    cfg = replace(CFG, policy=Policy(night_wake_w=1))
    assert look(app, small(tmp_path), Decides(), cfg=cfg)[0].why == "reaches you now"


def test_by_day_a_question_reaches_the_owner_at_once(app, tmp_path):  # noqa: F811
    signal(app, at=MONDAY + timedelta(hours=9))
    now = MONDAY + timedelta(hours=10)
    assert look(app, small(tmp_path), Decides(), now=now)[0].why == "reaches you now"


@pytest.mark.parametrize("action", ["ignore", "note"])
def test_ignore_and_note_are_logged_with_no_question(app, tmp_path, action):  # noqa: F811
    signal(app)
    (out,) = look(app, small(tmp_path), Decides(action))
    assert (act(out), out.question_id) == (action, None)
    assert (store.questions(app, site_id=1), actions(app)) == ([], [action])


def test_no_decision_is_logged_as_dropped(app, tmp_path):  # noqa: F811
    def mute(**_):
        return [{"type": "text", "text": "Hmm."}], "end_turn", 900, 90

    signal(app)
    (out,) = look(app, small(tmp_path), mute)
    assert (out.decision, out.why) == (None, "no decision: format: no decision")
    assert (statuses(app), actions(app)) == (["done"], ["dropped"])


def test_health_signals_are_left_for_the_owner_as_notices(app, tmp_path):  # noqa: F811
    model = Decides()
    signal(app, kind="offline", facts={})
    assert look(app, small(tmp_path), model) == [] and model.calls == 0
    assert statuses(app) == ["new"]


# code skips what needs no look


def test_a_habit_that_covers_the_signal_stops_the_look(app, tmp_path):  # noqa: F811 (FR-33)
    habit = Habit("Plug-6 is normal on Mondays", "0100000", "00:00", "07:00", "tap")
    store.add_habit(app, site_id=1, mac="AA", habit=habit, now=NOW)
    model = Decides()
    signal(app)
    (out,) = look(app, small(tmp_path), model)
    assert (out.looked, out.why, model.calls) == (False, "habit: Plug-6 is normal on Mondays", 0)
    assert (statuses(app), actions(app)) == (["skipped"], ["skipped"])


DRIFT = {"slot": 1, "time": "00:30", "peak_w": 1.0, "hours": 3}


def drift(app, day, slot=1):  # noqa: F811
    at = MONDAY + timedelta(days=day, minutes=30)
    signal(app, kind="automation_drift", at=at, facts={**DRIFT, "slot": slot})


def test_a_drifting_timer_is_looked_at_once_it_is_a_pattern(app, tmp_path):  # noqa: F811
    seen = []

    def model(**kw):
        seen.append(kw["messages"][0]["content"])
        return Decides("propose")(**kw)

    db = small(tmp_path)
    for day in (-2, -1):
        drift(app, day)
        (out,) = look(app, db, model)
        assert out.why.startswith("waiting: timer 1 drifted on ") and not out.looked
    drift(app, 0)
    (out,) = look(app, db, model)
    assert out.looked and "The same timer did this on 3 of the last 7 days" in seen[0]


def test_a_drifting_timer_is_looked_at_once_a_week(app, tmp_path):  # noqa: F811
    db = small(tmp_path)
    for day in (-3, -2, -1):
        drift(app, day)
        look(app, db, Decides("propose"))
    drift(app, 0)
    (out,) = look(app, db, Decides("propose"))
    assert out.why == "grouped: timer 1 was looked at in the last 7 days"


def test_another_timer_on_the_same_plug_counts_on_its_own(app, tmp_path):  # noqa: F811
    db = small(tmp_path)
    for day in (-2, -1):
        drift(app, day)
    drift(app, 0, slot=2)
    outs = look(app, db, Decides("propose"))
    assert outs[-1].why == "waiting: timer 2 drifted on 1 of the last 7 days"


# the owner's limits: one question a device a day, 5 a day


def test_a_second_question_about_a_device_that_day_becomes_a_note(app, tmp_path):  # noqa: F811
    signal(app)
    signal(app, at=MONDAY + timedelta(hours=2))
    first, second = look(app, small(tmp_path), Decides())
    assert first.question_id == 1
    assert (act(second), second.question_id) == ("note", None)
    assert second.why == "held: Plug-6 has a question that day already"


def test_after_5_questions_a_day_the_rest_become_notes(app, tmp_path):  # noqa: F811
    db = small(tmp_path)
    for n in range(5):  # questions already due that morning, about other plugs
        mac = f"C{n}"
        store.plug_seen(app, site_id=1, mac=mac, name=f"Plug-{10 + n}", ip="192.0.2.9",
                        timers_on=True, now=NOW)  # fmt: skip
        signal(app, mac=mac)
    signal(app)
    cfg = replace(CFG, plugs={**CFG.plugs, **{f"Plug-{10 + n}": "192.0.2.9" for n in range(5)}})
    outs = look(app, db, Decides(), cfg=cfg)
    assert [o.question_id is not None for o in outs] == [True] * 5 + [False]
    assert outs[-1].why == "held: 5 questions that day already"


@pytest.mark.parametrize(
    ("later", "status"),
    [(timedelta(hours=18, minutes=59), "open"), (timedelta(hours=19, minutes=1), "expired")],
)
def test_an_unanswered_question_expires_12_hours_after_it_is_due(app, tmp_path, later, status):  # noqa: F811
    signal(app)
    db = small(tmp_path)
    look(app, db, Decides())  # due at 07:00
    jobs.run_due(app, db, replace(CFG, plugs={}), site_id=1, now=MONDAY + later)
    assert [q.status for q in store.questions(app, site_id=1)] == [status]


# when the model fails: other events and other jobs go on (FR-37)


def tries(app):  # noqa: F811
    return [t for (t,) in app.execute("SELECT tries FROM events ORDER BY id")]


def test_an_unreachable_model_leaves_every_signal_waiting(app, tmp_path):  # noqa: F811
    model = Decides(error=Unreachable("no internet"))
    signal(app)
    signal(app, at=MONDAY + timedelta(hours=2))
    assert look(app, small(tmp_path), model) == []
    assert (statuses(app), tries(app), model.calls) == (["new", "new"], [0, 0], 1)


def test_at_the_budget_cap_signals_wait(app, tmp_path):  # noqa: F811
    model = Decides()
    signal(app)
    assert look(app, small(tmp_path), model, month_usd=0.0) == []
    assert (statuses(app), model.calls) == (["new"], 0)


def test_one_bad_signal_does_not_block_the_next(app, tmp_path):  # noqa: F811
    signal(app)
    signal(app, at=MONDAY + timedelta(hours=2))
    outs = look(app, small(tmp_path), Decides("note", error=ValueError("bad"), on={1}))
    assert (len(outs), statuses(app), tries(app)) == (1, ["new", "done"], [1, 0])


def test_after_3_failed_tries_a_signal_is_parked_and_left_alone(app, tmp_path):  # noqa: F811
    model, db = Decides(error=ValueError("bad")), small(tmp_path)
    signal(app)
    for _ in range(4):
        look(app, db, model)
    assert (statuses(app), tries(app), model.calls) == (["parked"], [3], 3)
    assert actions(app) == ["look_failed"] * 3 + ["event_parked"]


def test_other_jobs_still_run_when_the_look_step_breaks(app, tmp_path, monkeypatch):  # noqa: F811
    def broken(*args, **kwargs):
        raise RuntimeError("a bug")

    monkeypatch.setattr(jobs, "look_pass", broken)
    plug = FakePlug(mac="AA")
    plug.start()
    try:
        cfg = replace(CFG, plugs={"Plug-6": plug.ip})
        llm = Llm(app, 1, CFG.tz, CFG.tiers, CFG.budget, Decides(), clock=lambda: NOW)
        jobs.run_due(app, small(tmp_path), cfg, site_id=1, now=NOW, llm=llm)
    finally:
        plug.stop()
    ran = {job: store.last_run(app, site_id=1, job=job) for job in ("watch", "timers", "learn")}
    assert ran == {"watch": NOW, "timers": NOW, "learn": NOW}
    assert "look_pass_failed" in actions(app)


def test_the_watch_pass_lets_the_housemate_look(app, tmp_path):  # noqa: F811
    signal(app)
    llm = Llm(app, 1, CFG.tz, CFG.tiers, CFG.budget, Decides(), clock=lambda: NOW)
    quiet = energy(tmp_path, lambda t: 1.0)  # so the watch pass finds no signal of its own
    _, looked, _ = jobs.run_due(app, quiet, replace(CFG, plugs={}), site_id=1, now=NOW, llm=llm)
    assert [o.question_id for o in looked] == [1]


# the owner's tap


def asked(app, tmp_path, now=NOW):  # noqa: F811
    signal(app)
    look(app, small(tmp_path), Decides(), now=now)


def tap(app, choice, question=1, now=NOW):  # noqa: F811
    return jobs.answer(app, CFG, site_id=1, question_id=question, choice=choice, now=now)


def test_that_is_normal_saves_a_habit_and_says_its_scope(app, tmp_path):  # noqa: F811
    asked(app, tmp_path)
    assert tap(app, 2) == "Saved: Plug-6 is normal on Mondays, 01:00 to 07:00."
    ((_, h),) = store.habits(app, mac="AA")
    assert (h.days, h.start, h.end, h.source) == ("0100000", "01:00", "07:00", "tap")
    (q,) = store.questions(app, site_id=1)
    assert (q.status, q.answer) == ("answered", 2)


def test_a_switch_off_is_recorded_but_not_sent_in_shadow_mode(app, tmp_path):  # noqa: F811
    asked(app, tmp_path)
    assert tap(app, 1).startswith("Recorded: switch Plug-6 off. Plug-6 is set to advice")
    assert actions(app)[-2:] == ["answer", "shadow_action"]


def test_a_timer_choice_is_only_recorded_until_4c(app, tmp_path):  # noqa: F811
    signal(app)
    (eid,) = [e for (e,) in app.execute("SELECT id FROM events")]
    timer = Choice("Off at 01:30", "set_timer", "01:30", "07:00", "weekdays")
    store.add_question(app, site_id=1, event_id=eid, mac="AA", now=NOW, send_at=NOW,
                       decision=Decision("propose", "Timer?", (timer,)))  # fmt: skip
    assert tap(app, 1) == "Recorded: Off at 01:30. Shadow mode: nothing was sent to the plug."
    assert actions(app)[-1] == "shadow_action"


def test_remind_later_asks_again_in_2_hours(app, tmp_path):  # noqa: F811
    asked(app, tmp_path)
    assert tap(app, 3) == "I'll ask again at 06:00."
    (q,) = store.questions(app, site_id=1)
    assert (q.status, q.send_at) == ("open", (NOW + timedelta(hours=2)).astimezone(UTC))


def test_nothing_is_just_noted(app, tmp_path):  # noqa: F811
    signal(app)
    model = Decides()
    model.action = "propose"
    look(app, small(tmp_path), model)
    store.ask_again(app, question_id=1, at=NOW)  # keep it simple: open now
    # the stand-in offers Off, Normal, Later; "nothing" needs its own question
    app.execute("UPDATE questions SET choices = ? WHERE id = 1",
                ('[{"label": "Leave it", "does": "nothing", "off": null, "on": null,'
                 ' "days": null}]',))  # fmt: skip
    assert tap(app, 1) == "Noted."


def test_an_answer_needs_an_open_question_and_a_real_choice(app, tmp_path):  # noqa: F811
    asked(app, tmp_path)
    with pytest.raises(LookupError, match="choices 1 to 3"):
        tap(app, 4)
    tap(app, 1)
    with pytest.raises(LookupError, match="No open question 1"):
        tap(app, 1)


def test_a_restart_after_a_question_is_saved_never_asks_again(app, tmp_path, monkeypatch):  # noqa: F811
    signal(app)
    model, db = Decides(), small(tmp_path)

    def crash(*args, **kwargs):  # the container stops before the event is marked done
        raise SystemExit("stopped")

    monkeypatch.setattr(store, "event_done", crash)
    with pytest.raises(SystemExit):
        look(app, db, model)
    monkeypatch.undo()
    assert look(app, db, model) == [] and model.calls == 1  # not looked at again
    assert len(store.questions(app, site_id=1)) == 1


def test_two_runs_at_once_ask_only_once(app, tmp_path):  # noqa: F811 (e.g. `watch` beside `run`)
    signal(app)
    db = small(tmp_path)

    def other_run_first(**kw):  # the other run marks the event done while this one looks
        app.execute("UPDATE events SET status = 'done' WHERE id = 1")
        app.commit()
        return Decides()(**kw)

    (out,) = look(app, db, other_run_first)
    assert (out.question_id, out.why) == (None, "handled by another run")
    assert store.questions(app, site_id=1) == []


def test_two_taps_at_once_answer_only_once(app, tmp_path, monkeypatch):  # noqa: F811
    asked(app, tmp_path)
    monkeypatch.setattr(store, "answer_question", lambda *a, **kw: False)  # the other tap won
    with pytest.raises(LookupError, match="No open question 1"):
        tap(app, 2)
    assert store.habits(app, mac="AA") == []
