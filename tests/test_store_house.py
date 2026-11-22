from datetime import UTC, datetime, timedelta

import pytest

from agentc import store
from agentc.devices.base import Automations, PlugRule
from agentc.house import Card, Habit

NOW = datetime(2026, 11, 21, 7, 0, tzinfo=UTC)
LATE = Habit("Desk 2 runs late on Mondays", "0100000", "23:00", "02:00", "tap")


@pytest.fixture
def db(tmp_path):
    db = store.connect(tmp_path / "app.db")
    store.add_site(db, name="home", tz="Asia/Colombo")
    store.plug_seen(db, site_id=1, mac="AA", name="Plug-6", ip="192.0.2.1", timers_on=True, now=NOW)
    return db


def test_a_habit_is_saved_and_read_back(db):
    hid = store.add_habit(db, site_id=1, mac="AA", habit=LATE, now=NOW)
    assert store.habits(db, mac="AA") == [(hid, LATE)]


def test_an_ended_habit_is_no_longer_read(db):
    hid = store.add_habit(db, site_id=1, mac="AA", habit=LATE, now=NOW)
    store.end_habit(db, habit_id=hid, now=NOW + timedelta(days=30))
    assert store.habits(db, mac="AA") == []


def test_habits_belong_to_one_plug(db):
    store.plug_seen(db, site_id=1, mac="BB", name="Plug-2", ip="192.0.2.2", timers_on=True, now=NOW)
    store.add_habit(db, site_id=1, mac="BB", habit=LATE, now=NOW)
    assert store.habits(db, mac="AA") == []


def test_a_habit_comes_from_a_tap_chat_or_setup(db):
    with pytest.raises(ValueError, match="source"):
        store.add_habit(db, site_id=1, mac="AA", habit=Habit("x", "1111111", "00:00", "01:00",
                                                             "guess"), now=NOW)  # fmt: skip


# cards: one a plug


def test_a_plug_with_no_card_has_an_empty_one(db):
    assert store.load_card(db, mac="AA") == Card()


def test_a_card_is_saved_and_replaced(db):
    first = Card("computer", "study", "laptop charger", "", None, {3: "work"})
    store.save_card(db, site_id=1, mac="AA", card=first, now=NOW)
    assert store.load_card(db, mac="AA") == first
    second = Card("fridge", "kitchen", "", "", False, {})
    store.save_card(db, site_id=1, mac="AA", card=second, now=NOW)
    assert store.load_card(db, mac="AA") == second


# the plug's own automations, read with its timers


def test_a_plug_never_read_has_no_automations(db):
    assert store.load_automations(db, mac="AA") == Automations(None, ())


def test_automations_are_saved_and_replaced(db):
    first = Automations(14400, (PlugRule(1, True, "ON Time#Initialized DO Power 1 ENDON"),))
    store.save_automations(db, site_id=1, mac="AA", automations=first, now=NOW)
    assert store.load_automations(db, mac="AA") == first
    store.save_automations(db, site_id=1, mac="AA", automations=Automations(None, ()), now=NOW)
    assert store.load_automations(db, mac="AA") == Automations(None, ())
