from datetime import UTC, datetime

import pytest

from agentc.checks import Draft, Rule, Verdict, check, made_up
from agentc.options import Change, Option, Proposal

# Desk 2, as in EV-01
A = Option("A", (Change(9, "off", "01:30", "1111111"), Change(10, "on", "07:00", "1111111")),
           kwh=0.031, lkr=3.2, kg=0.013)  # fmt: skip
B = Option("B", (Change(9, "off", "01:30", "0111110"), Change(10, "on", "07:00", "0111110")),
           kwh=0.031, lkr=3.2, kg=0.013)  # fmt: skip
FACTS = """Desk 2 was on at about 5 W from 01:00 to 07:59 on Tue 03 Nov.
Last use in 14 days: 00:52. First use: 07:45.
A: off at 01:30, on at 07:00, every day. Saves 0.031 kWh, LKR 3.2, 0.013 kgCO2e a week.
B: the same, weekdays only. Saves 0.031 kWh, LKR 3.2, 0.013 kgCO2e a week."""
GOOD = Draft("A", "Desk 2 was left on at 5 W. Off at 01:30 and on at 7:00 saves LKR 3.2 a week.")


def verdict(draft=GOOD, *, access="advice", slots=tuple(range(9, 17)), rules=(), rejected=()):
    return check(
        draft, options=[A, B], facts=FACTS, access=access, slots=list(slots), rules=list(rules),
        rejected=list(rejected),
    )  # fmt: skip


def turned_down(*changes):
    """A proposal for these changes that the owner rejected."""
    return Proposal("AA", "Desk 2", changes, "", 0.0, 0.0, 0.0, "", "responder",
                    datetime(2026, 11, 4, tzinfo=UTC))  # fmt: skip


def test_a_good_draft_passes():
    assert verdict() == Verdict(None)


# 1. format


def test_an_option_that_was_not_offered_is_blocked():
    assert verdict(Draft("C", "Off at 01:30.")).blocked == "format: no option 'C'"


@pytest.mark.parametrize("reason", ["", "   ", "One. Two. Three."])
def test_the_reason_must_be_1_or_2_sentences(reason):
    assert verdict(Draft("A", reason)).blocked == "format: the reason must be 1 or 2 sentences"


# 2. numbers (EV-04)


def test_a_made_up_number_is_blocked():
    draft = Draft("A", "Off at 01:30 saves LKR 40 a week.")
    assert verdict(draft).blocked == "numbers: not in the facts: 40"


def test_numbers_match_however_they_are_written():
    assert made_up("on at 7:00, LKR 3.20, on the 3rd, 0.0130 kg", FACTS) == []


def test_a_wrong_time_is_a_made_up_number():
    assert made_up("off at 02:00", FACTS) == ["02:00"]


def test_no_action_needs_only_format_and_numbers():
    none = Draft("none", "Desk 2 is often used until 00:52.")
    assert verdict(none, access="none", slots=[]) == Verdict(None)


# 3. access (EV-05)


def test_a_device_with_access_none_is_never_changed():
    assert verdict(access="none").blocked == "access: this device is never changed"


# 4. slots


def test_only_allowed_timer_slots():
    assert verdict(slots=[9]).blocked == "slots: timer 10 is not one the system may use"


# 5 and 7. rules


def test_a_hard_rule_blocks():
    late = Rule("latest_off", "02:00", hard=True, text="Desk 2 may run until 02:00")
    assert verdict(rules=[late]).blocked == "rule: Desk 2 may run until 02:00"


def test_a_soft_rule_only_warns():
    late = Rule("latest_off", "02:00", hard=False, text="Desk 2 may run until 02:00")
    assert verdict(rules=[late]) == Verdict(None, ("Desk 2 may run until 02:00",))


def test_an_off_after_the_latest_time_keeps_the_rule():
    early = Rule("latest_off", "01:00", hard=True, text="Desk 2 may run until 01:00")
    assert verdict(rules=[early]) == Verdict(None)


def test_rule_kinds_not_built_yet_are_ignored():
    other = Rule("must_run", "02:00", hard=True, text="Router always on")
    assert verdict(rules=[other]).blocked is None


# 6. no repeats (EV-02)


def test_the_same_change_rejected_lately_is_blocked():
    old = turned_down(Change(11, "off", "01:45", "1111111"))  # 15 minutes apart
    assert verdict(rejected=[old]).blocked == (
        "repeat: the owner rejected the same change in the last 14 days"
    )


def test_times_either_side_of_midnight_are_close():
    old = turned_down(Change(9, "on", "23:50", "1111111"))
    draft = Draft("A", "Off at 01:30.")
    near = Option("A", (Change(9, "on", "00:10", "1111111"),), 0.031, 3.2, 0.013)
    result = check(draft, options=[near], facts=FACTS, access="advice", slots=[9], rules=[],
                   rejected=[old])  # fmt: skip
    assert result.blocked is not None


@pytest.mark.parametrize(
    "change",
    [
        Change(9, "off", "02:01", "1111111"),  # 31 minutes apart
        Change(9, "on", "01:30", "1111111"),  # on, not off
        Change(9, "off", "01:30", "1000001"),  # weekends: no day in common with B
    ],
)
def test_a_different_change_is_not_a_repeat(change):
    weekdays = Draft("B", "Off at 01:30 and on at 07:00 on weekdays.")
    assert verdict(weekdays, rejected=[turned_down(change)]).blocked is None


def test_the_same_times_on_fewer_days_is_a_repeat():  # EV-02
    old = turned_down(Change(9, "off", "01:30", "1111111"))
    weekdays = Draft("B", "Off at 01:30 and on at 07:00 on weekdays.")
    assert verdict(weekdays, rejected=[old]).blocked == (
        "repeat: the owner rejected the same change in the last 14 days"
    )
