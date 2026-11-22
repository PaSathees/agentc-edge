from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agentc import house
from agentc.config import Device, Rule
from agentc.devices.base import Automations, PlugRule, Timer
from agentc.house import Card, Habit

COLOMBO = ZoneInfo("Asia/Colombo")
MONDAY = "0100000"  # Sunday first, as Tasmota
DESK = Device("Desk 2")
CARD = Card("computer", "study", "laptop charger, monitor, speaker", "work desk",
            why={3: "on for the working day"})  # fmt: skip
LATE = Habit("Desk 2 runs late on Mondays", MONDAY, "23:00", "02:00", "tap")


def at(day, hhmm):
    """Local time on day `day` of November 2026 (2 Nov is a Monday)."""
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 11, day, h, m, tzinfo=COLOMBO)


# a habit covers a window on some days; a window past midnight belongs to the day it starts


@pytest.mark.parametrize(
    ("when", "covered"),
    [
        (at(2, "23:30"), True),  # Monday night
        (at(3, "01:30"), True),  # still Monday night, after midnight
        (at(3, "02:00"), False),  # the window ends at 02:00
        (at(3, "23:30"), False),  # Tuesday night: not a listed day
        (at(2, "22:59"), False),  # before it starts
    ],
)
def test_a_habit_covers_its_window(when, covered):
    assert house.covers(LATE, when) is covered


def test_a_daytime_habit_covers_only_its_hours():
    noon = Habit("TV on at lunch", "1111111", "12:00", "14:00", "chat")
    assert (house.covers(noon, at(4, "13:00")), house.covers(noon, at(4, "14:30"))) == (True, False)


# the card: the house model for one device, in the plain words the agent and the owner read


def timer(slot, action, time, days="0111110"):
    return Timer(slot, True, 0, time, 0, days, True, 1, action)


def test_a_card_says_what_the_device_is_its_timers_and_habits():
    text = house.card(DESK, CARD, timers=[timer(3, 1, "07:00")], habits=[LATE])
    assert text == "\n".join([
        "Desk 2: computer. Where: study. Plugged in: laptop charger, monitor, speaker.",
        "Notes: work desk",
        "Access: advice. Must run: no.",
        "Timer 3: On at 07:00, Monday to Friday. Why: on for the working day",
        "Habit: Desk 2 runs late on Mondays (Monday, 23:00 to 02:00)",
    ])  # fmt: skip


def test_a_card_with_nothing_known_says_so():
    text = house.card(Device("Plug-9"), Card(), timers=[], habits=[])
    assert text == "\n".join([
        "Plug-9: nothing known yet. Ask the owner what is plugged in.",
        "Access: advice. Must run: no.",
        "Timers: none.",
        "Habits: none yet.",
    ])  # fmt: skip


def test_a_card_shows_rules_and_a_timer_with_no_reason():
    rule = Rule("latest_off", "01:00", hard=False, text="Desk 2 may run until 01:00")
    desk = Device("Desk 2", rules=(rule,))
    text = house.card(desk, Card("computer"), timers=[timer(9, 0, "01:30", "1111111")], habits=[])
    assert "Rule (soft): Desk 2 may run until 01:00" in text
    assert "Timer 9: Off at 01:30, every day. Why: not known" in text


def test_a_card_skips_timers_that_are_not_set():
    unset = Timer(4, False, 0, "00:00", 0, "0000000", True, 1, 0)
    assert "Timers: none." in house.card(DESK, CARD, timers=[unset], habits=[])


# must run: the owner's word, else the kind


@pytest.mark.parametrize(
    ("card", "must"),
    [(Card("fridge"), True), (Card("router"), True), (Card("fan"), False),
     (Card("fridge", must_run=False), False), (Card("fan", must_run=True), True)],
)  # fmt: skip
def test_a_fridge_or_router_must_run_unless_the_owner_says(card, must):
    assert house.runs_always(card) is must


# the owner changes a card with key=value, as typed after `agentc house Plug-6`


def test_a_card_is_changed_key_by_key():
    new = house.updated(CARD, ["room=bedroom", "why.9=off at night", "must_run=yes"])
    assert (new.room, new.why, new.must_run, new.kind) == (
        "bedroom",
        {3: "on for the working day", 9: "off at night"},
        True,
        "computer",
    )


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ("kind=telly", "kind must be one of charger, computer, media"),
        ("why.17=x", "why.17: timer slots are 1 to 16"),
        ("why.a=x", "why.a: timer slots are 1 to 16"),
        ("must_run=maybe", "must_run is yes or no"),
        ("colour=red", "unknown key 'colour'"),
        ("room", "write it as key=value"),
    ],
)
def test_a_wrong_change_fails_before_anything_is_saved(change, error):
    with pytest.raises(ValueError, match=error):
        house.updated(CARD, [change])


def test_a_card_shows_the_plugs_own_auto_off_and_rules():
    auto = Automations(14400, (PlugRule(1, True, "ON Time#Initialized DO Power 1 ENDON"),
                               PlugRule(2, False, "ON x DO y ENDON")))  # fmt: skip
    text = house.card(DESK, CARD, timers=[], habits=[], automations=auto)
    assert "Auto-off: after 4 h, set on the plug" in text
    assert "Plug rule 1 (on): ON Time#Initialized DO Power 1 ENDON" in text
    assert "Plug rule 2 (off): ON x DO y ENDON" in text


@pytest.mark.parametrize(("seconds", "words"), [(14400, "4 h"), (36000, "10 h"), (90, "2 min"),
                                                (5.0, "5 s"), (5400, "1 h 30 min")])  # fmt: skip
def test_auto_off_in_plain_words(seconds, words):
    text = house.card(DESK, CARD, timers=[], habits=[], automations=Automations(seconds, ()))
    assert f"Auto-off: after {words}, set on the plug" in text
