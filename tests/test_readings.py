import sqlite3
from datetime import UTC, datetime

import pytest

from agentc import store

ROWS = [  # ts_utc, plug, power_w, total_kwh, error; the logger's columns that matter here
    ("2026-11-01T19:30:00+00:00", "Plug-6", 10.0, 0.500, None),
    ("2026-11-01T19:31:00+00:00", "Plug-6", None, None, "TimeoutError"),  # failed read
    ("2026-11-01T19:32:00+00:00", "Plug-6", 9.5, 0.501, None),
    ("2026-11-01T19:32:00+00:00", "Plug-2", 50.0, 7.000, None),  # another plug
    ("2026-11-01T20:00:00+00:00", "Plug-6", 1.0, 0.505, None),  # after the window
]
START = datetime(2026, 11, 1, 19, 30, tzinfo=UTC)
END = datetime(2026, 11, 1, 20, 0, tzinfo=UTC)  # end is excluded


@pytest.fixture
def energy_db(tmp_path):
    """A small energy.db shaped like the logger's."""
    path = tmp_path / "energy.db"
    with sqlite3.connect(path) as db:
        columns = "ts_utc TEXT, plug TEXT, power_w REAL, total_kwh REAL, error TEXT"
        db.execute(f"CREATE TABLE readings ({columns})")
        db.executemany("INSERT INTO readings VALUES (?, ?, ?, ?, ?)", ROWS)
    return path


def test_reads_one_plug_in_a_window_skipping_failed_reads(energy_db):
    db = store.open_readings(energy_db)
    assert store.readings(db, plug="Plug-6", start=START, end=END) == [
        (datetime(2026, 11, 1, 19, 30, tzinfo=UTC), 10.0),
        (datetime(2026, 11, 1, 19, 32, tzinfo=UTC), 9.5),
    ]


def test_counter_reads_the_plugs_energy_meter_in_a_window(energy_db):
    db = store.open_readings(energy_db)
    assert store.counter(db, plug="Plug-6", start=START, end=END) == [
        (datetime(2026, 11, 1, 19, 30, tzinfo=UTC), 0.500),
        (datetime(2026, 11, 1, 19, 32, tzinfo=UTC), 0.501),
    ]


def test_energy_db_is_opened_read_only(energy_db):
    db = store.open_readings(energy_db)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.execute("DELETE FROM readings")
