from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agentc.calc import ProfileHour
from agentc.devices.base import Timer
from agentc.policy import DEFAULT, Policy
from agentc.watcher import (
    Event,
    Run,
    Usual,
    automation_events,
    dedupe_key,
    is_unusual,
    logger_stopped,
    offline_events,
    run_event,
    timer_mismatch,
    unusual_runs,
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
    assert not is_unusual(25.0, Usual(10.0, 0.0), Policy(factor=3.0))


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


def test_a_plug_on_often_at_that_hour_needs_a_higher_rarely_on():
    assert not is_unusual(30.0, Usual(0.0, 0.3))
    assert is_unusual(30.0, Usual(0.0, 0.3), Policy(rarely_on=0.5))


@pytest.mark.parametrize(
    ("watts", "n", "policy", "count"),
    [
        (15.0, 61, DEFAULT, ["unusual_use"]),  # 15 W more for an hour: the 10 W rule
        (15.0, 61, Policy(more_w=20), ["left_on"]),  # not big enough: a small load left on
        (15.0, 61, Policy(run_min=61), ["left_on"]),
        (5.0, 31, DEFAULT, ["left_on"]),  # 5 W more for 30 min: the small-load rule
        (5.0, 31, Policy(small_w=6), []),
        (5.0, 31, Policy(left_on_min=31), []),
    ],
)
def test_the_policy_sets_how_big_and_how_long(watts, n, policy, count):
    found = unusual_runs(minutes(at(1), n, watts), OFF, tz="Asia/Colombo", policy=policy)
    assert [r.kind for r in found] == count


def test_a_normal_reading_ends_the_run():
    readings = minutes(at(1), 20, 30.0) + minutes(at(1, 20), 1, 0.0) + minutes(at(1, 21), 20, 30.0)
    assert runs(readings) == []


# events: what gets saved, and the key that stops repeats


def test_a_run_becomes_an_event_of_its_kind():
    run = Run(start=at(1).astimezone(UTC), end=at(3).astimezone(UTC), peak_w=30.0)
    assert run_event("AA", run) == Event(
        "unusual_use", "AA", run.start, {"peak_w": 30.0, "minutes": 120}
    )
    left = Run(run.start, run.end, 5.0, kind="left_on")
    assert run_event("AA", left).kind == "left_on"


def test_a_small_load_is_left_on_and_a_big_one_is_unusual_use():  # FR-09 v2
    (small,) = runs(minutes(at(1), 31, 5.0))
    (big,) = runs(minutes(at(1), 31, 30.0))
    assert (small.kind, big.kind) == ("left_on", "unusual_use")


def test_the_desk_2_night_is_left_on_by_02_10():  # 30 W 01:37 to 01:51, then 5 W
    readings = minutes(at(1, 37), 14, 30.0) + minutes(at(1, 51), 60, 5.0)
    early = [r for r in readings if r[0] <= at(2, 6).astimezone(UTC)]
    late = [r for r in readings if r[0] <= at(2, 10).astimezone(UTC)]
    assert (runs(early), [r.kind for r in runs(late)]) == ([], ["left_on"])


def test_the_key_uses_the_local_day_the_run_started():
    # 01:00 Tuesday in Colombo is still Monday in UTC: the key must say Tuesday
    event = Event("unusual_use", "AA", at(25).astimezone(UTC), {})
    assert dedupe_key(event, tz="Asia/Colombo") == "AA|unusual_use|2026-11-03"


def test_a_site_wide_event_is_keyed_to_the_site():
    event = Event("no_contact", None, at(10).astimezone(UTC), {})
    assert dedupe_key(event, tz="Asia/Colombo") == "site|no_contact|2026-11-02"


# offline and lost contact: from the logger's last rounds


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


def test_every_plug_failing_is_one_lost_contact_not_many_offline():
    assert offline_events(rounds(CUT, CUT, CUT), MACS) == [
        Event("no_contact", None, at(9, 0), {"plugs": 2})
    ]


def test_a_short_blackout_is_lost_contact_even_if_some_plugs_are_back():
    # all plugs failed, then Plug-1 came back first: still one power cut, no offline events
    assert offline_events(rounds(CUT, CUT, DOWN), MACS) == [
        Event("no_contact", None, at(9, 0), {"plugs": 2})
    ]


def test_a_one_minute_blackout_is_only_a_wifi_blip():
    assert offline_events(rounds(UP, CUT, UP), MACS) == []


def test_a_blackout_anywhere_since_the_last_pass_is_found():
    found = offline_events(rounds(UP, CUT, CUT, UP, UP, UP), MACS)
    assert found == [Event("no_contact", None, at(9, 1), {"plugs": 2})]


def test_too_few_rounds_says_nothing():
    assert offline_events(rounds(DOWN, DOWN), MACS) == []


def test_a_plug_never_reached_has_no_mac_so_no_event():
    assert offline_events(rounds(DOWN, DOWN, DOWN), {"Plug-1": "AA"}) == []


# the logger itself: no reading at all for 5 minutes (heartbeat)


def test_no_reading_for_5_minutes_means_the_logger_stopped():
    found = logger_stopped(at(9, 0), now=at(9, 6))
    assert found == Event("logger_stopped", None, at(9, 0), {"minutes": 6})


def test_a_reading_4_minutes_ago_is_fine():
    assert logger_stopped(at(9, 0), now=at(9, 4)) is None


def test_a_logger_that_never_ran_says_nothing():
    assert logger_stopped(None, now=at(9, 0)) is None


def test_each_stop_of_the_logger_is_its_own_event_even_on_one_day():
    first = Event("logger_stopped", None, at(9, 0).astimezone(UTC), {})
    second = Event("logger_stopped", None, at(13, 30).astimezone(UTC), {})
    assert dedupe_key(first, tz="Asia/Colombo") != dedupe_key(second, tz="Asia/Colombo")


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


# a small load left on for hours: 3 W more for 3 hours


def test_a_small_load_left_on_30_minutes_is_a_run():
    readings = minutes(at(1), 31, 5.0)  # 01:00 to 01:30, 5 W
    assert unusual_runs(readings, OFF, tz="Asia/Colombo") == [
        Run(start=readings[0][0], end=readings[-1][0], peak_w=5.0, kind="left_on")
    ]


def test_a_small_load_for_under_30_minutes_is_not():
    assert unusual_runs(minutes(at(1), 30, 5.0), OFF, tz="Asia/Colombo") == []  # 29 min


def test_under_3_w_more_is_never_a_run():
    assert unusual_runs(minutes(at(1), 300, 2.5), OFF, tz="Asia/Colombo") == []


def test_a_big_rise_inside_a_long_small_one_is_one_run():
    # 5 W from 01:00 to 05:00, with 30 W from 02:00 to 02:40
    readings = minutes(at(1), 60, 5.0) + minutes(at(2), 40, 30.0) + minutes(at(2, 40), 141, 5.0)
    assert unusual_runs(readings, OFF, tz="Asia/Colombo") == [
        Run(start=readings[0][0], end=readings[-1][0], peak_w=30.0)
    ]


# automations: what the plug's own timers should have done


def on_at(slot, hhmm, days="1111111", action=1):
    return Timer(slot, True, 0, hhmm, 0, days, True, 1, action)


def auto(timers, readings, now, policy=DEFAULT):
    return automation_events("TV", timers, readings, OFF, now=now, tz="Asia/Colombo",
                             policy=policy)  # fmt: skip


def test_a_timer_that_switches_on_for_nothing_is_drift():  # FR-34: TV on at 06:00, idle
    found = auto([on_at(1, "06:00")], minutes(at(6), 4 * 60, 0.5), now=at(10))
    assert found == [Event("automation_drift", "TV", at(6).astimezone(UTC),
                           {"slot": 1, "time": "06:00", "hours": 3, "peak_w": 0.5})]  # fmt: skip


def test_use_soon_after_the_timer_is_no_drift():
    readings = minutes(at(6), 60, 0.5) + minutes(at(7), 3 * 60, 80.0)  # watched from 07:00
    assert auto([on_at(1, "06:00")], readings, now=at(10)) == []


def test_drift_waits_until_the_hours_have_passed():
    assert auto([on_at(1, "06:00")], minutes(at(6), 2 * 60, 0.5), now=at(8)) == []


def test_a_timer_not_set_for_that_day_is_not_checked():
    sunday_only = on_at(1, "06:00", days="1000000")  # MONDAY is a Monday
    assert auto([sunday_only], minutes(at(6), 4 * 60, 0.5), now=at(10)) == []


def test_drift_needs_readings_for_the_whole_time():  # no data is not "idle"
    assert auto([on_at(1, "06:00")], minutes(at(6), 30, 0.5), now=at(10)) == []


def test_a_timer_that_switches_off_but_power_stays_is_an_override():
    off = on_at(2, "01:00", action=0)
    found = auto([off], minutes(at(1), 60, 60.0), now=at(2))
    assert found == [Event("override", "TV", at(1).astimezone(UTC),
                           {"slot": 2, "time": "01:00", "peak_w": 60.0})]  # fmt: skip


def test_an_off_timer_that_worked_is_no_override():
    off = on_at(2, "01:00", action=0)
    readings = minutes(at(0), 60, 60.0) + minutes(at(1), 60, 0.0)
    assert auto([off], readings, now=at(2)) == []


def test_an_override_waits_until_its_minutes_have_passed():
    off = on_at(2, "01:00", action=0)
    assert auto([off], minutes(at(1), 20, 60.0), now=at(1, 20)) == []


def test_sunrise_timers_and_unset_timers_are_skipped():
    sunrise = Timer(1, True, 1, "00:00", 0, "1111111", True, 1, 1)
    unset = Timer(2, False, 0, "06:00", 0, "1111111", True, 1, 1)
    assert auto([sunrise, unset], minutes(at(6), 4 * 60, 0.5), now=at(10)) == []


def test_the_policy_sets_how_long_drift_and_override_wait():
    on = auto(
        [on_at(1, "06:00")], minutes(at(6), 4 * 60, 0.5), now=at(10), policy=Policy(drift_h=5)
    )
    off = auto([on_at(2, "01:00", action=0)], minutes(at(1), 60, 60.0), now=at(2),
               policy=Policy(override_min=90))  # fmt: skip
    assert (on, off) == ([], [])
