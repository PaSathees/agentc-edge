import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agentc import config, jobs, learn, store
from agentc.calc import ProfileHour
from agentc.config import Budget
from agentc.house import Card
from agentc.llm import Llm, Unreachable
from agentc.policy import Policy
from agentc.watcher import Event
from tests.fake_model import FakeModel
from tests.fake_tasmota import FakePlug
from tests.test_cli import EXAMPLE
from tests.test_options import START, desk, night

COLOMBO = ZoneInfo("Asia/Colombo")
MONDAY = datetime(2026, 11, 2, tzinfo=COLOMBO)  # Monday 00:00 local
NOW = MONDAY + timedelta(hours=4)  # the pass runs at 04:00
CFG = config.load(EXAMPLE)  # Plug-6 is not listed: advice, site policy. Plug-2: access none


@pytest.fixture
def app(tmp_path):
    """app.db with two known plugs. Plug-6 has a profile: usually off at every hour."""
    db = store.connect(tmp_path / "app.db")
    store.add_site(db, name="home", tz="Asia/Colombo")
    for name, mac in (("Plug-6", "AA"), ("Plug-2", "BB")):
        store.plug_seen(db, site_id=1, mac=mac, name=name, ip="192.0.2.1", timers_on=True, now=NOW)
    store.save_profile(
        db, site_id=1, mac="AA", profile=[ProfileHour(h, 0.0, 0.0, 3) for h in range(168)]
    )
    return db


def energy(tmp_path, watts, *, failed=()):
    """energy.db from 00:00 to 04:00: Plug-2 at 1 W, Plug-6 at watts(local minute).

    failed: local times at which every plug's read failed.
    """
    rows = []
    for m in range(4 * 60):
        local = MONDAY + timedelta(minutes=m)
        ts = local.astimezone(UTC).isoformat()
        for plug, w in (("Plug-6", watts(local)), ("Plug-2", 1.0)):
            bad = local in failed or (plug == "Plug-6" and w is None)
            rows.append((ts, plug, None if bad else w, "TimeoutError" if bad else None))
    path = tmp_path / "energy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE readings (ts_utc, plug, power_w, error)")
        db.executemany("INSERT INTO readings VALUES (?, ?, ?, ?)", rows)
    db.close()
    return store.open_readings(path)


def kinds(events):
    return [(e.kind, e.plug_mac) for e in events]


def test_a_charger_left_on_is_saved_as_one_event(app, tmp_path):
    db = energy(tmp_path, lambda t: 30.0 if 1 <= t.hour < 3 else 1.0)
    assert kinds(jobs.watch_pass(app, db, CFG, site_id=1, now=NOW)) == [("unusual_use", "AA")]
    assert len(store.load_events(app, site_id=1)) == 1


def test_the_next_pass_finds_it_again_but_saves_nothing(app, tmp_path):
    db = energy(tmp_path, lambda t: 30.0 if 1 <= t.hour < 3 else 1.0)
    jobs.watch_pass(app, db, CFG, site_id=1, now=NOW)
    assert jobs.watch_pass(app, db, CFG, site_id=1, now=NOW) == []


def test_a_normal_night_saves_nothing(app, tmp_path):
    db = energy(tmp_path, lambda t: 1.0)
    assert jobs.watch_pass(app, db, CFG, site_id=1, now=NOW) == []


def test_a_plug_that_stopped_answering_is_offline(app, tmp_path):
    db = energy(tmp_path, lambda t: None if t.hour == 3 and t.minute >= 50 else 1.0)
    assert kinds(jobs.watch_pass(app, db, CFG, site_id=1, now=NOW)) == [("offline", "AA")]


def test_a_blackout_between_passes_is_found(app, tmp_path):
    blackout = {MONDAY + timedelta(hours=3, minutes=m) for m in (50, 51)}  # 03:50 to 03:51
    db = energy(tmp_path, lambda t: 1.0, failed=blackout)  # all back well before the pass
    assert kinds(jobs.watch_pass(app, db, CFG, site_id=1, now=NOW)) == [("no_contact", None)]


