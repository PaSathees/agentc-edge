"""The owner loop's rules: quiet hours and the scope of a habit from a tap (FR-36, FR-33)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agentc import house, owner

COLOMBO = ZoneInfo("Asia/Colombo")


def at(day, h, m=0):
    return datetime(2026, 11, day, h, m, tzinfo=COLOMBO)  # 2 Nov is a Monday


@pytest.mark.parametrize(
    ("local", "night"),
    [(at(2, 23, 29), False), (at(2, 23, 30), True), (at(3, 6, 59), True), (at(3, 7), False)],
)
def test_quiet_hours_are_23_30_to_07_00(local, night):
    assert owner.quiet(local) is night


@pytest.mark.parametrize(
    ("local", "watts", "due"),
    [
        (at(2, 2, 10), 5.0, at(2, 7)),  # small, after midnight: this morning
        (at(2, 23, 45), 5.0, at(3, 7)),  # small, before midnight: the next morning
        (at(2, 2, 10), 50.0, at(2, 7)),  # 50 W is not more than 50 W
        (at(2, 2, 10), 51.0, at(2, 2, 10)),  # big: at once
        (at(2, 9), 5.0, at(2, 9)),  # by day: at once
    ],
)
def test_when_a_question_reaches_the_owner(local, watts, due):
    assert owner.send_at(local, watts, night_wake_w=50) == due


@pytest.mark.parametrize(
    ("start", "text", "window"),
    [
        (at(2, 1, 37), "Mondays, 01:00 to 07:00", ("0100000", "01:00", "07:00")),
        (at(2, 23, 40), "Mondays, 23:00 to 07:00", ("0100000", "23:00", "07:00")),
        (at(8, 14, 20), "Sundays, 14:00 to 17:00", ("1000000", "14:00", "17:00")),
    ],
)  # fmt: skip
def test_a_that_is_normal_tap_becomes_a_habit_with_a_scope(start, text, window):
    h = owner.habit("Desk 2", start)
    assert (h.text, (h.days, h.start, h.end), h.source) == (f"Desk 2 is normal on {text}", window,
                                                            "tap")  # fmt: skip
    assert house.covers(h, start)  # the signal it came from is inside it
