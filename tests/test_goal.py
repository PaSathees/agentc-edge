"""The monthly carbon goal: the month so far, where it is heading, and goal drift (5b3)."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agentc import cli, config, goal, jobs, readings, store, words
from agentc.config import Goal
from agentc.watcher import Event
from tests.test_cli import EXAMPLE

ZONE = ZoneInfo("Asia/Colombo")
MID = datetime(2026, 11, 15, tzinfo=ZONE)  # half way: 14 days gone, 16 to go
CFG = replace(config.load(EXAMPLE), goal=Goal(4.0))


@pytest.fixture
def energy(tmp_path):
    """Plug-1 at a steady 10 W, every minute from 1 Nov to the 15th; Plug-2 never read."""
    path = tmp_path / "energy.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE readings (ts_utc, plug, power_w, error)")
    start = datetime(2026, 11, 1, tzinfo=ZONE)
    rows = [((start + timedelta(minutes=n)).astimezone(UTC).isoformat(), "Plug-1", 10.0, None)
            for n in range(14 * 24 * 60)]  # fmt: skip
    db.executemany("INSERT INTO readings VALUES (?, ?, ?, ?)", rows)
    db.commit()
    db.close()
    return readings.connect(path)


def test_the_month_so_far_and_where_it_is_heading(energy):
    name, used, projected = goal.month(energy, CFG, now=MID)
    assert (name, round(used, 2), round(projected, 2)) == ("2026-11", 3.36, 7.2)  # 10 W x 30 days


@pytest.mark.parametrize(("kg", "state"), [(4.0, "on track"), (3.2, "at risk"), (3.0, "off track")])
def test_status_against_the_goal(energy, kg, state):
    s = goal.status(energy, replace(CFG, goal=Goal(kg)), now=MID)  # heading to 3.05 kgCO2e
    assert s is not None and s.state == state and round(s.projected_kg, 2) == 3.05


def test_the_status_line_says_how_far_over(energy):
    s = goal.status(energy, replace(CFG, goal=Goal(3.0)), now=MID)
    assert s is not None and s.text(tz=CFG.tz) == (
        "November: 1.4 of 3.0 kgCO2e so far (3.4 kWh); heading to 3.1 kgCO2e by 30 Nov,"
        " 0.05 over: off track.")  # fmt: skip
    assert goal.Status.from_json(s.to_json()) == s


def test_no_goal_or_no_carbon_factor_no_status(energy):
    assert goal.status(energy, replace(CFG, goal=None), now=MID) is None
    assert goal.status(energy, replace(CFG, factor=None), now=MID) is None


def test_an_hour_never_seen_counts_at_the_plugs_own_average(tmp_path):
    db = sqlite3.connect(tmp_path / "one.db")
    db.execute("CREATE TABLE readings (ts_utc, plug, power_w, error)")
    hour = datetime(2026, 11, 10, 8, tzinfo=ZONE)  # one hour seen in 14 days: Tuesday 08:00
    db.executemany("INSERT INTO readings VALUES (?, ?, ?, ?)", [
        ((hour + timedelta(minutes=n)).astimezone(UTC).isoformat(), "Plug-1", 10.0, None)
        for n in range(60)])  # fmt: skip
    _, used, projected = goal.month(db, CFG, now=MID)
    assert round(projected - used, 2) == 3.84  # 16 days left, every hour at its 10 W


def test_a_plug_with_no_recent_readings_adds_nothing(energy):
    later = datetime(2026, 11, 30, tzinfo=ZONE)  # its readings stopped on the 15th
    _, used, projected = goal.month(energy, CFG, now=later)
    assert round(used, 2) == 3.36 and projected == used


# config


@pytest.mark.parametrize(
    ("block", "why"),
    [("month_kgco2e = 0", "must be a number above 0"), ('month_kgco2e = "4"', "above 0"),
     ("month_kgco2e = 4\nat_risk = 1.5", "between 0 and 1"),
     ("month_kgco2e = true", "above 0"), ("month_kgco2e = 4\nat_risk = true", "between 0 and 1"),
     ("month_kgco2e = nan", "above 0"), ("month_kgco2e = inf", "above 0"),
     ("month_kgco2e = 4\nat_risk = nan", "between 0 and 1")],
)  # fmt: skip
def test_a_bad_goal_fails_on_load(tmp_path, block, why):
    path = tmp_path / "config.toml"
    path.write_text(EXAMPLE.read_text() + f"\n[goal]\n{block}\n")
    with pytest.raises(ValueError, match=why):
        config.load(path)


def test_a_goal_loads_and_needs_carbon(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(EXAMPLE.read_text() + "\n[goal]\nmonth_kgco2e = 4\nat_risk = 0.8\n")
    assert config.load(path).goal == Goal(4.0, 0.8)
    start = EXAMPLE.read_text().index("[carbon]")
    end = EXAMPLE.read_text().index("\n[", start + 1)
    path.write_text(EXAMPLE.read_text()[:start] + EXAMPLE.read_text()[end:]
                    + "\n[goal]\nmonth_kgco2e = 4\n")  # fmt: skip
    with pytest.raises(ValueError, match="needs \\[carbon\\]"):
        config.load(path)


# the daily goal job and goal drift


@pytest.fixture
def app(tmp_path):
    db = store.connect(tmp_path / "app.db")
    store.add_site(db, name=CFG.site, tz=CFG.tz)
    return db


def notes(app):
    return [d["text"] for d in store.audit_since(app, site_id=1, action="note",
                                                 since=MID - timedelta(days=30))]  # fmt: skip


def test_the_status_is_kept_and_on_track_raises_nothing(app, energy):
    assert jobs.goal_check(app, energy, CFG, site_id=1, now=MID) == []
    kept = jobs.goal_status(app, site_id=1)
    assert kept is not None and kept.state == "on track" and notes(app) == []


def test_off_track_raises_goal_drift_once_a_week_with_a_note(app, energy):
    cfg = replace(CFG, goal=Goal(3.0))
    (e,) = jobs.goal_check(app, energy, cfg, site_id=1, now=MID)
    assert (e.kind, e.plug_mac, e.start.date().isoformat()) == ("goal_drift", None, "2026-11-09")
    assert jobs.goal_check(app, energy, cfg, site_id=1, now=MID + timedelta(hours=3)) == []
    (text,) = notes(app)  # the same week: once
    assert text.startswith("Heading over your carbon goal. November: 1.4 of 3.0 kgCO2e")


@pytest.fixture
def over(monkeypatch):
    """Always off track, so only the once-a-week rule decides."""
    s = goal.Status("2026-11", 9.0, 4.0, 12.0, 5.0, 3.0, "off track", MID.isoformat())
    monkeypatch.setattr(goal, "status", lambda energy, cfg, now: replace(
        s, month=f"{now.astimezone(ZONE):%Y-%m}"))  # fmt: skip


def check(app, energy, now):
    return jobs.goal_check(app, energy, CFG, site_id=1, now=now)


def test_drift_comes_once_a_week(app, energy, over):  # noqa: ARG001 (the fixture)
    assert len(check(app, energy, MID)) == 1  # Sunday 15 Nov: the week of Monday 9 Nov
    monday = MID + timedelta(days=1)
    assert len(check(app, energy, monday)) == 1  # a new week
    assert check(app, energy, monday + timedelta(days=6)) == []  # Sunday: the same week
    assert len(check(app, energy, monday + timedelta(days=7))) == 1


def test_a_new_month_starts_its_own_week(app, energy, over):  # noqa: ARG001 (the fixture)
    monday = datetime(2026, 11, 30, 12, tzinfo=ZONE)  # a Monday, the month's last day
    (nov,) = check(app, energy, monday)
    (dec,) = check(app, energy, monday + timedelta(days=1))
    assert (nov.start.day, dec.start.date().isoformat()) == (30, "2026-12-01")


def test_no_status_before_the_first_check(app):
    assert jobs.goal_status(app, site_id=1) is None


def test_without_a_goal_the_check_keeps_nothing(app, energy):
    assert jobs.goal_check(app, energy, replace(CFG, factor=None), site_id=1, now=MID) == []
    assert jobs.goal_status(app, site_id=1) is None


def test_the_jobs_check_the_goal_daily_when_one_is_set(app, energy):
    jobs.run_due(app, energy, CFG, site_id=1, now=MID)
    assert store.last_run(app, site_id=1, job="goal") == MID
    jobs.run_due(app, energy, replace(CFG, goal=None), site_id=1, now=MID + timedelta(days=2))
    assert store.last_run(app, site_id=1, job="goal") == MID  # no goal: not run


def test_goal_drift_in_words():
    e = Event("goal_drift", None, MID, {"projected_kg": 3.05, "goal_kg": 3.0})
    assert words.event_words(e, {}, tz=CFG.tz) == (
        "Heading over the carbon goal: about 3.05 of 3.00 kgCO2e by the month's end")  # fmt: skip


# agentc goal


@pytest.fixture
def home(tmp_path, energy):
    path = tmp_path / "config.toml"
    path.write_text(EXAMPLE.read_text())
    return path


def test_agentc_goal_shows_the_month_and_how_to_set_a_goal(home, capsys):
    assert cli.main(["--config", str(home), "goal"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"{datetime.now(ZONE):%B}: ") and "kgCO2e; heading to" in out
    assert "No [goal] set: add month_kgco2e under [goal]" in out


def test_agentc_goal_shows_the_status(home, capsys):
    home.write_text(home.read_text() + "\n[goal]\nmonth_kgco2e = 4\n")
    assert cli.main(["--config", str(home), "goal"]) == 0
    assert "of 4.0 kgCO2e so far" in capsys.readouterr().out


def test_agentc_goal_without_carbon_shows_kwh_only(home, capsys):
    text = home.read_text()
    start = text.index("[carbon]")
    home.write_text(text[:start] + text[text.index("\n[", start + 1) :])
    assert cli.main(["--config", str(home), "goal"]) == 0
    out = capsys.readouterr().out
    assert "kWh so far" in out and "kgCO2e" not in out.splitlines()[0]
