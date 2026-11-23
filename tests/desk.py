"""Test data: 14 days of a desk like Desk 2, with nights it was left on (like EV-01)."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from agentc.watcher import Run

COLOMBO = ZoneInfo("Asia/Colombo")
START = datetime(2026, 11, 2, 6, tzinfo=COLOMBO)  # Monday 06:00: day 0 of 14
DAY = 24 * 60


def of(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def desk(waste=(0, 1)):
    """One reading a minute. In use at 30 W from 07:45 to 08:45 and from 19:00 to 00:50.

    On the nights of the days in `waste`: used at 01:00 for 10 minutes, then left on at
    5 W until 05:59.
    """
    rows = []
    for n in range(14 * DAY):
        local = START + timedelta(minutes=n)
        m = local.hour * 60 + local.minute
        w = 0.0
        if of("07:45") <= m <= of("08:45") or m >= of("19:00") or m <= of("00:50"):
            w = 30.0
        elif n // DAY in waste and of("01:00") <= m < of("06:00"):
            w = 30.0 if m < of("01:10") else 5.0
        rows.append((local.astimezone(UTC), w))
    return rows


def night(day):
    """The waste on the night of `day` (01:00 the next morning to 05:59)."""
    begin = START + timedelta(days=day, hours=19)  # 06:00 + 19 h = 01:00
    end = begin + timedelta(hours=4, minutes=59)
    return Run(begin.astimezone(UTC), end.astimezone(UTC), 30.0)
