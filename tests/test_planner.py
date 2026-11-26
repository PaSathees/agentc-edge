"""The weekly Planner's code half: replaying timers on real readings, and the item checks."""

import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agentc import config, planner, readings, store
from agentc.config import Device, Rule
from agentc.devices.base import Timer
from agentc.house import Card
from agentc.planner import (
    Change,
    WhatIf,
    before_after,
    check_item,
    check_plan,
    ready,
    relay,
    what_if,
)
from tests.test_cli import EXAMPLE

ZONE = ZoneInfo("Asia/Colombo")
END = datetime(2026, 11, 23, 0, 0, tzinfo=ZONE)  # replays 9 to 22 Nov
CFG = config.load(EXAMPLE)
BOX = Device("PeoTV box", access="write")
ON6 = Timer(1, True, 0, "06:00", 0, "1111111", True, 1, 1)
OFF1 = Timer(2, True, 0, "01:00", 0, "1111111", True, 1, 0)
TIMERS = [ON6, OFF1]


# the relay, minute by minute


def test_the_relay_follows_the_timers_and_carries_over_midnight():
    day = relay(TIMERS, date(2026, 11, 20))
    assert day[0] is True  # still on from 06:00 the day before
    assert day[60] is False and day[6 * 60 - 1] is False  # off at 01:00 until 06:00
    assert day[6 * 60] is True and day[-1] is True


def test_with_no_timer_the_relay_is_the_owners():
    assert set(relay([], date(2026, 11, 20))) == {None}


def test_a_timer_acts_only_on_its_days_and_only_at_a_clock_time():
    weekday = Timer(3, True, 0, "08:00", 0, "0111110", True, 1, 0)
    sunrise = Timer(4, True, 1, "00:00", 0, "1111111", True, 1, 0)
    unset = Timer(5, False, 0, "09:00", 0, "1111111", True, 1, 0)
    friday, saturday = date(2026, 11, 20), date(2026, 11, 21)
    assert relay([weekday, sunrise, unset], friday)[8 * 60] is False
    assert relay([weekday], saturday)[8 * 60] is False  # carried from Friday, not acted on
    assert relay([Timer(3, True, 0, "08:00", 0, "-MTWTF-", True, 1, 0)], friday)[8 * 60] is False


# what_if, on readings with known answers


@pytest.fixture
def energy(tmp_path):
    """14 days of a TV box: idle at 2 W 06:00 to 19:00, watched at 30 W 19:00 to 22:00,
    idle again to 01:00, off (0 W) 01:00 to 06:00. One reading a minute."""
    path = tmp_path / "energy.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE readings (ts_utc, plug, power_w, error)")
    rows = []
    for n in range(14):
        day = datetime(2026, 11, 9, tzinfo=ZONE) + timedelta(days=n)
        for minute in range(24 * 60):
            h = minute // 60
            w = 0.0 if 1 <= h < 6 else 30.0 if 19 <= h < 22 else 2.0
            at = (day + timedelta(minutes=minute)).astimezone(UTC).isoformat()
            rows.append((at, "Plug-9", w, None))
    db.executemany("INSERT INTO readings VALUES (?, ?, ?, ?)", rows)
    db.commit()
    db.close()
    return readings.connect(path)


def run(energy, change, timers=TIMERS, device=BOX):
    return what_if(energy, CFG, device, "Plug-9", timers, change, end=END)


def test_a_later_start_saves_what_it_idled_and_was_never_used(energy):
    r = run(energy, Change("move_timer", slot=1, time="07:00"))
    assert (round(r.saved_kwh, 3), r.added_kwh, r.used_days, r.seen_days) == (0.06, 0.0, 0, 14)
    assert r.text().startswith("Saves about 0.06 kWh, LKR") and r.text().endswith(
        "In use in the new off time on 0 of 14 days.")  # fmt: skip


def test_an_earlier_off_over_viewing_hours_counts_the_days_in_use(energy):
    r = run(energy, Change("move_timer", slot=2, time="21:00"))
    # 21:00 to 22:00 watched at 30 W, 22:00 to 01:00 idle at 2 W: 36 Wh a day, 1.08 kWh in 30
    assert (round(r.saved_kwh, 2), r.used_days) == (1.08, 14)


