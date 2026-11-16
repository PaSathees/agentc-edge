from datetime import UTC, datetime
from zoneinfo import ZoneInfo

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


def test_event_times_are_saved_in_utc(db):  # so the newest one sorts last as text
    local = Event("offline", "AA", NOW.astimezone(ZoneInfo("Asia/Colombo")), {})
    store.add_event(db, site_id=1, event=local, dedupe="AA|offline|2026-11-03")
    assert db.execute("SELECT start_utc FROM events").fetchone() == ("2026-11-02T19:30:00+00:00",)


def test_new_events_are_one_kind_not_yet_handled(db):
    cut = Event("power_cut", None, NOW, {"plugs": 10})
    store.add_event(db, site_id=1, event=LEFT_ON, dedupe="a")
    store.add_event(db, site_id=1, event=cut, dedupe="b")
    store.add_event(db, site_id=1, event=LEFT_ON, dedupe="c")
    store.event_done(db, event_id=1)
    assert store.new_events(db, site_id=1, kind="unusual_use") == [(3, LEFT_ON)]
