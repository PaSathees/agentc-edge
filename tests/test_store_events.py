import sqlite3
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
    cut = Event("no_contact", None, NOW, {"plugs": 10})
    store.add_event(db, site_id=1, event=cut, dedupe="site|no_contact|2026-11-03")
    assert store.load_events(db, site_id=1) == [cut]


def test_event_times_are_saved_in_utc(db):  # so the newest one sorts last as text
    local = Event("offline", "AA", NOW.astimezone(ZoneInfo("Asia/Colombo")), {})
    store.add_event(db, site_id=1, event=local, dedupe="AA|offline|2026-11-03")
    assert db.execute("SELECT start_utc FROM events").fetchone() == ("2026-11-02T19:30:00+00:00",)


def test_new_events_are_one_kind_not_yet_handled(db):
    cut = Event("no_contact", None, NOW, {"plugs": 10})
    store.add_event(db, site_id=1, event=LEFT_ON, dedupe="a")
    store.add_event(db, site_id=1, event=cut, dedupe="b")
    store.add_event(db, site_id=1, event=LEFT_ON, dedupe="c")
    store.event_done(db, event_id=1)
    assert store.new_events(db, site_id=1, kind="unusual_use") == [(3, LEFT_ON)]


def test_a_failed_try_is_counted_and_the_third_parks_it(db):
    store.add_event(db, site_id=1, event=LEFT_ON, dedupe="a")
    assert [store.event_failed(db, event_id=1, limit=3) for _ in range(3)] == [False, False, True]
    assert db.execute("SELECT status, tries FROM events").fetchone() == ("parked", 3)
    assert store.new_events(db, site_id=1, kind="unusual_use") == []
    assert store.count_events(db, site_id=1, status="parked") == 1


def test_an_app_db_from_before_phase_2_gets_the_tries_column(tmp_path):
    old = sqlite3.connect(tmp_path / "app.db")
    old.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, site_id, kind, plug_mac,"
                " device_id, start_utc, facts, status, dedupe UNIQUE)")  # fmt: skip
    old.execute("PRAGMA user_version = 2")
    old.close()
    db = store.connect(tmp_path / "app.db")
    assert "tries" in [row[1] for row in db.execute("PRAGMA table_info(events)")]


def test_an_app_db_from_phase_3c_gets_the_sent_column(tmp_path):
    old = sqlite3.connect(tmp_path / "app.db")
    old.execute("CREATE TABLE questions (id INTEGER PRIMARY KEY, site_id, event_id, plug_mac,"
                " action, text, choices, status, send_at, created, answer, answered)")  # fmt: skip
    old.execute("PRAGMA user_version = 6")
    old.close()
    db = store.connect(tmp_path / "app.db")
    assert "sent" in [row[1] for row in db.execute("PRAGMA table_info(questions)")]
    assert db.execute("PRAGMA user_version").fetchone() == (store.SCHEMA_VERSION,)


def test_an_app_db_from_phase_3d3_gets_the_reads_column(tmp_path):
    old = sqlite3.connect(tmp_path / "app.db")
    old.execute("CREATE TABLE chats (id INTEGER PRIMARY KEY, site_id, plug_mac, kind, status,"
                " draft, created)")  # fmt: skip
    old.execute("PRAGMA user_version = 9")
    old.close()
    db = store.connect(tmp_path / "app.db")
    assert "reads" in [row[1] for row in db.execute("PRAGMA table_info(chats)")]


def test_an_app_db_from_phase_5b1_gets_the_ready_column(tmp_path):
    old = sqlite3.connect(tmp_path / "app.db")
    old.execute("CREATE TABLE plan_items (id INTEGER PRIMARY KEY, plan_id, site_id, plug_mac,"
                " change, why, saving, status, decided)")  # fmt: skip
    old.execute("PRAGMA user_version = 11")
    old.close()
    db = store.connect(tmp_path / "app.db")
    assert "ready" in [row[1] for row in db.execute("PRAGMA table_info(plan_items)")]


