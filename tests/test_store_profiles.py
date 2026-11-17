import sqlite3
from datetime import UTC, datetime

import pytest

from agentc import store
from agentc.calc import ProfileHour

DESK = [ProfileHour(1, 10.0, 1.0, 7), ProfileHour(2, 1.0, 0.0, 7), ProfileHour(25, 10.0, 0.9, 7)]


@pytest.fixture
def db(tmp_path):
    db = store.connect(tmp_path / "app.db")
    db.execute("INSERT INTO sites (id, name, tz) VALUES (1, 'home', 'Asia/Colombo')")
    db.execute("INSERT INTO plugs (mac, site_id, name) VALUES ('AA', 1, 'Plug-6')")
    db.execute("INSERT INTO plugs (mac, site_id, name) VALUES ('BB', 1, 'Plug-2')")
    return db


def test_saved_profile_loads_back_the_same(db):
    store.save_profile(db, site_id=1, mac="AA", profile=DESK)
    assert store.load_profile(db, mac="AA") == DESK


def test_rebuild_replaces_the_old_hours(db):
    store.save_profile(db, site_id=1, mac="AA", profile=DESK)
    store.save_profile(db, site_id=1, mac="AA", profile=DESK[:1])
    assert store.load_profile(db, mac="AA") == DESK[:1]  # hours 2 and 25 gone, not left stale


def test_each_plug_keeps_its_own_profile(db):
    store.save_profile(db, site_id=1, mac="AA", profile=DESK)
    store.save_profile(db, site_id=1, mac="BB", profile=DESK[:1])
    assert store.load_profile(db, mac="AA") == DESK


def test_no_profile_yet_loads_empty(db):
    assert store.load_profile(db, mac="AA") == []


# learned policy values (B3b)

T = datetime(2026, 11, 17, tzinfo=UTC)


def test_learned_values_load_back_and_a_new_learn_replaces_them(db):
    store.save_learned(
        db, site_id=1, mac="AA", values={"use_w": (12.0, "why"), "small_w": (1.5, "w")}, at=T
    )
    assert store.learned(db, mac="AA") == {"small_w": (1.5, "w"), "use_w": (12.0, "why")}
    store.save_learned(db, site_id=1, mac="AA", values={}, at=T)  # now one level only
    assert store.learned(db, mac="AA") == {}
    assert store.learned(db, mac="BB") == {}


def test_an_app_db_from_before_b3b_gets_the_new_thresholds_table(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE thresholds (site_id, plug_mac PRIMARY KEY, factor, changed_by)")
    old.execute("PRAGMA user_version = 1")
    old.close()
    db = store.connect(path)
    db.execute("INSERT INTO sites (id, name, tz) VALUES (1, 'home', 'Asia/Colombo')")
    db.execute("INSERT INTO plugs (mac, site_id, name) VALUES ('AA', 1, 'Plug-6')")
    store.save_learned(db, site_id=1, mac="AA", values={"use_w": (12.0, "why")}, at=T)
    assert store.learned(db, mac="AA") == {"use_w": (12.0, "why")}