def test_an_earlier_start_costs_its_standby(energy):
    r = run(energy, Change("move_timer", slot=1, time="05:00"))
    assert (r.saved_kwh, round(r.added_kwh, 3), r.net_kwh < 0) == (0.0, 0.06, True)
    assert r.text().startswith("Costs about 0.06 kWh")


def test_a_pair_adds_an_off_window(energy):
    r = run(energy, Change("add_pair", off="12:00", on="13:00", days="every_day"))
    assert round(r.saved_kwh, 3) == 0.06 and r.used_days == 0


def test_a_pair_that_matches_what_happens_already_saves_nothing(energy):
    same = run(energy, Change("add_pair", off="01:00", on="06:00", days="every_day"))
    assert same.text().startswith("No saving: it uses about the same.")


def test_a_pair_that_switches_on_early_costs_standby(energy):
    early = run(energy, Change("add_pair", off="02:00", on="03:00", days="every_day"))
    assert round(early.added_kwh, 2) == 0.18 and early.text().startswith("Costs about 0.18 kWh")


def test_no_readings_no_saving(tmp_path):
    db = sqlite3.connect(tmp_path / "e.db")
    db.execute("CREATE TABLE readings (ts_utc, plug, power_w, error)")
    r = run(db, Change("move_timer", slot=1, time="07:00"))
    assert r.seen_days == 0 and r.text() == (
        "No readings in the last 14 days, so no saving can be worked out.")  # fmt: skip


# the checks on a plan item


GOOD = WhatIf(0.06, 0.0, 0, 14, "0.06 kWh")
MOVE = Change("move_timer", slot=1, time="07:00")


MEDIA = Card("media")


def check(change=MOVE, device=BOX, card=MEDIA, timers=TIMERS, timers_on=True,
          result=GOOD, refused=()):  # fmt: skip
    return check_item(change, device=device, card=card, timers=timers, timers_on=timers_on,
                      result=result, refused=refused)  # fmt: skip


LATE = Rule("latest_off", "02:00", True, "The TVs may run until 02:00")
FULL = [Timer(n, True, 0, "03:00", 0, "1111111", True, 1, 0) for n in range(9, 16)]


@pytest.mark.parametrize(
    ("kw", "why"),
    [({}, None),
     ({"change": Change("add_pair", off="02:00", on="07:30", days="weekdays")}, None),
     ({"change": Change("remove_timer", slot=1)}, None),
     ({"device": Device("PeoTV box")}, "access: PeoTV box is set to advice."),
     ({"card": Card("router")}, "must run: PeoTV box must always run"),
     ({"change": Change("swap")}, "format: a change is one of"),
     ({"change": Change("move_timer", slot=7, time="07:00")}, "format: no set timer 7"),
     ({"timers": [replace(ON6, mode=1)]}, "format: only timers set at a clock time"),
     ({"change": Change("move_timer", slot=1, time="7am")}, "format: a time is HH:MM"),
     ({"change": Change("move_timer", slot=1, time="06:00")}, "format: the new time is the same"),
     ({"change": Change("add_pair", off="02:00", on="07:30", days="sometimes")},
      "format: a pair needs off and on"),
     ({"change": Change("add_pair", off="02:00", on="02:00", days="weekdays")},
      "format: off and on are the same time"),
     ({"change": Change("add_pair", off="02:00", on="07:30", days="weekdays"),
       "timers": FULL}, "slots: PeoTV box has no 2 free timer slots"),
     ({"timers_on": False}, "timers: PeoTV box's plug has its Timers switch off"),
     ({"change": Change("move_timer", slot=2, time="00:30"),
       "device": replace(BOX, rules=(LATE,))}, "rule: The TVs may run until 02:00"),
     ({"refused": [MOVE.to_json()]}, "not now: the owner said Not now to this in the last 30"),
     ({"result": WhatIf(0.06, 0.0, 3, 14, "x")}, None),  # 1 to 3 days: only to talk over
     ({"result": WhatIf(0.06, 0.0, 4, 14, "x")}, "used: PeoTV box was in use in the new off"),
     ({"result": WhatIf(0.0, 0.06, 0, 14, "x")}, "no saving: it would not use less")],
)  # fmt: skip
def test_check_item(kw, why):
    got = check(**kw)
    assert got == why if why is None else (got or "").startswith(why)