def test_an_app_db_from_phase_5b5_keeps_its_chats_and_takes_review_chats(tmp_path):
    db = store.connect(tmp_path / "app.db")
    store.add_site(db, name="home", tz="Asia/Colombo")
    store.plug_seen(db, site_id=1, mac="AA", name="Plug-1", ip="192.0.2.1", timers_on=True,
                    now=NOW)  # fmt: skip
    chat = store.open_chat(db, site_id=1, mac="AA", kind="setup", now=NOW)
    store.add_turn(db, chat_id=chat, site_id=1, role="agent", text="Hello", now=NOW)
    old = SCHEMA_12_CHATS  # the table as 5b5 left it
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("PRAGMA legacy_alter_table = ON")  # keep chat_turns pointing at "chats"
    db.execute("ALTER TABLE chats RENAME TO chats_new")
    db.execute(old)
    db.execute(
        "INSERT INTO chats (id, site_id, plug_mac, kind, status, draft, created, reads)"
        " SELECT id, site_id, plug_mac, kind, status, draft, created, reads FROM chats_new"
    )
    db.execute("DROP TABLE chats_new")
    db.execute("PRAGMA user_version = 12")
    db.commit()
    db.close()
    db = store.connect(tmp_path / "app.db")
    assert [(r, t) for _, r, t in store.turns(db, chat_id=chat)] == [("agent", "Hello")]
    assert store.chat(db, chat_id=chat).plug_mac == "AA"  # pyright: ignore[reportOptionalMemberAccess]
    store.add_plan(db, site_id=1, week="2026-11-23", summary="s", items=[], now=NOW)
    review = store.open_review(db, site_id=1, plan_id=1, item_id=None, now=NOW)
    assert store.open_review(db, site_id=1, plan_id=1, item_id=None, now=NOW) == review
    found = store.chat(db, chat_id=review)
    assert found is not None and (found.plug_mac, found.plan_id, found.item_id) == ("", 1, None)
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


SCHEMA_12_CHATS = """CREATE TABLE chats (
    id       INTEGER PRIMARY KEY,
    site_id  INTEGER NOT NULL REFERENCES sites(id),
    plug_mac TEXT NOT NULL REFERENCES plugs(mac),
    kind     TEXT NOT NULL CHECK (kind IN ('setup')),
    status   TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'saved', 'closed')),
    draft    TEXT,
    created  TEXT NOT NULL,
    reads    TEXT
)"""


def test_a_migration_stopped_half_way_does_not_block_the_next_start(tmp_path):
    db = store.connect(tmp_path / "app.db")
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("PRAGMA legacy_alter_table = ON")
    db.execute("ALTER TABLE chats RENAME TO chats_new")
    db.execute(SCHEMA_12_CHATS)
    db.execute("DROP TABLE chats_new")
    db.execute("CREATE TABLE chats_new (id)")  # left behind by a stop half way
    db.execute("PRAGMA user_version = 12")
    db.commit()
    db.close()
    db = store.connect(tmp_path / "app.db")
    assert "plan_id" in [c[1] for c in db.execute("PRAGMA table_info(chats)")]
    assert db.execute("SELECT name FROM sqlite_master WHERE name = 'chats_new'").fetchone() is None


def test_a_migration_that_fails_leaves_the_old_table_as_it_was(tmp_path):
    db = sqlite3.connect(tmp_path / "app.db")
    db.execute(SCHEMA_12_CHATS.replace("CHECK (kind IN ('setup'))", ""))
    db.execute("INSERT INTO chats (site_id, plug_mac, kind, created) VALUES (1, 'AA', 'odd', 'x')")
    db.execute("PRAGMA user_version = 12")
    db.commit()
    db.close()
    with pytest.raises(sqlite3.IntegrityError):  # 'odd' breaks the new table's CHECK
        store.connect(tmp_path / "app.db")
    db = sqlite3.connect(tmp_path / "app.db")
    assert db.execute("SELECT kind FROM chats").fetchall() == [("odd",)]
    assert db.execute("SELECT name FROM sqlite_master WHERE name = 'chats_new'").fetchone() is None
