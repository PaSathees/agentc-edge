from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agentc.calc import Band, Factor, Period, Schedule, TimeOfUse
from agentc.devices.base import Timer
from agentc.options import (
    Change,
    Gap,
    Money,
    clock,
    gap,
    lkr,
    minutes,
    of,
    offer,
    saved,
    use,
    window,
)
from agentc.policy import Policy
from agentc.watcher import Run, Usual

COLOMBO = ZoneInfo("Asia/Colombo")
START = datetime(2026, 11, 2, 6, tzinfo=COLOMBO)  # Monday 06:00: day 0 of 14
OFF = {h: Usual(watts=0.0, on_share=0.0) for h in range(168)}
FREE = list(range(9, 17))
TARIFF = (Schedule(None, (Band(60, 30), Band(None, 50))),)  # every saved unit at 50 LKR
MONEY = Money(TARIFF, month_kwh=100, levy=0.0, factor=Factor("f", 0.5, "Example", "2025-01-01"))


def later(hhmm):
    """How long after the 06:00 START the clock next reads hhmm: 02:00 is 20 hours."""
    return timedelta(minutes=(of(hhmm) - of("06:00")) % (24 * 60))


def desk(waste=(0, 1)):
    """14 days of Desk 2, one reading a minute, like EV-01.

    In use at 30 W from 07:45 to 08:45 and from 19:00 to 00:50. On the nights of
    the days in `waste`: used at 01:00 for 10 minutes, then left on at 5 W until 05:59.
    """
    rows = []
    for n in range(14 * 24 * 60):
        local = START + timedelta(minutes=n)
        m = minutes(local)
        w = 0.0
        if of("07:45") <= m <= of("08:45") or m >= of("19:00") or m <= of("00:50"):
            w = 30.0
        elif n // (24 * 60) in waste and of("01:00") <= m < of("06:00"):
            w = 30.0 if m < of("01:10") else 5.0
        rows.append((local.astimezone(UTC), w))
    return rows


def night(day):
    """The waste on the night of `day`, as the Watcher would find it."""
    begin = START + timedelta(days=day) + later("01:00")
    end = begin + timedelta(hours=4, minutes=59)  # 05:59
    return Run(begin.astimezone(UTC), end.astimezone(UTC), 30.0)


def fix(run, readings, *, timers=(), slots=FREE, money=MONEY):
    g = gap(use(readings, OFF, tz="Asia/Colombo"), run, tz="Asia/Colombo")
    assert g is not None
    return offer(
        run, readings, g, tz="Asia/Colombo", days=14, timers=list(timers), slots=slots,
        money=money,
    )  # fmt: skip


def timer(slot, time, action, enable=True):
    return Timer(slot, enable, 0, time, 0, "1111111", True, 1, action)


# times on the clock: no day start


def test_minutes_count_from_midnight():
    assert (minutes(datetime(2026, 11, 2, 0, 52)), of("07:45"), clock(of("01:30"))) == (
        52, 465, "01:30"
    )  # fmt: skip
    assert clock(24 * 60 + 90) == "01:30"


# use: the minutes of the day the plug is used


def at(day, hhmm, n, watts):
    """n readings, one a minute, from the clock's next hhmm after day `day` starts, at `watts`."""
    begin = START + timedelta(days=day) + later(hhmm)
    return [((begin + timedelta(minutes=k)).astimezone(UTC), watts) for k in range(n)]


def span(start, end):
    """The clock minutes from start to end, both in."""
    return frozenset(range(of(start), of(end) + 1))


def test_20_minutes_or_more_is_use():
    assert use(at(0, "02:00", 21, 30.0), OFF, tz="Asia/Colombo") == span("02:00", "02:20")


def test_the_policy_sets_what_use_is():
    readings = at(0, "02:00", 21, 10.0)  # 10 W for 20 minutes
    assert use(readings, OFF, tz="Asia/Colombo", policy=Policy(use_w=11)) == frozenset()
    assert use(readings, OFF, tz="Asia/Colombo", policy=Policy(use_min=21)) == frozenset()


def test_exactly_10_w_above_standby_is_use():
    assert use(at(0, "02:00", 21, 10.0), OFF, tz="Asia/Colombo") == span("02:00", "02:20")


