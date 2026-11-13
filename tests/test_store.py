import sqlite3

import pytest

from agentc import store

# The app.db tables from docs/design/data.md (LangGraph makes its own).
TABLES = {
    "sites", "plugs", "devices", "placements", "timers", "rules", "profiles",
    "thresholds", "weather", "factors", "goals", "events", "proposals",
    "writes", "audit", "llm_calls", "settings",
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
    with pytest.raises(sqlite3.IntegrityError):  # device 999 does not exist
        home.execute(
            "INSERT INTO placements (site_id, device_id, plug_mac, from_utc) VALUES (?, ?, ?, ?)",
            (1, 999, "AA", "2026-11-12T00:00:00Z"),
        )


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
