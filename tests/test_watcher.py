from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agentc.calc import ProfileHour
from agentc.devices.base import Timer
from agentc.watcher import (
    Event,
    Run,
    Usual,
    dedupe_key,
    is_unusual,
    offline_events,
    timer_mismatch,
    unusual_runs,
    unusual_use,
    usual_week,
)

COLOMBO = ZoneInfo("Asia/Colombo")
MONDAY = datetime(2026, 11, 2, tzinfo=COLOMBO)  # Monday 00:00 local
OFF = {h: Usual(watts=0.0, on_share=0.0) for h in range(168)}  # a plug that is usually off


def minutes(start, n, watts):
    """n readings, one a minute from `start` (local time), all at `watts`; returned in UTC."""
    return [((start + timedelta(minutes=m)).astimezone(UTC), watts) for m in range(n)]


def at(hour, minute=0):
    return MONDAY + timedelta(hours=hour, minutes=minute)


# usual_week: what "usual" means for each hour


def test_an_hour_with_3_days_keeps_its_own_value():
    profile = [ProfileHour(h, 5.0 if h == 1 else 1.0, 0.1, 3) for h in range(168)]
    assert usual_week(profile)[1] == Usual(watts=5.0, on_share=0.1)  # not the week's 1.0


def test_an_hour_with_under_3_days_uses_the_same_hour_across_the_week():
    # 01:00 is 4 W on Monday to Thursday, 30 W Friday to Sunday; 2 days each
    profile = [ProfileHour(h, 30.0 if h // 24 >= 4 else 4.0, 0.5, 2) for h in range(168)]
    usual = usual_week(profile)
    assert usual[1] == usual[24 * 6 + 1] == Usual(watts=4.0, on_share=0.5)  # middle of 7 values


def test_an_hour_with_no_data_at_all_has_no_usual():
    profile = [ProfileHour(h, 1.0, 0.0, 3) for h in range(168) if h % 24 != 5]
    assert 5 not in usual_week(profile)


# is_unusual: one reading against its usual


@pytest.mark.parametrize(
    ("watts", "usual", "unusual"),
    [
        (25.0, Usual(0.0, 0.0), True),  # usually off, now 25 W
        (9.0, Usual(0.0, 0.0), False),  # under 10 W more: too small to matter
        (25.0, Usual(15.0, 0.0), False),  # not 2 x usual
        (25.0, Usual(0.0, 0.5), False),  # on half the time at this hour: normal use
        (25.0, None, False),  # no usual for this hour: never alert
    ],
)
def test_is_unusual(watts, usual, unusual):
    assert is_unusual(watts, usual) is unusual


def test_a_raised_factor_needs_more_power():  # after a "false alarm", the factor goes up
    assert is_unusual(25.0, Usual(10.0, 0.0))
    assert not is_unusual(25.0, Usual(10.0, 0.0), factor=3.0)


# unusual_runs: stretches of unusual power


def runs(readings):
    return unusual_runs(readings, OFF, tz="Asia/Colombo")


def test_charger_left_on_overnight_is_one_run():
    found = runs(minutes(at(1), 121, 30.0))  # 01:00 to 03:00 at 30 W
    assert found == [Run(start=at(1).astimezone(UTC), end=at(3).astimezone(UTC), peak_w=30.0)]


def test_a_normal_day_has_no_runs():
    assert runs(minutes(at(0), 24 * 60, 1.0)) == []


@pytest.mark.parametrize(("n", "count"), [(30, 0), (31, 1)])  # 31 readings span 30 min
def test_a_run_must_last_30_minutes(n, count):
    assert len(runs(minutes(at(1), n, 30.0))) == count


def test_six_hours_unusual_is_one_run_not_many():
    assert len(runs(minutes(at(1), 6 * 60, 30.0))) == 1


def test_failed_reads_inside_a_run_are_skipped():
    readings = minutes(at(1), 20, 30.0) + minutes(at(1, 23), 20, 30.0)  # 3 min missing
    assert len(runs(readings)) == 1


def test_a_gap_over_5_minutes_restarts_the_count():
    readings = minutes(at(1), 20, 30.0) + minutes(at(1, 26), 20, 30.0)  # 6 min missing
    assert runs(readings) == []


def test_a_normal_reading_ends_the_run():
    readings = minutes(at(1), 20, 30.0) + minutes(at(1, 20), 1, 0.0) + minutes(at(1, 21), 20, 30.0)
    assert runs(readings) == []


# events: what gets saved, and the key that stops repeats


def test_a_run_becomes_an_unusual_use_event():
    run = Run(start=at(1).astimezone(UTC), end=at(3).astimezone(UTC), peak_w=30.0)
    assert unusual_use("AA", run) == Event(
        "unusual_use", "AA", run.start, {"peak_w": 30.0, "minutes": 120}
    )


def test_the_key_uses_the_local_day_the_run_started():
    # 01:00 Tuesday in Colombo is still Monday in UTC: the key must say Tuesday
    event = Event("unusual_use", "AA", at(25).astimezone(UTC), {})
    assert dedupe_key(event, tz="Asia/Colombo") == "AA|unusual_use|2026-11-03"


def test_a_site_wide_event_is_keyed_to_the_site():
    event = Event("power_cut", None, at(10).astimezone(UTC), {})
    assert dedupe_key(event, tz="Asia/Colombo") == "site|power_cut|2026-11-02"


# offline and power cut: from the logger's last rounds


def rounds(*answered):
    """One logger round a minute; each argument maps plug name -> did it answer."""
    return [(at(9, m), dict(a)) for m, a in enumerate(answered)]


MACS = {"Plug-1": "AA", "Plug-2": "BB"}
UP, DOWN = {"Plug-1": True, "Plug-2": True}, {"Plug-1": True, "Plug-2": False}
CUT = {"Plug-1": False, "Plug-2": False}


def test_three_failed_rounds_is_offline():
    found = offline_events(rounds(UP, DOWN, DOWN, DOWN), MACS)
    assert found == [Event("offline", "BB", at(9, 1), {"plug": "Plug-2", "rounds": 3})]


def test_two_failed_rounds_is_not_offline_yet():
    assert offline_events(rounds(UP, UP, DOWN, DOWN), MACS) == []


def test_every_plug_failing_is_one_power_cut_not_many_offline():
    assert offline_events(rounds(CUT, CUT, CUT), MACS) == [
        Event("power_cut", None, at(9, 0), {"plugs": 2})
    ]


def test_too_few_rounds_says_nothing():
    assert offline_events(rounds(DOWN, DOWN), MACS) == []


def test_a_plug_never_reached_has_no_mac_so_no_event():
    assert offline_events(rounds(DOWN, DOWN, DOWN), {"Plug-1": "AA"}) == []


# timer mismatch: the plug's timers changed since we saved them


def timer(slot, time="07:30"):
    return Timer(slot, True, 0, time, 0, "0111110", True, 1, 1)


def test_a_changed_timer_is_a_mismatch():
    saved, read = [timer(1), timer(2)], [timer(1, "08:00"), timer(2)]
    assert timer_mismatch("AA", saved, read, now=at(9)) == Event(
        "timer_mismatch", "AA", at(9), {"slots": "1"}
    )


def test_same_timers_are_no_mismatch():
    assert timer_mismatch("AA", [timer(1)], [timer(1)], now=at(9)) is None


def test_the_first_read_is_no_mismatch():  # nothing saved yet to compare with
    assert timer_mismatch("AA", [], [timer(1)], now=at(9)) is None