def test_a_gap_over_5_minutes_starts_a_new_use():
    readings = at(0, "02:00", 11, 30.0) + at(0, "02:16", 21, 30.0)  # 10 min, 6 min gap, 20 min
    assert use(readings, OFF, tz="Asia/Colombo") == span("02:16", "02:36")


def test_standby_is_not_use():
    standby = {h: Usual(watts=8.0, on_share=0.0) for h in range(168)}  # 8 W all the time
    readings = at(0, "02:00", 30, 15.0)  # 7 W above standby: under 10 W more
    assert use(readings, standby, tz="Asia/Colombo") == frozenset()


# gap: the stretch with no use that the run falls in


def run_at(start, end):
    begin = START + later(start)
    return Run(begin.astimezone(UTC), (begin + (later(end) - later(start))).astimezone(UTC), 30.0)


def desk_use():
    return use(desk(), OFF, tz="Asia/Colombo")


def test_night_waste_gets_the_night_gap():
    # the waste nights start with 10 minutes at 30 W, then 5 W left on: neither is use
    assert gap(desk_use(), night(0), tz="Asia/Colombo") == Gap(of("00:50"), of("07:45"))


def test_daytime_waste_gets_the_daytime_gap():
    assert gap(desk_use(), run_at("12:00", "14:00"), tz="Asia/Colombo") == Gap(
        of("08:45"), of("19:00")
    )  # fmt: skip


def test_a_run_that_starts_in_use_takes_the_gap_after_it():
    assert gap(desk_use(), run_at("00:00", "03:00"), tz="Asia/Colombo") == Gap(
        of("00:50"), of("07:45")
    )  # fmt: skip


def test_a_gap_over_midnight():
    days = [(t, 30.0 if of("06:00") <= minutes(t.astimezone(COLOMBO)) < of("22:00") else 0.0)
            for t, _ in desk()]  # fmt: skip
    used = use(days, OFF, tz="Asia/Colombo")
    assert gap(used, run_at("23:00", "23:59"), tz="Asia/Colombo") == Gap(of("21:59"), of("06:00"))


def test_no_gap_when_the_run_is_all_in_use_or_there_is_no_use():
    assert gap(desk_use(), run_at("20:00", "21:00"), tz="Asia/Colombo") is None
    assert gap(frozenset(), night(0), tz="Asia/Colombo") is None


# window: the off and on times


def test_off_half_an_hour_after_the_latest_use_on_before_the_first():
    # EV-01: in use until 00:50, first use 07:45
    assert window(Gap(latest=of("00:50"), first=of("07:45"))) == (of("01:30"), of("07:00"))


def test_a_use_ending_on_the_half_hour_is_not_rounded_up_again():
    assert window(Gap(latest=of("01:00"), first=of("09:00"))) == (of("01:30"), of("08:30"))


def test_the_policy_sets_the_step_and_the_margin():
    g = Gap(latest=of("00:52"), first=of("07:45"))
    assert window(g, Policy(step_min=15, margin_min=15)) == (of("01:15"), of("07:30"))


def test_no_fix_keeps_a_plug_off_longer_than_max_off_h():  # the 18 h fix from thin data
    g = Gap(latest=of("20:14"), first=of("15:40"))
    assert window(g) is None
    assert window(g, Policy(max_off_h=18)) == (of("21:00"), of("15:00"))


@pytest.mark.parametrize(("latest", "first"), [("05:50", "06:10"), ("01:00", "01:30")])
def test_no_gap_long_enough_means_no_window(latest, first):
    no_cap = Policy(max_off_h=24)  # so only the gap's own length decides
    assert window(Gap(of(latest), of(first)), no_cap) is None


def test_a_window_over_midnight():
    assert window(Gap(latest=of("22:00"), first=of("06:00"))) == (of("22:30"), of("05:30"))
    assert window(Gap(latest=of("23:45"), first=of("06:00"))) == (of("00:30"), of("05:30"))


# offer: the fixes for one event