def test_lost_contact_with_every_plug_is_one_site_event(app, tmp_path):
    last = {MONDAY + timedelta(hours=3, minutes=m) for m in range(55, 60)}
    db = energy(tmp_path, lambda t: 1.0, failed=last)
    assert kinds(jobs.watch_pass(app, db, CFG, site_id=1, now=NOW)) == [("no_contact", None)]


# the daily timer check


@pytest.fixture
def plugs():
    """Fake plugs with the same MACs as the app's Plug-6 and Plug-2."""
    ps = {"Plug-6": FakePlug(mac="AA"), "Plug-2": FakePlug(mac="BB")}
    for p in ps.values():
        p.start()
    yield ps
    for p in ps.values():
        p.stop()


def check(app, plugs):
    ips = {name: p.ip for name, p in plugs.items()}
    return jobs.timer_check(app, replace(CFG, plugs=ips), site_id=1, now=NOW)


def test_the_first_timer_check_is_no_mismatch(app, plugs):
    assert check(app, plugs) == []


def test_a_timer_changed_on_the_plug_is_a_mismatch(app, plugs):
    check(app, plugs)
    plugs["Plug-6"].timers[1]["Time"] = "08:00"  # someone changed it in the plug's web page
    assert kinds(check(app, plugs)) == [("timer_mismatch", "AA")]


def test_a_plug_that_does_not_answer_is_no_mismatch(app, plugs):
    check(app, plugs)
    plugs["Plug-6"].offline = True  # the logger reports it offline instead
    assert check(app, plugs) == []


# the timetable: what runs when


@pytest.fixture
def quiet(tmp_path):
    """energy.db for a normal night: nothing to report."""
    return energy(tmp_path, lambda t: 1.0)


def due(app, db, plugs, now):
    """Run whatever is due at `now`; return when each job last ran."""
    ips = {name: p.ip for name, p in plugs.items()}
    jobs.run_due(app, db, replace(CFG, plugs=ips), site_id=1, now=now)
    return {job: store.last_run(app, site_id=1, job=job) for job in ("watch", "timers")}


def test_the_first_start_runs_every_job(app, quiet, plugs):
    assert due(app, quiet, plugs, NOW) == {"watch": NOW, "timers": NOW}


def test_a_job_runs_again_only_when_its_time_comes(app, quiet, plugs):
    due(app, quiet, plugs, NOW)
    later = NOW + timedelta(minutes=10)
    assert due(app, quiet, plugs, later) == {"watch": NOW, "timers": NOW}  # too soon
    later = NOW + timedelta(minutes=15)
    assert due(app, quiet, plugs, later) == {"watch": later, "timers": NOW}  # daily: not yet


def test_passes_missed_while_the_app_was_down_run_once(app, quiet, plugs):
    due(app, quiet, plugs, NOW)
    back = NOW + timedelta(hours=3)  # 12 passes missed
    assert due(app, quiet, plugs, back)["watch"] == back


# one alert per outage: a plug must answer again before a new one


def reported_at(app, kind, mac, local):
    """An event of this kind already saved by an earlier pass, starting at `local`."""
    store.add_event(app, site_id=1, event=Event(kind, mac, local, {}), dedupe=f"earlier {kind}")


def test_an_outage_that_goes_on_is_reported_once(app, tmp_path):
    reported_at(app, "offline", "AA", MONDAY + timedelta(hours=1))
    db = energy(tmp_path, lambda t: None if t.hour >= 1 else 1.0)  # down from 01:00
    assert jobs.watch_pass(app, db, CFG, site_id=1, now=NOW) == []


def test_a_plug_that_answered_in_between_is_a_new_outage(app, tmp_path):
    reported_at(app, "offline", "AA", MONDAY + timedelta(hours=1))
    db = energy(tmp_path, lambda t: None if t.hour == 1 or t.hour == 3 else 1.0)  # back at 02:00
    assert kinds(jobs.watch_pass(app, db, CFG, site_id=1, now=NOW)) == [("offline", "AA")]


