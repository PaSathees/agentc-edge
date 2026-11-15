from datetime import UTC, datetime

import pytest

from agentc import store
from agentc.watcher import Event

NOW = datetime(2026, 11, 2, 19, 30, tzinfo=UTC)
LEFT_ON = Event("unusual_use", "AA", NOW, {"peak_w": 30.0, "minutes": 120})


@pytest.fixture
def db(tmp_path):
    db = store.connect(tmp_path / "app.db")
    site_id = store.add_site(db, name="home", tz="Asia/Colombo")
    store.plug_seen(db, site_id=site_id, mac="AA", name="Plug-6", ip="192.0.2.1",
                    timers_on=True, now=NOW)  # fmt: skip
    return db


def test_an_event_is_saved_and_read_back(db):
    assert store.add_event(db, site_id=1, event=LEFT_ON, dedupe="AA|unusual_use|2026-11-03")
    assert store.load_events(db, site_id=1) == [LEFT_ON]


def test_the_same_event_is_saved_only_once(db):
    store.add_event(db, site_id=1, event=LEFT_ON, dedupe="AA|unusual_use|2026-11-03")
    assert not store.add_event(db, site_id=1, event=LEFT_ON, dedupe="AA|unusual_use|2026-11-03")
    assert len(store.load_events(db, site_id=1)) == 1


def test_a_site_wide_event_has_no_plug(db):
    cut = Event("power_cut", None, NOW, {"plugs": 10})
    store.add_event(db, site_id=1, event=cut, dedupe="site|power_cut|2026-11-03")
    assert store.load_events(db, site_id=1) == [cut]