def test_ev_01_desk_2_off_at_01_30_on_at_07_00():
    a, b = fix(night(0), desk())
    assert a.id == "A"
    assert a.changes == (Change(9, "off", "01:30", "1111111"), Change(10, "on", "07:00", "1111111"))
    assert b.changes[0].days == "0111110"  # the run was on a weekday night
    # 2 nights x 5 W x 01:30 to 05:59 (270 min) = 0.045 kWh in 14 days = 0.0225 a week
    assert a.kwh == pytest.approx(0.0225)
    assert a.lkr == pytest.approx(0.0225 * 50)  # top band
    assert a.kg == pytest.approx(0.0225 * 0.5)


def test_b_counts_only_days_like_the_run_and_is_dropped_if_it_saves_nothing():
    weekend = desk(waste=(5,))  # Saturday night: the off timer fires on Sunday
    [a] = fix(night(0), weekend)  # the event was on a weekday
    assert a.id == "A"


def test_a_weekend_run_gets_b_on_weekends():
    _, b = fix(night(5), desk(waste=(5,)))
    assert b.changes[0].days == "1000001"  # Sunday and Saturday


def test_a_plug_that_already_turns_on_in_the_morning_gets_only_the_off_timer():
    [a, _] = fix(night(0), desk(), timers=[timer(1, "06:30", action=1)])
    assert a.changes == (Change(9, "off", "01:30", "1111111"),)


def test_slots_in_use_are_skipped_but_disabled_ones_are_free():
    timers = [timer(9, "12:00", action=0), timer(10, "12:00", action=0, enable=False)]
    [a, _] = fix(night(0), desk(), timers=timers)
    assert [c.slot for c in a.changes] == [10, 11]


def test_an_off_timer_or_a_disabled_one_is_not_a_morning_on():
    timers = [timer(1, "06:30", action=0), timer(2, "06:30", action=1, enable=False)]
    [a, _] = fix(night(0), desk(), timers=timers)
    assert len(a.changes) == 2


def test_no_free_slot_means_no_fix():
    assert fix(night(0), desk(), slots=[9]) == []  # needs 2: off and on


def test_daytime_waste_gets_a_daytime_fix():
    noon = run_at("12:00", "14:00")
    readings = [(t, 5.0 if noon.start <= t <= noon.end else w) for t, w in desk()]
    a, _ = fix(noon, readings)
    assert [(c.action, c.time) for c in a.changes] == [("off", "09:30"), ("on", "18:30")]


def test_a_run_after_one_nights_window_takes_the_next_nights():
    # Friday 08:00 to Saturday 03:00: the Friday 01:30 window had closed; Saturday's cuts it
    friday = START + timedelta(days=4)
    run = Run((friday + later("08:00")).astimezone(UTC), (friday + later("03:00")).astimezone(UTC),
              30.0)  # fmt: skip
    readings = desk(waste=(4,))  # left on into Saturday morning
    _, b = offer(run, readings, Gap(of("00:50"), of("07:45")), tz="Asia/Colombo", days=14,
                 timers=[], slots=FREE, money=MONEY)  # fmt: skip
    assert b.changes[0].days == "1000001"  # weekends: Saturday's window


def test_a_run_the_timers_would_not_cut_has_no_fix():
    assert fix(run_at("00:52", "01:20"), desk()) == []  # in the gap, but over before 01:30


def test_used_all_day_has_no_gap_so_no_fix():
    busy = [(at, 30.0) for at, _ in desk()]
    assert gap(use(busy, OFF, tz="Asia/Colombo"), night(0), tz="Asia/Colombo") is None


# saved and lkr


def test_a_gap_in_readings_counts_5_minutes_at_most():
    at = START + later("02:00")
    readings = [(at.astimezone(UTC), 60.0), ((at + timedelta(hours=1)).astimezone(UTC), 60.0)]
    assert saved(readings, of("01:30"), of("07:00"), "1111111", COLOMBO) == pytest.approx(0.005)


def test_time_of_use_is_priced_at_the_rate_when_the_timer_fires():
    tou = TimeOfUse((Period("05:30", 30.0), Period("22:30", 20.0)))
    money = Money(tou, month_kwh=100, levy=0.025, factor=MONEY.factor)
    assert lkr(1.0, money, at=of("01:30")) == pytest.approx(20 / 0.975)  # night rate


def test_a_block_saving_includes_the_levy():
    money = Money(TARIFF, month_kwh=100, levy=0.025, factor=MONEY.factor)
    assert lkr(0.7, money, at=0) == pytest.approx(0.7 * 50 / 0.975)