def test_a_long_loss_of_contact_is_one_event(app, tmp_path):
    reported_at(app, "no_contact", None, MONDAY + timedelta(hours=1))
    every = {MONDAY + timedelta(minutes=m) for m in range(60, 4 * 60)}  # all down from 01:00
    db = energy(tmp_path, lambda t: 1.0, failed=every)
    assert jobs.watch_pass(app, db, CFG, site_id=1, now=NOW) == []


def test_a_must_run_device_raises_no_unusual_use(app, tmp_path):  # FM-04: a fridge's cycles
    db = energy(tmp_path, lambda t: 30.0 if t >= MONDAY + timedelta(hours=1) else 0.0)
    store.save_card(app, site_id=1, mac="AA", card=Card("fridge"), now=NOW)
    assert jobs.watch_pass(app, db, CFG, site_id=1, now=NOW) == []
    store.save_card(app, site_id=1, mac="AA", card=Card("fan"), now=NOW)
    assert kinds(jobs.watch_pass(app, db, CFG, site_id=1, now=NOW)) == [("unusual_use", "AA")]


# the logger itself: a heartbeat from outside it


def gappy(tmp_path, *stretches):
    """energy.db with Plug-6 at 1 W only in these (from, to) local hour stretches."""
    rows = [
        ((MONDAY + timedelta(minutes=m)).astimezone(UTC).isoformat(), "Plug-6", 1.0, None)
        for a, b in stretches for m in range(int(a * 60), int(b * 60))
    ]  # fmt: skip
    path = tmp_path / "energy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE readings (ts_utc, plug, power_w, error)")
        db.executemany("INSERT INTO readings VALUES (?, ?, ?, ?)", rows)
    db.close()
    return store.open_readings(path)


def test_a_logger_that_stopped_is_reported_once(app, tmp_path):
    db = gappy(tmp_path, (0, 1))  # last reading 00:59
    at = MONDAY + timedelta(hours=1, minutes=10)
    (e,) = jobs.watch_pass(app, db, CFG, site_id=1, now=at)
    last = MONDAY + timedelta(minutes=59)
    assert (e.kind, e.start, e.facts) == ("logger_stopped", last, {"minutes": 11})
    assert jobs.watch_pass(app, db, CFG, site_id=1, now=at + timedelta(minutes=15)) == []


def test_a_logger_that_stops_again_is_a_new_event(app, tmp_path):
    db = gappy(tmp_path, (0, 1), (2, 3))
    jobs.watch_pass(app, db, CFG, site_id=1, now=MONDAY + timedelta(hours=1, minutes=10))
    found = jobs.watch_pass(app, db, CFG, site_id=1, now=MONDAY + timedelta(hours=3, minutes=10))
    assert kinds(found) == [("logger_stopped", None)]


def test_failed_reads_still_mean_the_logger_runs(app, tmp_path):
    every = {MONDAY + timedelta(minutes=m) for m in range(3 * 60, 4 * 60)}
    db = energy(tmp_path, lambda t: 1.0, failed=every)
    found = jobs.watch_pass(app, db, CFG, site_id=1, now=NOW)
    assert "logger_stopped" not in [e.kind for e in found]


# the respond step: each finished unusual run gets one answer


END = START + timedelta(days=14)  # just after the last of the 14 days in desk()


def desk_energy(tmp_path, waste=(0, 1)):
    """energy.db with 14 days of Desk 2 on Plug-6 and on Plug-2 (EV-01's pattern)."""
    rows = [
        (at.isoformat(), plug, w, None) for at, w in desk(waste) for plug in ("Plug-6", "Plug-2")
    ]
    path = tmp_path / "energy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE readings (ts_utc, plug, power_w, error)")
        db.executemany("INSERT INTO readings VALUES (?, ?, ?, ?)", rows)
    db.close()
    return store.open_readings(path)


def left_on(app, day, mac="AA", late=timedelta(0)):
    """The night of `day` saved as an event. late: the event starts that long after the run."""
    event = Event("unusual_use", mac, night(day).start + late, {})
    store.add_event(app, site_id=1, event=event, dedupe=f"{mac}|{day}")


