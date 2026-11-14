from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from agentc.calc import Period, TimeOfUse, month_tou_lkr, tou_rate

COLOMBO = ZoneInfo("Asia/Colombo")
# Made-up round rates with Sri Lanka's three periods; off-peak runs past midnight to 05:30
TOU = TimeOfUse((Period("05:30", 30), Period("18:30", 60), Period("22:30", 20)), fixed=500)


def local(day, hhmm):
    """A Colombo time on 2026-11-<day>, as the UTC time the meter stores."""
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 11, day, h, m, tzinfo=COLOMBO).astimezone(UTC)


@pytest.mark.parametrize(
    ("hhmm", "rate"),
    [
        ("05:30", 30),  # a period starts on its first minute
        ("12:00", 30),
        ("18:29", 30),
        ("18:30", 60),
        ("23:00", 20),
        ("02:00", 20),  # before the first start: still last night's off-peak
    ],
)
def test_rate_by_time_of_day(hhmm, rate):
    assert tou_rate(TOU, local(3, hhmm), tz="Asia/Colombo") == rate


def test_month_bill_by_hand():
    counter = [
        (local(3, "12:00"), 1.0), (local(3, "12:01"), 1.5),  # 0.5 kWh in the day
        (local(3, "19:00"), 1.5), (local(3, "19:01"), 2.5),  # 1.0 kWh in the peak
        (local(4, "02:00"), 2.5), (local(4, "02:01"), 3.0),  # 0.5 kWh off-peak
    ]  # fmt: skip
    assert month_tou_lkr(counter, TOU, tz="Asia/Colombo") == pytest.approx(
        500 + 0.5 * 30 + 1.0 * 60 + 0.5 * 20
    )


def test_the_order_periods_are_listed_in_does_not_matter():
    shuffled = TimeOfUse(TOU.periods[::-1], fixed=500)
    assert tou_rate(shuffled, local(3, "02:00"), tz="Asia/Colombo") == 20
