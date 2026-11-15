import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agentc import jobs, store
from agentc.calc import ProfileHour
from agentc.watcher import Event
from tests.fake_tasmota import FakePlug

COLOMBO = ZoneInfo("Asia/Colombo")
MONDAY = datetime(2026, 11, 2, tzinfo=COLOMBO)  # Monday 00:00 local
NOW = MONDAY + timedelta(hours=4)  # the pass runs at 04:00


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
    assert kinds(jobs.watch_pass(app, db, site_id=1, tz="Asia/Colombo", now=NOW)) == [
        ("unusual_use", "AA")
    ]
    assert len(store.load_events(app, site_id=1)) == 1


def test_the_next_pass_finds_it_again_but_saves_nothing(app, tmp_path):
    db = energy(tmp_path, lambda t: 30.0 if 1 <= t.hour < 3 else 1.0)
    jobs.watch_pass(app, db, site_id=1, tz="Asia/Colombo", now=NOW)
    assert jobs.watch_pass(app, db, site_id=1, tz="Asia/Colombo", now=NOW) == []


def test_a_normal_night_saves_nothing(app, tmp_path):
    db = energy(tmp_path, lambda t: 1.0)
    assert jobs.watch_pass(app, db, site_id=1, tz="Asia/Colombo", now=NOW) == []


def test_a_plug_that_stopped_answering_is_offline(app, tmp_path):
    db = energy(tmp_path, lambda t: None if t.hour == 3 and t.minute >= 50 else 1.0)
    assert kinds(jobs.watch_pass(app, db, site_id=1, tz="Asia/Colombo", now=NOW)) == [
        ("offline", "AA")
    ]


def test_a_blackout_between_passes_is_found(app, tmp_path):
    blackout = {MONDAY + timedelta(hours=3, minutes=m) for m in (50, 51)}  # 03:50 to 03:51
    db = energy(tmp_path, lambda t: 1.0, failed=blackout)  # all back well before the pass
    assert kinds(jobs.watch_pass(app, db, site_id=1, tz="Asia/Colombo", now=NOW)) == [
        ("power_cut", None)
    ]


def test_a_power_cut_is_one_site_event(app, tmp_path):
    last = {MONDAY + timedelta(hours=3, minutes=m) for m in range(55, 60)}
    db = energy(tmp_path, lambda t: 1.0, failed=last)
    assert kinds(jobs.watch_pass(app, db, site_id=1, tz="Asia/Colombo", now=NOW)) == [
        ("power_cut", None)
    ]


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
    return jobs.timer_check(app, site_id=1, tz="Asia/Colombo", plugs=ips, now=NOW)


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
    jobs.run_due(app, db, site_id=1, tz="Asia/Colombo", plugs=ips, now=now)
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
    assert jobs.watch_pass(app, db, site_id=1, tz="Asia/Colombo", now=NOW) == []


def test_a_plug_that_answered_in_between_is_a_new_outage(app, tmp_path):
    reported_at(app, "offline", "AA", MONDAY + timedelta(hours=1))
    db = energy(tmp_path, lambda t: None if t.hour == 1 or t.hour == 3 else 1.0)  # back at 02:00
    assert kinds(jobs.watch_pass(app, db, site_id=1, tz="Asia/Colombo", now=NOW)) == [
        ("offline", "AA")
    ]


def test_a_long_power_cut_is_one_event(app, tmp_path):
    reported_at(app, "power_cut", None, MONDAY + timedelta(hours=1))
    every = {MONDAY + timedelta(minutes=m) for m in range(60, 4 * 60)}  # all down from 01:00
    db = energy(tmp_path, lambda t: 1.0, failed=every)
    assert jobs.watch_pass(app, db, site_id=1, tz="Asia/Colombo", now=NOW) == []
