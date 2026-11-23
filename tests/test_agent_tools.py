"""The housemate agent's read tools: plain words from code, one tool at a time (FR-15 v2)."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agentc import config, store
from agentc.agent_tools import Tools, segments
from agentc.calc import Period, ProfileHour, TimeOfUse
from agentc.house import Card
from agentc.watcher import Event
from tests.test_cli import EXAMPLE

COLOMBO = ZoneInfo("Asia/Colombo")
MONDAY = datetime(2026, 11, 2, tzinfo=COLOMBO)
NOW = MONDAY + timedelta(hours=2, minutes=10)  # Monday 02:10, the Desk 2 night
CFG = config.load(EXAMPLE)  # Plug-1 is "Desk 2", Plug-2 is "3D printer"


def at(h, m=0, day=0):
    return MONDAY + timedelta(days=day, hours=h, minutes=m)


def minutes(start, n, watts):
    return [((start + timedelta(minutes=k)).astimezone(UTC), watts) for k in range(n)]


@pytest.fixture
def tools(tmp_path):
    app = store.connect(tmp_path / "app.db")
    store.add_site(app, name="home", tz="Asia/Colombo")
    for name, mac in (("Plug-1", "AA"), ("Plug-2", "BB")):
        store.plug_seen(app, site_id=1, mac=mac, name=name, ip="192.0.2.1", timers_on=True,
                        now=NOW)  # fmt: skip
    store.save_card(app, site_id=1, mac="AA", card=Card("computer", "desk 2", "monitor"), now=NOW)
    store.save_profile(app, site_id=1, mac="AA",
                       profile=[ProfileHour(h, 0.0, 0.05, 3) for h in range(168)])  # fmt: skip
    rows = [(t.isoformat(), "Plug-1", w, None) for t, w in
            minutes(at(1, 37), 14, 30.0) + minutes(at(1, 51), 19, 5.0)]  # fmt: skip
    rows += [(t.isoformat(), "Plug-2", 0.0, None) for t, _ in minutes(at(2), 10, 0.0)]
    path = tmp_path / "energy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE readings (ts_utc, plug, power_w, error)")
        db.executemany("INSERT INTO readings VALUES (?, ?, ?, ?)", rows)
    db.close()
    return Tools(app, store.open_readings(path), CFG, site_id=1, now=NOW)


# segments: a stretch of readings in plain words, not raw minutes


def test_segments_join_readings_at_the_same_level():
    readings = minutes(at(1, 37), 14, 30.0) + minutes(at(1, 51), 19, 5.0)
    assert segments(readings, tz="Asia/Colombo") == (
        "Mon 01:37 to 01:50: about 30 W\nMon 01:51 to 02:09: about 5 W"
    )


def test_segments_say_where_readings_are_missing():
    readings = minutes(at(1), 10, 5.0) + minutes(at(1, 30), 10, 5.0)
    assert segments(readings, tz="Asia/Colombo") == (
        "Mon 01:00 to 01:09: about 5 W\nMon 01:10 to 01:29: no readings\n"
        "Mon 01:30 to 01:39: about 5 W"
    )


def test_segments_of_nothing():
    assert segments([], tz="Asia/Colombo") == "No readings."


# the tools


def test_device_shows_its_card(tools):
    assert tools.run("device", {"device": "Desk 2"}).startswith(
        "Desk 2: computer. Where: desk 2. Plugged in: monitor."
    )


def test_an_unknown_device_lists_the_known_ones(tools):
    assert tools.run("device", {"device": "Toaster"}) == (
        "Error: no device named 'Toaster'. Known: Desk 2, 3D printer"
    )


def test_power_shows_the_last_hours(tools):
    assert tools.run("power", {"device": "Desk 2", "hours": 1}) == (
        "Mon 01:37 to 01:50: about 30 W\nMon 01:51 to 02:09: about 5 W"
    )


def test_power_hours_stay_between_1_and_24(tools):
    assert tools.run("power", {"device": "Desk 2", "hours": 99}) == tools.run(
        "power", {"device": "Desk 2", "hours": 24}
    )


def test_usual_gives_each_hour_of_a_day(tools):
    text = tools.run("usual", {"device": "Desk 2", "day": "Monday", "start": "01:00",
                               "end": "03:00"})  # fmt: skip
    assert text == ("Monday 01:00: usually about 0 W, on 5% of the time\n"
                    "Monday 02:00: usually about 0 W, on 5% of the time")  # fmt: skip


def test_usual_without_a_profile_says_so(tools):
    assert tools.run("usual", {"device": "3D printer", "day": "Monday", "start": "01:00",
                               "end": "02:00"}) == "No usual week yet for 3D printer."  # fmt: skip


def test_house_now_shows_every_device(tools):
    assert tools.run("house_now", {}) == "Desk 2: 5 W\n3D printer: 0 W"


def test_house_now_says_when_a_plug_has_no_recent_reading(tools):
    later = replace(tools, now=NOW + timedelta(hours=1))
    assert (
        later.run("house_now", {})
        == "Desk 2: no reading in the last 5 min\n3D printer: no reading in the last 5 min"
    )


def test_past_signals_lists_this_devices_recent_events(tools):
    old = Event("left_on", "AA", at(1, 37, day=-7), {"minutes": 30, "peak_w": 5.0})
    store.add_event(tools.app, site_id=1, event=old, dedupe="old")
    assert tools.run("past_signals", {"device": "Desk 2"}) == "Mon 26 Oct 01:37: left on"


def test_past_signals_of_a_quiet_device(tools):
    assert tools.run("past_signals", {"device": "3D printer"}) == "None in the last 14 days."


def test_cost_of_a_stretch(tools):
    text = tools.run("cost", {"device": "Desk 2", "start": "2026-11-02 01:51",
                              "end": "2026-11-02 02:10"})  # fmt: skip
    assert text == "From Mon 01:51 to Mon 02:10: 0.002 kWh, LKR 0.1, 0.001 kgCO2e"


def test_cost_with_a_bad_time_is_an_error(tools):
    assert tools.run("cost", {"device": "Desk 2", "start": "tonight", "end": "later"}).startswith(
        "Error: times are YYYY-MM-DD HH:MM"
    )


def test_an_unknown_tool_is_an_error(tools):
    assert tools.run("switch_off", {}) == "Error: no tool named 'switch_off'"


def test_a_device_not_read_yet_is_an_error(tools):
    fresh = replace(tools, app=store.connect(":memory:"))
    store.add_site(fresh.app, name="home", tz="Asia/Colombo")
    assert fresh.run("power", {"device": "Desk 2", "hours": 1}) == (
        "Error: Desk 2 has not been read yet"
    )


def test_cost_on_a_time_of_use_tariff_uses_the_rate_at_the_start(tools):
    tou = TimeOfUse((Period("05:30", 30.0), Period("22:30", 20.0)), fixed=0.0)
    cheap = replace(tools, cfg=replace(CFG, tariff=tou, levy=0.0))
    text = cheap.run("cost", {"device": "Desk 2", "start": "2026-11-02 01:51",
                              "end": "2026-11-02 02:10"})  # fmt: skip
    assert "LKR 0.0," in text  # 0.0016 kWh x LKR 20 = 0.03


def test_cost_without_a_tariff_or_carbon_is_kwh_only(tools):
    bare = replace(tools, cfg=replace(CFG, tariff=None, factor=None))
    text = bare.run("cost", {"device": "Desk 2", "start": "2026-11-02 01:51",
                             "end": "2026-11-02 02:10"})  # fmt: skip
    assert text == "From Mon 01:51 to Mon 02:10: 0.002 kWh (no tariff or carbon factor set)"
