from datetime import UTC, datetime, timedelta

import pytest

from agentc import store, timers
from tests.fake_tasmota import FakePlug

NOW = datetime(2026, 11, 13, 7, 8, 23, tzinfo=UTC)  # same instant as the fake's clock


@pytest.fixture
def db(tmp_path):
    db = store.connect(tmp_path / "app.db")
    db.execute("INSERT INTO sites (id, name, tz) VALUES (1, 'home', 'Asia/Colombo')")
    return db


@pytest.fixture
def plugs():
    """Two running fakes, named like the real ones."""
    ps = {"Plug-2": FakePlug(mac="AA:00:00:00:00:02"), "Plug-6": FakePlug(mac="AA:00:00:00:00:06")}
    for p in ps.values():
        p.start()
    yield ps
    for p in ps.values():
        p.stop()


def run(db, plugs):
    return timers.import_all(db, site_id=1, plugs={n: p.ip for n, p in plugs.items()}, now=NOW)


def kinds(problems):
    return {(p.plug, p.kind) for p in problems}


def test_clean_plugs_import_with_no_problems(db, plugs):
    plugs["Plug-2"].timers[1].update(Enable=1, Time="07:30", Days="0111110", Action=1)
    assert run(db, plugs) == []
    first = store.load_timers(db, mac="AA:00:00:00:00:02")[0]
    assert (first.enable, first.time, first.days) == (True, "07:30", "0111110")


def test_unreachable_plug_is_reported_and_the_others_still_import(db, plugs):
    plugs["Plug-6"].offline = True
    assert kinds(run(db, plugs)) == {("Plug-6", "unreachable")}
    assert len(store.load_timers(db, mac="AA:00:00:00:00:02")) == 16


def test_switch_off_with_an_enabled_timer_is_reported(db, plugs):
    plugs["Plug-6"].timers_on = False
    plugs["Plug-6"].timers[1]["Enable"] = 1
    assert kinds(run(db, plugs)) == {("Plug-6", "timers_off")}


def test_switch_off_with_no_timers_set_is_not_a_problem(db, plugs):
    plugs["Plug-6"].timers_on = False
    assert run(db, plugs) == []


def test_wrong_timezone_is_reported(db, plugs):
    plugs["Plug-6"].timezone = "+00:00"
    assert kinds(run(db, plugs)) == {("Plug-6", "timezone")}


@pytest.mark.parametrize(("skew_s", "reported"), [(30, False), (119, False), (300, True)])
def test_clock_drift_over_2_minutes_is_reported(db, plugs, skew_s, reported):
    plugs["Plug-6"].utc = (NOW + timedelta(seconds=skew_s)).replace(tzinfo=None).isoformat()
    assert (("Plug-6", "clock") in kinds(run(db, plugs))) is reported


def test_every_problem_has_plain_words(db, plugs):
    plugs["Plug-6"].timers_on = False
    plugs["Plug-6"].timers[1]["Enable"] = 1
    (problem,) = run(db, plugs)
    assert "Plug-6" in problem.text and "timers" in problem.text.lower()