def answer(app, energy, model, now=END, month_usd=3.0, cfg=CFG):
    llm = Llm(app, 1, cfg.tz, cfg.tiers, Budget(month_usd, 0.05), model, clock=lambda: now)
    return jobs.respond_pass(app, energy, cfg, llm, site_id=1, now=now)


def statuses(app):
    return [s for (s,) in app.execute("SELECT status FROM events ORDER BY id")]


def test_a_finished_run_gets_one_proposal(app, tmp_path):
    model, energy = FakeModel(), desk_energy(tmp_path)
    left_on(app, 1)
    (p,) = answer(app, energy, model)
    assert [(c.action, c.time, c.days) for c in p.changes] == [
        ("off", "01:30", "1111111"), ("on", "07:00", "1111111")
    ]  # fmt: skip
    assert statuses(app) == ["done"]
    assert answer(app, energy, model) == []  # answered once only
    assert model.calls == 1


def test_a_run_still_going_waits_for_its_end(app, tmp_path):
    model, energy = FakeModel(), desk_energy(tmp_path, waste=(13,))
    left_on(app, 13)
    assert answer(app, energy, model, now=END - timedelta(minutes=90)) == []  # 04:30: on
    assert (statuses(app), model.calls) == (["new"], 0)
    assert len(answer(app, energy, model, now=END + timedelta(minutes=6))) == 1  # off at 06:00


def test_a_run_that_just_ended_is_answered_at_once(app, tmp_path):
    left_on(app, 12)
    just_after = START + timedelta(days=13, minutes=2)  # 06:02: two readings at 0 W since
    assert len(answer(app, desk_energy(tmp_path, waste=(12,)), FakeModel(), now=just_after)) == 1


def test_an_event_inside_a_longer_run_gets_the_whole_run(app, tmp_path):
    left_on(app, 1, late=timedelta(hours=1))  # a pass that saw only the run's last hours
    assert len(answer(app, desk_energy(tmp_path), FakeModel())) == 1


def test_a_fix_the_owner_rejected_is_not_offered_again(app, tmp_path):  # EV-02
    model, energy = FakeModel(), desk_energy(tmp_path)
    left_on(app, 1)
    (p,) = answer(app, energy, model)
    store.add_proposal(app, site_id=1, event_id=1, proposal=p, status="rejected", now=END)
    left_on(app, 0)  # the same waste, another night
    assert answer(app, energy, model) == []
    assert model.calls == 1


def test_a_run_no_longer_in_the_readings_is_done_without_a_call(app, tmp_path):
    model = FakeModel()
    left_on(app, 1)
    assert answer(app, desk_energy(tmp_path, waste=()), model) == []
    assert (statuses(app), model.calls) == (["done"], 0)
    assert app.execute("SELECT action, ref, detail FROM audit").fetchall() == [
        ("no_case", "event:1", '{"why": "no run in the readings"}')
    ]  # fmt: skip


def test_under_a_week_of_readings_is_done_without_a_call(app, tmp_path):
    model = FakeModel()
    left_on(app, 1)
    assert answer(app, desk_energy(tmp_path), model, now=START + timedelta(days=6, hours=23)) == []
    assert (statuses(app), model.calls) == (["done"], 0)
    assert app.execute("SELECT detail FROM audit").fetchone() == (
        '{"why": "under 7 days of readings"}',
    )


def test_the_policy_sets_the_days_needed(app, tmp_path):
    left_on(app, 1)
    sooner = replace(CFG, policy=Policy(min_days=3))
    assert len(answer(app, desk_energy(tmp_path), FakeModel(), now=START + timedelta(days=4),
                      cfg=sooner)) == 1  # fmt: skip


def test_nothing_to_offer_is_said_too(app, tmp_path):
    left_on(app, 1)
    strict = replace(CFG, policy=Policy(max_off_h=5))  # the 01:30 to 07:00 fix is 5.5 h
    assert answer(app, desk_energy(tmp_path), FakeModel(), cfg=strict) == []
    assert app.execute("SELECT detail FROM audit").fetchone() == ('{"why": "nothing to offer"}',)