def test_a_move_of_an_on_timer_is_not_checked_against_latest_off_rules():
    assert check(device=replace(BOX, rules=(LATE,))) is None  # it switches on, not off


def test_only_a_change_that_never_cut_real_use_is_ready():
    assert ready(GOOD) and not ready(WhatIf(0.06, 0.0, 1, 14, "x"))


def test_a_plan_has_at_most_3_different_items():
    other = [Change("move_timer", slot=n, time="07:00") for n in range(1, 5)]
    assert check_plan(other[:3]) is None
    assert check_plan(other) == "format: at most 3 items a week"
    assert check_plan([MOVE, MOVE]) == "format: the same change twice"


def test_before_and_after_in_the_owners_words():
    assert before_after(MOVE, TIMERS) == ("On at 06:00, every day", "On at 07:00, every day")
    assert before_after(Change("remove_timer", slot=2), TIMERS) == (
        "Off at 01:00, every day", "No timer")  # fmt: skip
    assert before_after(Change("add_pair", off="02:00", on="07:30", days="weekdays"), []) == (
        "No timer", "Off at 02:00, on at 07:30, Monday to Friday")  # fmt: skip


def test_a_removed_timer_is_gone_from_the_replay():
    assert Change("remove_timer", slot=2).apply(TIMERS) == [ON6]


# plans in app.db


NOW = datetime(2026, 11, 23, 12, 30, tzinfo=UTC)


@pytest.fixture
def app(tmp_path):
    db = store.connect(tmp_path / "app.db")
    store.add_site(db, name="home", tz="Asia/Colombo")
    store.plug_seen(db, site_id=1, mac="AA", name="Plug-9", ip="192.0.2.9", timers_on=True,
                    now=NOW)  # fmt: skip
    return db


def test_a_plan_is_saved_once_a_week_with_its_items(app):
    item = ("AA", MOVE.to_json(), "It idles from 06:00.", "Saves about 0.06 kWh.", False,
            "On at 06:00", "On at 07:00")  # fmt: skip
    plan = store.add_plan(app, site_id=1, week="2026-11-16", summary="A quiet week.",
                          items=[item], now=NOW)  # fmt: skip
    assert store.latest_plan(app, site_id=1) == (1, "2026-11-16", "A quiet week.", NOW.isoformat())
    assert plan == 1 and store.has_plan(app, site_id=1, week="2026-11-16")
    again = store.add_plan(app, site_id=1, week="2026-11-16", summary="x", items=[item], now=NOW)
    assert again is None
    (i,) = store.plan_items(app, plan_id=1)
    assert (i.plug_mac, i.change, i.status, i.ready) == ("AA", MOVE.to_json(), "open", False)
    assert (i.was, i.becomes) == ("On at 06:00", "On at 07:00")
    assert store.plan_item(app, site_id=1, item_id=1) == i
    assert store.plan_item(app, site_id=2, item_id=1) is None  # another site's: not found


def test_a_new_plan_expires_the_open_items_of_older_ones(app):
    item = ("AA", MOVE.to_json(), "why", "saving", True, "", "")
    store.add_plan(app, site_id=1, week="2026-11-09", summary="s", items=[item, item], now=NOW)
    store.decide_item(app, item_id=1, status="done", now=NOW)
    later = NOW + timedelta(days=7)
    store.add_plan(app, site_id=1, week="2026-11-16", summary="s", items=[item], now=later)
    assert [(i.status, i.decided) for i in store.plan_items(app, plan_id=1)] == [
        ("done", NOW.isoformat()), ("expired", later.isoformat())]  # fmt: skip
    assert [i.status for i in store.plan_items(app, plan_id=2)] == ["open"]


