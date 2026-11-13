"""The only module that runs SQL on app.db (ADR-006). Schema: docs/design/data.md.

Times are ISO 8601 text in UTC; profiles and timers use the site's local time.
JSON columns hold text written with json.dumps.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from agentc.devices.base import Timer

SCHEMA_VERSION = 1

SCHEMA = """
-- One home or plant room; every other row belongs to one
CREATE TABLE IF NOT EXISTS sites (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    tz        TEXT NOT NULL,
    lat       REAL,
    lon       REAL
);
-- The hardware, keyed by MAC; name matches readings.plug in energy.db
CREATE TABLE IF NOT EXISTS plugs (
    mac         TEXT PRIMARY KEY,
    site_id     INTEGER NOT NULL REFERENCES sites(id),
    name        TEXT NOT NULL,
    ip          TEXT,
    first_seen  TEXT,
    last_seen   TEXT,
    reachable   INTEGER NOT NULL DEFAULT 1 CHECK (reachable IN (0, 1)),
    last_error  TEXT,
    timers_on   INTEGER CHECK (timers_on IN (0, 1))  -- the plug's global Timers switch
);
-- What is plugged in; access and the timer slots the system may use
CREATE TABLE IF NOT EXISTS devices (
    id      INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    name    TEXT NOT NULL,
    room    TEXT,
    type    TEXT,
    rated_w REAL,
    access  TEXT NOT NULL DEFAULT 'advice' CHECK (access IN ('none', 'advice', 'write')),
    slots   TEXT NOT NULL DEFAULT '9-16'
);
-- Which device was on which plug, and when (devices move)
CREATE TABLE IF NOT EXISTS placements (
    site_id   INTEGER NOT NULL REFERENCES sites(id),
    device_id INTEGER NOT NULL REFERENCES devices(id),
    plug_mac  TEXT NOT NULL REFERENCES plugs(mac),
    from_utc  TEXT NOT NULL,
    to_utc    TEXT,
    PRIMARY KEY (device_id, from_utc)
);
-- Last read of each plug's 16 Tasmota timers; fields as Tasmota names them
CREATE TABLE IF NOT EXISTS timers (
    site_id  INTEGER NOT NULL REFERENCES sites(id),
    plug_mac TEXT NOT NULL REFERENCES plugs(mac),
    slot     INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 16),
    enable   INTEGER NOT NULL DEFAULT 0 CHECK (enable IN (0, 1)),
    mode     INTEGER NOT NULL DEFAULT 0 CHECK (mode IN (0, 1, 2)),
    time     TEXT NOT NULL DEFAULT '00:00',
    window   INTEGER NOT NULL DEFAULT 0 CHECK (window BETWEEN 0 AND 15),
    days     TEXT NOT NULL DEFAULT '0000000',
    repeat   INTEGER NOT NULL DEFAULT 1 CHECK (repeat IN (0, 1)),
    output   INTEGER NOT NULL DEFAULT 1,
    action   INTEGER NOT NULL DEFAULT 0 CHECK (action BETWEEN 0 AND 3),
    source   TEXT NOT NULL DEFAULT 'import' CHECK (source IN ('import', 'actuator', 'owner')),
    read_at  TEXT,
    PRIMARY KEY (plug_mac, slot)
);
-- Owner rules per device; hard ones block a proposal, soft ones warn
CREATE TABLE IF NOT EXISTS rules (
    id        INTEGER PRIMARY KEY,
    site_id   INTEGER NOT NULL REFERENCES sites(id),
    device_id INTEGER NOT NULL REFERENCES devices(id),
    kind      TEXT NOT NULL CHECK (kind IN
                ('must_run', 'min_runtime', 'latest_off', 'never_off', 'priority')),
    params    TEXT NOT NULL DEFAULT '{}',
    hard      INTEGER NOT NULL CHECK (hard IN (0, 1)),
    weight    REAL NOT NULL DEFAULT 1,
    origin    TEXT NOT NULL CHECK (origin IN ('owner', 'rejection', 'chat')),
    active    INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
-- Usual power per plug for each hour of the week (0 = Monday 00:00)
CREATE TABLE IF NOT EXISTS profiles (
    site_id   INTEGER NOT NULL REFERENCES sites(id),
    plug_mac  TEXT NOT NULL REFERENCES plugs(mac),
    hour      INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 167),
    median_w  REAL NOT NULL,
    on_share  REAL NOT NULL,
    days_used INTEGER NOT NULL,
    PRIMARY KEY (plug_mac, hour)
);
-- How far above its profile a plug must go to raise an event
CREATE TABLE IF NOT EXISTS thresholds (
    site_id    INTEGER NOT NULL REFERENCES sites(id),
    plug_mac   TEXT PRIMARY KEY REFERENCES plugs(mac),
    factor     REAL NOT NULL,
    changed_by TEXT NOT NULL
);
-- Open-Meteo hourly temperature, forecast and actual
CREATE TABLE IF NOT EXISTS weather (
    site_id  INTEGER NOT NULL REFERENCES sites(id),
    ts_utc   TEXT NOT NULL,
    temp_c   REAL,
    forecast INTEGER NOT NULL CHECK (forecast IN (0, 1)),
    PRIMARY KEY (site_id, ts_utc, forecast)
);
-- Grid factor and tariff, each with its source; reports cite the id
CREATE TABLE IF NOT EXISTS factors (
    id         TEXT PRIMARY KEY,
    site_id    INTEGER NOT NULL REFERENCES sites(id),
    kind       TEXT NOT NULL CHECK (kind IN ('grid', 'tariff')),
    value      TEXT NOT NULL,
    unit       TEXT NOT NULL,
    source     TEXT NOT NULL,
    valid_from TEXT NOT NULL
);
-- Monthly kgCO2e goal
CREATE TABLE IF NOT EXISTS goals (
    site_id INTEGER NOT NULL REFERENCES sites(id),
    month   TEXT NOT NULL,
    kgco2e  REAL NOT NULL,
    PRIMARY KEY (site_id, month)
);
-- What the Watcher saw; dedupe (plug + kind + day) stops repeats
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY,
    site_id   INTEGER NOT NULL REFERENCES sites(id),
    kind      TEXT NOT NULL,
    plug_mac  TEXT REFERENCES plugs(mac),
    device_id INTEGER REFERENCES devices(id),
    start_utc TEXT NOT NULL,
    facts     TEXT NOT NULL DEFAULT '{}',
    status    TEXT NOT NULL DEFAULT 'new',
    dedupe    TEXT NOT NULL UNIQUE
);
-- Agent output, one row per version; body is the JSON contract
CREATE TABLE IF NOT EXISTS proposals (
    id       INTEGER NOT NULL,
    version  INTEGER NOT NULL DEFAULT 1,
    site_id  INTEGER NOT NULL REFERENCES sites(id),
    event_id INTEGER REFERENCES events(id),
    source   TEXT NOT NULL CHECK (source IN ('responder', 'planner', 'assistant', 'owner')),
    body     TEXT NOT NULL,
    status   TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'blocked', 'open',
               'rejected', 'expired', 'approved', 'recorded', 'written', 'verified', 'failed')),
    created  TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
