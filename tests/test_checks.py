"""Checks shared by every agent: grounded numbers and the owner's latest-off rules."""

import pytest

from agentc.checks import breaks, made_up
from agentc.config import Rule

FACTS = """Desk 2 was on at about 5 W from 01:00 to 07:59 on Tue 03 Nov.
From Tue 01:00 to Tue 07:00: 0.031 kWh, LKR 3.2, 0.013 kgCO2e"""


def test_numbers_in_the_facts_pass():
    assert made_up("About 5 W from 01:00 to 07:59, LKR 3.2", FACTS) == []


def test_a_made_up_number_is_found():
    assert made_up("Off at 01:00 saves LKR 40 a week.", FACTS) == ["40"]


def test_numbers_match_however_they_are_written():
    assert made_up("on at 7:00, LKR 3.20, on the 3rd, 0.0130 kg", FACTS) == []


def test_a_wrong_time_is_a_made_up_number():
    assert made_up("off at 02:00", FACTS) == ["02:00"]


@pytest.mark.parametrize(
    ("off", "until", "broken"),
    [
        ("01:30", "02:00", True),  # off 30 minutes before the owner's time
        ("23:30", "01:00", True),  # off before midnight, the owner's time after it
        ("05:30", "06:30", True),  # either side of the old 06:00 day start
        ("08:00", "01:00", False),  # off the next morning: after the owner's time
        ("01:00", "01:00", False),  # off at the owner's time: kept
    ],
)
def test_a_latest_off_rule_is_read_on_the_clock(off, until, broken):
    assert breaks(off, Rule("latest_off", until, hard=True, text="")) is broken


def test_rule_kinds_not_built_yet_never_break():
    assert breaks("01:30", Rule("must_run", "02:00", hard=True, text="")) is False