def test_the_site_policy_sets_what_the_pass_finds(app, tmp_path):
    db = energy(tmp_path, lambda t: 30.0 if 1 <= t.hour < 3 else 1.0)
    calm = replace(CFG, policy=Policy(more_w=40, small_w=40))
    assert jobs.watch_pass(app, db, calm, site_id=1, now=NOW) == []


def test_the_model_is_told_how_many_days_the_readings_cover(app, tmp_path):
    left_on(app, 1)
    answer(app, desk_energy(tmp_path), FakeModel(), now=START + timedelta(days=7))
    (prompt,) = app.execute("SELECT prompt FROM llm_calls").fetchone()
    assert "Over the last 7 days it was never in use" in prompt


def test_a_plug_with_access_none_is_never_asked_about(app, tmp_path):  # EV-05
    model = FakeModel()
    left_on(app, 1, mac="BB")  # Plug-2: the 3D printer
    assert answer(app, desk_energy(tmp_path), model) == []
    assert (statuses(app), model.calls) == (["done"], 0)


def test_at_the_budget_cap_events_wait(app, tmp_path):
    model = FakeModel()
    left_on(app, 0)
    left_on(app, 1)
    assert answer(app, desk_energy(tmp_path), model, month_usd=0.0) == []
    assert (statuses(app), model.calls) == (["new", "new"], 0)


def test_only_unusual_runs_go_to_the_responder(app, tmp_path):
    reported_at(app, "offline", "AA", night(1).start)
    assert answer(app, desk_energy(tmp_path), FakeModel()) == []
    assert statuses(app) == ["new"]  # kept for the owner's inbox (B4)


def test_the_watch_pass_answers_when_asked(app, tmp_path):
    left_on(app, 1)
    llm = Llm(app, 1, CFG.tz, CFG.tiers, CFG.budget, FakeModel(), clock=lambda: END)
    _, made = jobs.run_due(app, desk_energy(tmp_path), replace(CFG, plugs={}), site_id=1, now=END,
                           llm=llm)  # fmt: skip
    assert len(made) == 1


@pytest.mark.parametrize(
    "policy",
    [
        Policy(history_days=8),  # night 1 is 13 days back: out of the readings
        Policy(more_w=40, small_w=40),  # the readings no longer show it as unusual
    ],
)
def test_the_device_policy_sets_which_readings_and_runs_the_answer_sees(app, tmp_path, policy):
    left_on(app, 1)
    assert answer(app, desk_energy(tmp_path), FakeModel(), cfg=replace(CFG, policy=policy)) == []
    (why,) = app.execute("SELECT detail FROM audit").fetchone()
    assert why == '{"why": "no run in the readings"}'


# learning each plug's levels (B3b)


def test_learning_saves_each_plugs_values(app, tmp_path):
    found = jobs.learn_pass(app, desk_energy(tmp_path), CFG, site_id=1, now=END)
    assert found["Plug-6"] == learn.Levels(standby=5.0, active=30.0)
    assert {k: v for k, (v, _) in store.learned(app, mac="AA").items()} == {
        "use_w": 12.5, "more_w": 12.5, "small_w": 2.5
    }  # fmt: skip


def test_a_plug_with_too_few_days_keeps_what_it_learned(app, tmp_path):
    store.save_learned(app, site_id=1, mac="AA", values={"use_w": (9.0, "old")}, at=END)
    found = jobs.learn_pass(app, desk_energy(tmp_path), CFG, site_id=1,
                            now=START + timedelta(days=6, hours=23))  # fmt: skip
    assert "Plug-6" not in found
    assert store.learned(app, mac="AA") == {"use_w": (9.0, "old")}


def test_learned_values_reach_the_watch_pass(app, tmp_path):
    db = energy(tmp_path, lambda t: 2.0 if t >= MONDAY + timedelta(minutes=30) else 0.0)
    assert jobs.watch_pass(app, db, CFG, site_id=1, now=NOW) == []  # 2 W: under the 3 W rule
    store.save_learned(app, site_id=1, mac="AA", values={"small_w": (1.0, "learned")}, at=NOW)
    assert kinds(jobs.watch_pass(app, db, CFG, site_id=1, now=NOW)) == [("unusual_use", "AA")]


