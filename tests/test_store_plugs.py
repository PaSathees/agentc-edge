from datetime import UTC, datetime

import pytest

from agentc import store
from agentc.devices.base import Timer

T0 = datetime(2026, 11, 13, 7, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 13, 7, 15, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    db = store.connect(tmp_path / "app.db")
    db.execute("INSERT INTO sites (id, name, tz) VALUES (1, 'home', 'Asia/Colombo')")
    return db


def plug_row(db, mac="AA"):
    sql = "SELECT name, ip, first_seen, last_seen, reachable, last_error, timers_on FROM plugs"
    return db.execute(sql + " WHERE mac = ?", (mac,)).fetchone()


def sixteen(time="00:00"):
    return [Timer(n, False, 0, time, 0, "0000000", False, 1, 0) for n in range(1, 17)]


def test_plug_seen_twice_keeps_first_seen_and_updates_the_rest(db):
    store.plug_seen(db, site_id=1, mac="AA", name="Plug-6", ip="192.0.2.1", timers_on=False, now=T0)
    store.plug_seen(db, site_id=1, mac="AA", name="Plug-6", ip="192.0.2.2", timers_on=True, now=T1)
    assert plug_row(db) == ("Plug-6", "192.0.2.2", T0.isoformat(), T1.isoformat(), 1, None, 1)


def test_plug_failed_marks_it_unreachable_and_keeps_last_seen(db):
    store.plug_seen(db, site_id=1, mac="AA", name="Plug-6", ip="192.0.2.1", timers_on=True, now=T0)
    store.plug_failed(db, name="Plug-6", error="timed out")
    assert plug_row(db)[3:6] == (T0.isoformat(), 0, "timed out")


def test_seen_again_after_a_failure_clears_the_error(db):
    store.plug_seen(db, site_id=1, mac="AA", name="Plug-6", ip="192.0.2.1", timers_on=True, now=T0)
    store.plug_failed(db, name="Plug-6", error="timed out")
    store.plug_seen(db, site_id=1, mac="AA", name="Plug-6", ip="192.0.2.1", timers_on=True, now=T1)
    assert plug_row(db)[4:6] == (1, None)


def test_timers_round_trip(db):
    store.plug_seen(db, site_id=1, mac="AA", name="Plug-2", ip="192.0.2.1", timers_on=True, now=T0)
    timers = sixteen()
    timers[0] = Timer(1, True, 0, "07:30", 0, "0111110", True, 1, 1)
    store.save_timers(db, site_id=1, mac="AA", timers=timers, now=T0)
    assert store.load_timers(db, mac="AA") == timers


def test_saving_timers_again_replaces_them(db):
    store.plug_seen(db, site_id=1, mac="AA", name="Plug-2", ip="192.0.2.1", timers_on=True, now=T0)
    store.save_timers(db, site_id=1, mac="AA", timers=sixteen("00:00"), now=T0)
    store.save_timers(db, site_id=1, mac="AA", timers=sixteen("06:00"), now=T1)
    assert [t.time for t in store.load_timers(db, mac="AA")] == ["06:00"] * 16
