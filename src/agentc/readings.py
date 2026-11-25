"""energy.db, read only. It belongs to the logger: the app never writes it (ADR-006).

Each row is one plug's reading in one logger round, with its time as UTC ISO text.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def connect(path: str | Path) -> sqlite3.Connection:
    """Open energy.db read-only. Any write through this connection fails."""
    return sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)


def power(
    db: sqlite3.Connection, *, plug: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """One plug's minute readings in [start, end), oldest first; failed reads skipped."""
    sql = """SELECT ts_utc, power_w FROM readings
             WHERE plug = ? AND ts_utc >= ? AND ts_utc < ? AND power_w IS NOT NULL
             ORDER BY ts_utc"""
    rows = db.execute(sql, (plug, *utc_bounds(start, end)))
    return [(datetime.fromisoformat(ts), w) for ts, w in rows]


def counter(
    db: sqlite3.Connection, *, plug: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """The plug's own energy meter (total kWh) in [start, end), oldest first."""
    sql = """SELECT ts_utc, total_kwh FROM readings
             WHERE plug = ? AND ts_utc >= ? AND ts_utc < ? AND total_kwh IS NOT NULL
             ORDER BY ts_utc"""
    rows = db.execute(sql, (plug, *utc_bounds(start, end)))
    return [(datetime.fromisoformat(ts), total) for ts, total in rows]


def last_rounds(
    db: sqlite3.Connection, *, n: int, end: datetime
) -> list[tuple[datetime, dict[str, bool]]]:
    """The logger's last n rounds before `end`, oldest first: which plugs answered."""
    sql = """SELECT ts_utc, plug, error IS NULL FROM readings
             WHERE ts_utc IN (SELECT DISTINCT ts_utc FROM readings
                              WHERE ts_utc < ? ORDER BY ts_utc DESC LIMIT ?)
             ORDER BY ts_utc"""
    rounds: dict[str, dict[str, bool]] = {}
    for ts, plug, answered in db.execute(sql, (end.astimezone(UTC).isoformat(), n)):
        rounds.setdefault(ts, {})[plug] = bool(answered)
    return [(datetime.fromisoformat(ts), plugs) for ts, plugs in rounds.items()]


def last_round(db: sqlite3.Connection, *, end: datetime) -> datetime | None:
    """When the logger's newest round before `end` was, failed reads included. None if none."""
    sql = "SELECT MAX(ts_utc) FROM readings WHERE ts_utc < ?"
    (ts,) = db.execute(sql, (end.astimezone(UTC).isoformat(),)).fetchone()
    return datetime.fromisoformat(ts) if ts else None


def answered(db: sqlite3.Connection, *, plug: str | None, start: datetime, end: datetime) -> bool:
    """Did this plug (or, with None, any plug) answer at least once in [start, end)?"""
    sql = "SELECT 1 FROM readings WHERE ts_utc >= ? AND ts_utc < ? AND error IS NULL"
    args: tuple[str, ...] = utc_bounds(start, end)
    if plug is not None:
        sql, args = sql + " AND plug = ?", (*args, plug)
    return db.execute(sql + " LIMIT 1", args).fetchone() is not None


def utc_bounds(start: datetime, end: datetime) -> tuple[str, str]:
    """[start, end) as UTC text, to compare with the stored times."""
    # Stored as UTC ISO text, so the bounds must be UTC text too for the comparison to hold
    return start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()


def latest(db: sqlite3.Connection, *, since: datetime) -> dict[str, float | None]:
    """Each plug's watts in the logger's newest round since a time; None for a failed read.
    Empty if no round came since then."""
    sql = """SELECT plug, power_w FROM readings
             WHERE ts_utc = (SELECT MAX(ts_utc) FROM readings WHERE ts_utc >= ?)"""
    return dict(db.execute(sql, (since.astimezone(UTC).isoformat(),)).fetchall())
