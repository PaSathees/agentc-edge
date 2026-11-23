import sqlite3

import pytest

from agentc import store

# The app.db tables from docs/design/data.md.
TABLES = {
    "sites", "plugs", "timers", "profiles", "thresholds", "events", "audit", "llm_calls",
    "cards", "automations", "habits", "questions", "settings",
}  # fmt: skip


@pytest.fixture
def db(tmp_path):
    """A fresh app.db in a temp folder, deleted after each test."""
    return store.connect(tmp_path / "app.db")


@pytest.fixture
def home(db):
    """db with one site (id 1) and one plug (mac "AA"), the parents most rows need."""
    db.execute("INSERT INTO sites (id, name, tz) VALUES (?, ?, ?)", (1, "home", "Asia/Colombo"))
    db.execute("INSERT INTO plugs (mac, site_id, name) VALUES (?, ?, ?)", ("AA", 1, "Plug-6"))
    return db


def table_names(db: sqlite3.Connection) -> set[str]:
    rows = db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {r[0] for r in rows}


def test_connect_creates_every_table(db):
    assert TABLES <= table_names(db)


def test_connect_twice_is_safe(tmp_path):
    path = tmp_path / "app.db"
    first = store.connect(path)
    first.execute("INSERT INTO sites (id, name, tz) VALUES (?, ?, ?)", (1, "home", "Asia/Colombo"))
    first.commit()
    first.close()
    second = store.connect(path)
    assert TABLES <= table_names(second)
    assert second.execute("SELECT name FROM sites").fetchall() == [("home",)]


def test_wal_mode_on(db):
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_foreign_keys_on(home):
    with pytest.raises(sqlite3.IntegrityError):  # plug "ZZ" does not exist
        home.execute("INSERT INTO timers (site_id, plug_mac, slot) VALUES (1, 'ZZ', 1)")


def test_an_old_app_db_loses_the_unused_tables_and_keeps_working(tmp_path):
    old = sqlite3.connect(tmp_path / "app.db")
    old.executescript("CREATE TABLE devices (id INTEGER PRIMARY KEY);"
                      "CREATE TABLE proposals (id INTEGER); CREATE TABLE writes (id INTEGER);"
                      "CREATE TABLE factors (id TEXT); PRAGMA user_version = 7;")  # fmt: skip
    old.close()
    db = store.connect(tmp_path / "app.db")
    assert not {"proposals", "writes", "factors"} & table_names(db)
    assert "devices" in table_names(db)  # an old events table may still point at it: kept
    assert db.execute("PRAGMA user_version").fetchone() == (store.SCHEMA_VERSION,)


def test_every_table_has_site_id(db):
    for table in TABLES - {"sites"}:
        columns = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        assert "site_id" in columns, f"{table} has no site_id"


def test_timer_slot_must_be_1_to_16(home):
    add = "INSERT INTO timers (site_id, plug_mac, slot) VALUES (1, 'AA', ?)"
    home.execute(add, (16,))  # last allowed slot
    for bad in (0, 17):
        with pytest.raises(sqlite3.IntegrityError):
            home.execute(add, (bad,))