-- Each timer write with before, after and read back; Undo restores before
CREATE TABLE IF NOT EXISTS writes (
    id          INTEGER PRIMARY KEY,
    site_id     INTEGER NOT NULL REFERENCES sites(id),
    proposal_id INTEGER NOT NULL,
    version     INTEGER NOT NULL,
    plug_mac    TEXT NOT NULL REFERENCES plugs(mac),
    slot        INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 16),
    before      TEXT NOT NULL,
    after       TEXT NOT NULL,
    read_back   TEXT,
    ok          INTEGER CHECK (ok IN (0, 1)),
    ts_utc      TEXT NOT NULL,
    FOREIGN KEY (proposal_id, version) REFERENCES proposals(id, version)
);
-- Append-only history of every change, by code, agent or owner
CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    ts_utc  TEXT NOT NULL,
    actor   TEXT NOT NULL CHECK (actor IN ('code', 'agent', 'owner')),
    action  TEXT NOT NULL,
    ref     TEXT,
    detail  TEXT NOT NULL DEFAULT '{}'
);
-- Every model call: prompt, output, tokens, cost (trace and budget)
CREATE TABLE IF NOT EXISTS llm_calls (
    id             INTEGER PRIMARY KEY,
    site_id        INTEGER NOT NULL REFERENCES sites(id),
    run_id         TEXT NOT NULL,
    agent          TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt         TEXT NOT NULL,
    output         TEXT,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    usd            REAL,
    latency_ms     INTEGER,
    proposal_id    INTEGER,
    ts_utc         TEXT NOT NULL
);
-- Shadow mode, pause, caps; key-value
CREATE TABLE IF NOT EXISTS settings (
    site_id INTEGER NOT NULL REFERENCES sites(id),
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (site_id, key)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open app.db, creating any missing table. Safe to call on every start."""
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 5000")
    db.executescript(SCHEMA)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return db


def plug_seen(
    db: sqlite3.Connection, *, site_id: int, mac: str, name: str, ip: str,
    timers_on: bool, now: datetime,
) -> None:  # fmt: skip
    """Record a plug that answered: insert it, or update it and clear any error."""
    with db:
        db.execute(
            """INSERT INTO plugs (mac, site_id, name, ip, first_seen, last_seen, timers_on)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (mac) DO UPDATE SET name = excluded.name, ip = excluded.ip,
                 last_seen = excluded.last_seen, timers_on = excluded.timers_on,
                 reachable = 1, last_error = NULL""",
            (mac, site_id, name, ip, now.isoformat(), now.isoformat(), timers_on),
        )


def plug_failed(db: sqlite3.Connection, *, name: str, error: str) -> None:
    """Mark a known plug unreachable. A plug never seen has no MAC yet, so no row."""
    with db:
        db.execute("UPDATE plugs SET reachable = 0, last_error = ? WHERE name = ?", (error, name))


TIMER_FIELDS = "slot, enable, mode, time, window, days, repeat, output, action"


def save_timers(
    db: sqlite3.Connection, *, site_id: int, mac: str, timers: list[Timer], now: datetime
) -> None:
    """Replace a plug's stored timers with what was just read from it."""
    rows = [(site_id, mac, now.isoformat(), *_timer_values(t)) for t in timers]
    with db:  # one transaction: all 16 or none
        db.execute("DELETE FROM timers WHERE plug_mac = ?", (mac,))
        db.executemany(
            f"INSERT INTO timers (site_id, plug_mac, read_at, {TIMER_FIELDS})"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def load_timers(db: sqlite3.Connection, *, mac: str) -> list[Timer]:
    sql = f"SELECT {TIMER_FIELDS} FROM timers WHERE plug_mac = ? ORDER BY slot"
    return [
        Timer(slot, bool(en), mode, time, window, days, bool(rep), output, action)
        for slot, en, mode, time, window, days, rep, output, action in db.execute(sql, (mac,))
    ]


def _timer_values(t: Timer) -> tuple[int | str, ...]:
    return (t.slot, int(t.enable), t.mode, t.time, t.window, t.days, int(t.repeat),
            t.output, t.action)  # fmt: skip
