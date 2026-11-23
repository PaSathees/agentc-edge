import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agentc import config, jobs, learn, store
from agentc.calc import ProfileHour
from agentc.devices.base import Timer
from agentc.house import Card
from agentc.watcher import Event
from tests.desk import START, desk
from tests.fake_tasmota import FakePlug
from tests.test_cli import EXAMPLE

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
    blackout = {MONDAY + timedelta(hours=3, minutes=m) for m in (55, 56)}  # since the 03:55 pass
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
    later = NOW + timedelta(minutes=4)
    assert due(app, quiet, plugs, later) == {"watch": NOW, "timers": NOW}  # too soon
    later = NOW + timedelta(minutes=5)
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


def test_a_saved_timer_that_switched_on_for_nothing_is_drift(app, tmp_path):
    on = Timer(1, True, 0, "00:30", 0, "1111111", True, 1, 1)
    store.save_timers(app, site_id=1, mac="AA", timers=[on], now=NOW)
    db = energy(tmp_path, lambda t: 1.0)  # standby all night
    assert ("automation_drift", "AA") in kinds(jobs.watch_pass(app, db, CFG, site_id=1, now=NOW))


def test_the_watch_pass_runs_every_5_minutes(app, quiet, plugs):
    due(app, quiet, plugs, NOW)
    assert due(app, quiet, plugs, NOW + timedelta(minutes=5))["watch"] == NOW + timedelta(minutes=5)


def changed_energy(tmp_path):
    """14 days of Plug-6: 25 W when on, then 60 W in the last 2 days."""
    rows = []
    for d in range(14):
        active = 60.0 if d >= 12 else 25.0
        for m in range(0, 24 * 60, 5):
            local = START + timedelta(days=d, minutes=m)
            rows.append((local.astimezone(UTC).isoformat(), "Plug-6", 2.0 if m < 720 else active,
                         None))  # fmt: skip
    path = tmp_path / "energy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE readings (ts_utc, plug, power_w, error)")
        db.executemany("INSERT INTO readings VALUES (?, ?, ?, ?)", rows)
    db.close()
    return store.open_readings(path)


def test_a_new_device_is_found_once_by_the_daily_check(app, tmp_path):
    db = changed_energy(tmp_path)
    assert kinds(jobs.change_check(app, db, CFG, site_id=1, now=END)) == [("change_point", "AA")]
    assert jobs.change_check(app, db, CFG, site_id=1, now=END + timedelta(days=1)) == []


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
    """energy.db with 14 days of Desk 2 on Plug-6 and on Plug-2."""
    rows = [
        (at.isoformat(), plug, w, None) for at, w in desk(waste) for plug in ("Plug-6", "Plug-2")
    ]
    path = tmp_path / "energy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE readings (ts_utc, plug, power_w, error)")
        db.executemany("INSERT INTO readings VALUES (?, ?, ?, ?)", rows)
    db.close()
    return store.open_readings(path)


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
    assert kinds(jobs.watch_pass(app, db, CFG, site_id=1, now=NOW)) == [("left_on", "AA")]


def test_a_plug_down_to_one_level_forgets_what_it_learned(app, tmp_path):
    store.save_learned(app, site_id=1, mac="AA", values={"use_w": (9.0, "old")}, at=END)
    jobs.learn_pass(app, desk_energy(tmp_path, waste=()), CFG, site_id=1, now=END)  # 30 W only
    assert store.learned(app, mac="AA") == {}


def test_learning_runs_once_a_day(app, plugs, tmp_path):
    ips = {name: p.ip for name, p in plugs.items()}
    jobs.run_due(app, desk_energy(tmp_path), replace(CFG, plugs=ips), site_id=1, now=END)
    assert store.last_run(app, site_id=1, job="learn") == END
    assert store.learned(app, mac="AA") != {}
