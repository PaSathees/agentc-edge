from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from agentc.calc import ProfileHour, build_profile

COLOMBO = ZoneInfo("Asia/Colombo")
MONDAY = datetime(2026, 11, 2, tzinfo=COLOMBO)  # Monday 00:00 local


def week_of_minutes(power, days=7, start=MONDAY):
    """One reading a minute for `days` days; power(local_time) gives the watts."""
    out = []
    for m in range(days * 24 * 60):
        local = start + timedelta(minutes=m)
        out.append((local.astimezone(UTC), power(local)))
    return out


def desk(local):
    """A desk left on from 01:00 to 01:59 every night (10 W), 1 W standby otherwise."""
    return 10.0 if local.hour == 1 else 1.0


def by_hour(profile):
    return {p.hour: p for p in profile}


def test_hand_worked_week():
    p = by_hour(build_profile(week_of_minutes(desk), tz="Asia/Colombo"))
    assert len(p) == 168
    assert p[1] == ProfileHour(hour=1, median_w=10.0, on_share=1.0, days_used=1)  # Mon 01:00
    assert p[2] == ProfileHour(hour=2, median_w=1.0, on_share=0.0, days_used=1)  # Mon 02:00
    assert p[24 + 1].median_w == 10.0  # Tuesday 01:00 is hour 25


def test_half_on_half_off_hour():
    def half(local):  # first 30 min of every hour at 10 W, then 0 W
        return 10.0 if local.minute < 30 else 0.0

    hour0 = by_hour(build_profile(week_of_minutes(half), tz="Asia/Colombo"))[0]
    assert (hour0.median_w, hour0.on_share) == (5.0, 0.5)  # median of 30 zeros and 30 tens


def test_two_weeks_count_two_days_per_hour():
    p = by_hour(build_profile(week_of_minutes(desk, days=14), tz="Asia/Colombo"))
    assert p[1].days_used == 2


def test_hours_use_local_time_not_utc():
    # 19:30 UTC on Monday is 01:00 on Tuesday in Colombo: it must land in hour 25, not 19
    p = by_hour(build_profile(week_of_minutes(desk), tz="Asia/Colombo"))
    assert p[19].median_w == 1.0 and p[25].median_w == 10.0


def test_under_seven_days_gives_no_profile():
    assert build_profile(week_of_minutes(desk, days=6), tz="Asia/Colombo") is None