def test_an_item_is_decided_once_and_not_now_is_remembered(app):
    item = ("AA", MOVE.to_json(), "why", "saving", True, "", "")
    store.add_plan(app, site_id=1, week="2026-11-16", summary="s", items=[item], now=NOW)
    assert store.decide_item(app, item_id=1, status="not_now", now=NOW)
    assert not store.decide_item(app, item_id=1, status="done", now=NOW)  # a second tap
    assert store.refused(app, mac="AA", since=NOW - timedelta(days=30)) == [MOVE.to_json()]
    assert store.refused(app, mac="AA", since=NOW + timedelta(seconds=1)) == []


def test_no_plan_before_the_first_week(app):
    assert store.latest_plan(app, site_id=1) is None


# the solver


def cand(cid, wh, cost=0, spot=None, ready=True):
    return planner.Candidate(cid, "Plug-9", "AA", "PeoTV box", None, "do it", wh, cost,
                             spot or cid, ready, "")  # fmt: skip


def ids(chosen):
    return [c.id for c in chosen]


def test_choose_closes_the_gap_with_the_least_disruption():
    cs = [cand("A", 80), cand("B", 10), cand("C", 30, 3, spot="B"), cand("D", 400, 1),
          cand("E", 2900, 2), cand("F", 200, 2)]  # fmt: skip
    assert ids(planner.choose(cs, gap_wh=1800)) == ["A", "B", "E"]  # the widget's example
    assert planner.shortfall_wh(planner.choose(cs, gap_wh=1800), gap_wh=1800) == 0


def test_with_no_gap_only_free_changes():
    cs = [cand("A", 80), cand("E", 2900, 2), cand("Z", 0)]
    assert ids(planner.choose(cs, gap_wh=0)) == ["A"]
    assert planner.choose([cand("Z", 0)], gap_wh=500) == []


def test_a_change_that_cut_use_must_save_100_wh_more():
    near = [cand("ready", 83, 0, spot="t1"), cand("talk", 96, 1, spot="t1")]
    assert ids(planner.choose(near, gap_wh=5000)) == ["ready"]
    far = [cand("ready", 83, 0, spot="t1"), cand("talk", 300, 1, spot="t1")]
    assert ids(planner.choose(far, gap_wh=5000)) == ["talk"]


def test_out_of_reach_picks_the_least_shortfall_and_at_most_3():
    cs = [cand(c, 100 * n) for n, c in enumerate("ABCDE", 1)]
    chosen = planner.choose(cs, gap_wh=10_000)
    assert ids(chosen) == ["C", "D", "E"]
    assert planner.shortfall_wh(chosen, gap_wh=10_000) == 8800


def test_idle_power_counts_only_on_but_not_in_use(tmp_path):
    db = sqlite3.connect(tmp_path / "e.db")
    db.execute("CREATE TABLE readings (ts_utc, plug, power_w, error)")
    start = END - timedelta(days=14)
    rows = []
    for n in range(14 * 24 * 60):  # 5 W idle, 30 W for 3 h each evening, 0 W 01:00 to 06:00
        at = start + timedelta(minutes=n)
        h = at.hour
        rows.append((at.astimezone(UTC).isoformat(), "Plug-9", 0.0 if 1 <= h < 6 else
                     30.0 if 19 <= h < 22 else 5.0, None))  # fmt: skip
    db.executemany("INSERT INTO readings VALUES (?, ?, ?, ?)", rows)
    wh, days = planner.idle_wh(db, BOX, "Plug-9", end=END)
    assert (wh, days) == (2400, 14)  # 16 h a day at 5 W: 80 Wh a day, so 2400 Wh in 30
    empty = sqlite3.connect(tmp_path / "none.db")
    empty.execute("CREATE TABLE readings (ts_utc, plug, power_w, error)")
    assert planner.idle_wh(empty, BOX, "Plug-9", end=END) == (0, 0)


def test_numbers_too_large_for_the_solver_raise():
    with pytest.raises(ValueError, match="too large"):
        planner.choose([cand("A", 10**12), cand("B", 10**12)], gap_wh=10**12)