def test_learned_values_reach_the_respond_step(app, tmp_path):
    left_on(app, 1)
    store.save_learned(app, site_id=1, mac="AA", values={"use_w": (40.0, "learned")}, at=END)
    assert answer(app, desk_energy(tmp_path), FakeModel()) == []  # 30 W at the desk: not use
    (why,) = app.execute("SELECT detail FROM audit").fetchone()
    assert why == '{"why": "nothing to offer"}'


def test_a_plug_down_to_one_level_forgets_what_it_learned(app, tmp_path):
    store.save_learned(app, site_id=1, mac="AA", values={"use_w": (9.0, "old")}, at=END)
    jobs.learn_pass(app, desk_energy(tmp_path, waste=()), CFG, site_id=1, now=END)  # 30 W only
    assert store.learned(app, mac="AA") == {}


def test_learning_runs_once_a_day(app, plugs, tmp_path):
    ips = {name: p.ip for name, p in plugs.items()}
    jobs.run_due(app, desk_energy(tmp_path), replace(CFG, plugs=ips), site_id=1, now=END)
    assert store.last_run(app, site_id=1, job="learn") == END
    assert store.learned(app, mac="AA") != {}


# when the model fails: other events and other jobs go on (blast radius)


class Failing(FakeModel):
    """Raises `error` on the calls listed in `on` (1 = the first call); else answers."""

    def __init__(self, error, on=None):
        super().__init__()
        self.error, self.on = error, on

    def __call__(self, **kwargs):
        if self.on is None or self.calls + 1 in self.on:
            self.calls += 1
            raise self.error
        return super().__call__(**kwargs)


def tries(app):
    return [t for (t,) in app.execute("SELECT tries FROM events ORDER BY id")]


def test_an_unreachable_model_leaves_every_event_waiting(app, tmp_path):
    model = Failing(Unreachable("no internet"))
    left_on(app, 0)
    left_on(app, 1)
    assert answer(app, desk_energy(tmp_path), model) == []
    assert (statuses(app), tries(app), model.calls) == (["new", "new"], [0, 0], 1)  # stops at once


def test_a_failing_event_is_tried_again_on_the_next_pass(app, tmp_path):
    model = Failing(ValueError("bad answer"))
    left_on(app, 1)
    answer(app, desk_energy(tmp_path), model)
    assert (statuses(app), tries(app)) == (["new"], [1])


def test_one_bad_event_does_not_block_the_next(app, tmp_path):
    model = Failing(ValueError("bad answer"), on={1})
    left_on(app, 0)
    left_on(app, 1)
    made = answer(app, desk_energy(tmp_path), model)
    assert (len(made), statuses(app), tries(app)) == (1, ["new", "done"], [1, 0])


def test_after_3_failed_tries_an_event_is_parked_and_left_alone(app, tmp_path):
    model, energy = Failing(ValueError("bad answer")), desk_energy(tmp_path)
    left_on(app, 1)
    for _ in range(3):
        answer(app, energy, model)
    assert (statuses(app), tries(app)) == (["parked"], [3])
    answer(app, energy, model)
    assert model.calls == 3  # a parked event is not tried again
    actions = [a for (a,) in app.execute("SELECT action FROM audit ORDER BY id")]
    assert actions == ["respond_failed"] * 3 + ["event_parked"]


def test_other_jobs_still_run_when_the_respond_step_breaks(app, plugs, quiet, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("a bug")

    monkeypatch.setattr(jobs, "respond_pass", broken)
    ips = {name: p.ip for name, p in plugs.items()}
    model = Llm(app, 1, CFG.tz, CFG.tiers, Budget(3.0, 0.05), FakeModel(), clock=lambda: NOW)
    jobs.run_due(app, quiet, replace(CFG, plugs=ips), site_id=1, now=NOW, llm=model)
    ran = {job: store.last_run(app, site_id=1, job=job) for job in ("watch", "timers", "learn")}
    assert ran == {"watch": NOW, "timers": NOW, "learn": NOW}
    (action,) = app.execute("SELECT action FROM audit").fetchone()
    assert action == "respond_pass_failed"
