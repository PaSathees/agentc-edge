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
