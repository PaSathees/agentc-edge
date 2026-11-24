"""The only module that runs SQL on app.db (decision ADR-006). Tables: docs/design/data.md.

Times are ISO 8601 text in UTC; profiles and timers use the site's local time.
JSON columns hold text written with json.dumps.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentc.calc import ProfileHour
from agentc.devices.base import Automations, PlugRule, Timer
from agentc.house import SOURCES, Card, Habit
from agentc.owner import Choice, Decision, Question
from agentc.readings import utc_bounds
from agentc.watcher import Event

# 2: thresholds a row. 3: events.tries. 4: cards, habits. 5: automations. 6: questions.
# 7: questions.sent. 8: unused v1 tables dropped. 9: chats
SCHEMA_VERSION = 9
UNUSED = ("writes", "proposals", "placements", "rules", "weather", "goals", "factors")

SCHEMA = """
-- One home or plant room; every other row belongs to one
CREATE TABLE IF NOT EXISTS sites (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    tz        TEXT NOT NULL
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
-- Values learned per plug, each with why (e.g. its standby level)
CREATE TABLE IF NOT EXISTS thresholds (
    site_id  INTEGER NOT NULL REFERENCES sites(id),
    plug_mac TEXT NOT NULL REFERENCES plugs(mac),
    name     TEXT NOT NULL,
    value    REAL NOT NULL,
    why      TEXT NOT NULL,
    at       TEXT NOT NULL,
    PRIMARY KEY (plug_mac, name)
);
-- The signals; dedupe (plug + kind + day) stops repeats
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY,
    site_id   INTEGER NOT NULL REFERENCES sites(id),
    kind      TEXT NOT NULL,
    plug_mac  TEXT REFERENCES plugs(mac),
    start_utc TEXT NOT NULL,
    facts     TEXT NOT NULL DEFAULT '{}',
    status    TEXT NOT NULL DEFAULT 'new',  -- new, done (looked at), skipped or parked
    tries     INTEGER NOT NULL DEFAULT 0,  -- failed attempts to answer it
    dedupe    TEXT NOT NULL UNIQUE
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
    ts_utc         TEXT NOT NULL
);
-- What the device on each plug is: the house model's card (FR-30). One row a plug
CREATE TABLE IF NOT EXISTS cards (
    plug_mac TEXT PRIMARY KEY REFERENCES plugs(mac),
    site_id  INTEGER NOT NULL REFERENCES sites(id),
    kind     TEXT NOT NULL,
    room     TEXT NOT NULL,
    what     TEXT NOT NULL,
    notes    TEXT NOT NULL,
    must_run INTEGER CHECK (must_run IN (0, 1)),  -- NULL: from the kind
    why      TEXT NOT NULL DEFAULT '{}',  -- JSON: timer slot -> what it is for
    updated  TEXT NOT NULL
);
-- What each plug does on its own besides timers: auto-off and rules; read with the timers
CREATE TABLE IF NOT EXISTS automations (
    plug_mac   TEXT PRIMARY KEY REFERENCES plugs(mac),
    site_id    INTEGER NOT NULL REFERENCES sites(id),
    auto_off_s REAL,  -- NULL: no auto-off
    rules      TEXT NOT NULL DEFAULT '[]',  -- JSON: [[n, on, text], ...]
    read_at    TEXT NOT NULL
);
-- What is normal for the owner, from their answers (FR-33); ended habits are kept
CREATE TABLE IF NOT EXISTS habits (
    id       INTEGER PRIMARY KEY,
    site_id  INTEGER NOT NULL REFERENCES sites(id),
    plug_mac TEXT NOT NULL REFERENCES plugs(mac),
    text     TEXT NOT NULL,
    days     TEXT NOT NULL,  -- 7 chars, Sunday first, as Tasmota
    start    TEXT NOT NULL,  -- local HH:MM
    end_hm   TEXT NOT NULL,  -- local HH:MM; earlier than start = the next day
    source   TEXT NOT NULL CHECK (source IN ('tap', 'chat', 'setup')),
    created  TEXT NOT NULL,
    ended    TEXT  -- set when the owner ends it; NULL = active
);
-- What the housemate asks or proposes, and the owner's answer (FR-36)
CREATE TABLE IF NOT EXISTS questions (
    id       INTEGER PRIMARY KEY,
    site_id  INTEGER NOT NULL REFERENCES sites(id),
    event_id INTEGER NOT NULL REFERENCES events(id),
    plug_mac TEXT NOT NULL REFERENCES plugs(mac),
    action   TEXT NOT NULL CHECK (action IN ('ask', 'propose')),
    text     TEXT NOT NULL,
    choices  TEXT NOT NULL,  -- JSON: [{label, does, off, on, days}, ...]
    status   TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered', 'expired')),
    send_at  TEXT NOT NULL,  -- when it may reach the owner: at once, or 07:00 after the night
    created  TEXT NOT NULL,
    answer   INTEGER,  -- the choice picked, from 1
    answered TEXT,
    sent     TEXT  -- when it went to the phone; NULL: not yet
);
-- A conversation with the owner, e.g. the setup chat for one plug; the draft card waits
-- here until the owner saves it
CREATE TABLE IF NOT EXISTS chats (
    id       INTEGER PRIMARY KEY,
    site_id  INTEGER NOT NULL REFERENCES sites(id),
    plug_mac TEXT NOT NULL REFERENCES plugs(mac),
    kind     TEXT NOT NULL CHECK (kind IN ('setup')),
    status   TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'saved', 'closed')),
    draft    TEXT,  -- JSON: the card and habits the agent proposed; NULL: none yet
    created  TEXT NOT NULL
);
-- Each message in a chat, in order
CREATE TABLE IF NOT EXISTS chat_turns (
    id      INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    site_id INTEGER NOT NULL REFERENCES sites(id),
    role    TEXT NOT NULL CHECK (role IN ('owner', 'agent')),
    text    TEXT NOT NULL,
    ts_utc  TEXT NOT NULL
);
-- Shadow mode, pause, caps; key-value
CREATE TABLE IF NOT EXISTS settings (
    site_id INTEGER NOT NULL REFERENCES sites(id),
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (site_id, key)
);
"""


def open_app(path: str | Path) -> sqlite3.Connection:
    """Open app.db as it is, with no schema change: for each web request."""
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 5000")
    return db


def connect(path: str | Path) -> sqlite3.Connection:
    """Open app.db, creating any missing table. Safe to call on every start."""
    db = open_app(path)
    db.execute("PRAGMA journal_mode = WAL")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == 1:
        db.execute("DROP TABLE IF EXISTS thresholds")  # version 1's shape; never written to
    if version < 8:  # version 1 tables, never used now; Phase 4 adds back what it needs
        for table in UNUSED:
            db.execute(f"DROP TABLE IF EXISTS {table}")
    db.executescript(SCHEMA)
    if "tries" not in [col[1] for col in db.execute("PRAGMA table_info(events)")]:
        db.execute("ALTER TABLE events ADD COLUMN tries INTEGER NOT NULL DEFAULT 0")  # before 3
    if "sent" not in [col[1] for col in db.execute("PRAGMA table_info(questions)")]:
        db.execute("ALTER TABLE questions ADD COLUMN sent TEXT")  # before 7
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return db


def add_site(db: sqlite3.Connection, *, name: str, tz: str) -> int:
    """Return the site's id. Add the site the first time we see its name."""
    with db:
        row = db.execute("SELECT id FROM sites WHERE name = ?", (name,)).fetchone()
        if row is None:
            sql = "INSERT INTO sites (name, tz) VALUES (?, ?) RETURNING id"
            row = db.execute(sql, (name, tz)).fetchone()
    return row[0]


def site_id(db: sqlite3.Connection, *, name: str) -> int | None:
    """The site's id, or None if it is not there yet (or app.db is not made yet)."""
    try:
        row = db.execute("SELECT id FROM sites WHERE name = ?", (name,)).fetchone()
    except sqlite3.OperationalError:  # no tables yet: the jobs make them on their first start
        return None
    return row[0] if row else None


def plug_mac(db: sqlite3.Connection, *, name: str) -> str | None:
    """The MAC of the plug with this name, or None if we never reached it."""
    sql = "SELECT mac FROM plugs WHERE name = ? ORDER BY last_seen DESC LIMIT 1"
    row = db.execute(sql, (name,)).fetchone()
    return row[0] if row else None


def plug_macs(db: sqlite3.Connection, *, site_id: int) -> dict[str, str]:
    """Every plug the app has reached: name -> MAC."""
    sql = "SELECT name, mac FROM plugs WHERE site_id = ? ORDER BY name"
    return dict(db.execute(sql, (site_id,)).fetchall())


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


PROFILE_FIELDS = "hour, median_w, on_share, days_used"


def save_profile(
    db: sqlite3.Connection, *, site_id: int, mac: str, profile: list[ProfileHour]
) -> None:
    """Replace a plug's stored profile with a freshly built one."""
    rows = [(site_id, mac, p.hour, p.median_w, p.on_share, p.days_used) for p in profile]
    with db:  # one transaction: the old profile or the new one, never a mix
        db.execute("DELETE FROM profiles WHERE plug_mac = ?", (mac,))
        db.executemany(
            f"INSERT INTO profiles (site_id, plug_mac, {PROFILE_FIELDS}) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )


def load_profile(db: sqlite3.Connection, *, mac: str) -> list[ProfileHour]:
    sql = f"SELECT {PROFILE_FIELDS} FROM profiles WHERE plug_mac = ? ORDER BY hour"
    return [ProfileHour(*row) for row in db.execute(sql, (mac,))]


def save_learned(
    db: sqlite3.Connection, *, site_id: int, mac: str, values: dict[str, tuple[float, str]],
    at: datetime,
) -> None:  # fmt: skip
    """Replace a plug's learned policy values (name -> value, why). Empty: none learned."""
    when = at.astimezone(UTC).isoformat()
    rows = [(site_id, mac, name, v, why, when) for name, (v, why) in values.items()]
    with db:
        db.execute("DELETE FROM thresholds WHERE plug_mac = ?", (mac,))
        db.executemany("INSERT INTO thresholds VALUES (?, ?, ?, ?, ?, ?)", rows)


def learned(db: sqlite3.Connection, *, mac: str) -> dict[str, tuple[float, str]]:
    """A plug's learned policy values: name -> (value, why)."""
    sql = "SELECT name, value, why FROM thresholds WHERE plug_mac = ? ORDER BY name"
    return {name: (value, why) for name, value, why in db.execute(sql, (mac,))}


def add_audit(
    db: sqlite3.Connection, *, site_id: int, at: datetime, actor: str, action: str, ref: str,
    detail: dict[str, str],
) -> None:  # fmt: skip
    """Add a line to the history of changes, e.g. why a draft was blocked. Never edited."""
    with db:
        db.execute(
            "INSERT INTO audit (site_id, ts_utc, actor, action, ref, detail)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (site_id, at.astimezone(UTC).isoformat(), actor, action, ref, json.dumps(detail)),
        )


def add_llm_call(
    db: sqlite3.Connection, *, site_id: int, at: datetime, run_id: str, agent: str, model: str,
    prompt_version: str, prompt: str, output: str | None, tokens_in: int | None,
    tokens_out: int | None, usd: float | None, latency_ms: int,
) -> int:  # fmt: skip
    """Save the trace of one model call (FR-27); return its id. output None = the call failed."""
    with db:
        (cid,) = db.execute(
            "INSERT INTO llm_calls (site_id, run_id, agent, model, prompt_version, prompt, output,"
            " tokens_in, tokens_out, usd, latency_ms, ts_utc)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (site_id, run_id, agent, model, prompt_version, prompt, output, tokens_in, tokens_out,
             usd, latency_ms, at.astimezone(UTC).isoformat()),
        ).fetchone()  # fmt: skip
    return cid


def llm_usd(
    db: sqlite3.Connection, *, site_id: int, since: datetime | None = None,
    run_id: str | None = None,
) -> float:  # fmt: skip
    """USD spent on model calls: since a time, in one run, or both (FR-28 budget)."""
    sql = """SELECT COALESCE(SUM(usd), 0) FROM llm_calls WHERE site_id = ?
             AND ts_utc >= COALESCE(?, ts_utc) AND run_id = COALESCE(?, run_id)"""
    after = since.astimezone(UTC).isoformat() if since else None
    (usd,) = db.execute(sql, (site_id, after, run_id)).fetchone()
    return usd


def add_event(db: sqlite3.Connection, *, site_id: int, event: Event, dedupe: str) -> bool:
    """Save an event. False if one with the same key is already saved."""
    with db:
        cur = db.execute(
            "INSERT OR IGNORE INTO events (site_id, kind, plug_mac, start_utc, facts, dedupe)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (site_id, event.kind, event.plug_mac, event.start.astimezone(UTC).isoformat(),
             json.dumps(event.facts), dedupe),
        )  # fmt: skip
    return cur.rowcount == 1


def load_events(db: sqlite3.Connection, *, site_id: int) -> list[Event]:
    sql = "SELECT kind, plug_mac, start_utc, facts FROM events WHERE site_id = ? ORDER BY id"
    return [_event(*row) for row in db.execute(sql, (site_id,))]


def get_event(db: sqlite3.Connection, *, event_id: int) -> Event | None:
    """One event by its id."""
    sql = "SELECT kind, plug_mac, start_utc, facts FROM events WHERE id = ?"
    row = db.execute(sql, (event_id,)).fetchone()
    return _event(*row) if row else None


def new_events(db: sqlite3.Connection, *, site_id: int, kind: str) -> list[tuple[int, Event]]:
    """Events of one kind that nothing has handled yet, as (id, event), oldest first."""
    sql = """SELECT id, kind, plug_mac, start_utc, facts FROM events
             WHERE site_id = ? AND kind = ? AND status = 'new' ORDER BY id"""
    return [(eid, _event(*row)) for eid, *row in db.execute(sql, (site_id, kind))]


def event_done(db: sqlite3.Connection, *, event_id: int, status: str = "done") -> None:
    """Mark an event handled: done (looked at) or skipped. What came of it is in audit."""
    with db:
        db.execute("UPDATE events SET status = ? WHERE id = ?", (status, event_id))


def recent_events(
    db: sqlite3.Connection, *, site_id: int, mac: str, kind: str, since: datetime
) -> list[tuple[int, str, Event]]:
    """This plug's events of one kind that started since `since`, as (id, status, event)."""
    sql = """SELECT id, status, kind, plug_mac, start_utc, facts FROM events
             WHERE site_id = ? AND plug_mac = ? AND kind = ? AND start_utc >= ? ORDER BY id"""
    rows = db.execute(sql, (site_id, mac, kind, since.astimezone(UTC).isoformat()))
    return [(eid, status, _event(*row)) for eid, status, *row in rows]


def event_failed(db: sqlite3.Connection, *, event_id: int, limit: int) -> bool:
    """Count one failed try. At `limit` tries the event is parked: no more tries. True if so."""
    with db:
        db.execute("UPDATE events SET tries = tries + 1 WHERE id = ?", (event_id,))
        db.execute("UPDATE events SET status = 'parked' WHERE id = ? AND tries >= ?",
                   (event_id, limit))  # fmt: skip
    (status,) = db.execute("SELECT status FROM events WHERE id = ?", (event_id,)).fetchone()
    return status == "parked"


def save_card(db: sqlite3.Connection, *, site_id: int, mac: str, card: Card, now: datetime) -> None:
    """Save the card for the device on a plug, replacing the one before."""
    must = None if card.must_run is None else int(card.must_run)
    with db:
        db.execute(
            "INSERT OR REPLACE INTO cards (plug_mac, site_id, kind, room, what, notes, must_run,"
            " why, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mac, site_id, card.kind, card.room, card.what, card.notes, must,
             json.dumps({str(k): v for k, v in card.why.items()}),
             now.astimezone(UTC).isoformat()),
        )  # fmt: skip


def load_card(db: sqlite3.Connection, *, mac: str) -> Card:
    """The plug's card; an empty one if the owner has said nothing yet."""
    sql = "SELECT kind, room, what, notes, must_run, why FROM cards WHERE plug_mac = ?"
    if (row := db.execute(sql, (mac,)).fetchone()) is None:
        return Card()
    kind, room, what, notes, must, why = row
    return Card(kind, room, what, notes, None if must is None else bool(must),
                {int(k): v for k, v in json.loads(why).items()})  # fmt: skip


def save_automations(
    db: sqlite3.Connection, *, site_id: int, mac: str, automations: Automations, now: datetime
) -> None:
    """Replace what was last read of the plug's own automations."""
    rules = json.dumps([[r.n, r.on, r.text] for r in automations.rules])
    with db:
        db.execute(
            "INSERT OR REPLACE INTO automations (plug_mac, site_id, auto_off_s, rules, read_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (mac, site_id, automations.auto_off_s, rules, now.astimezone(UTC).isoformat()),
        )


def load_automations(db: sqlite3.Connection, *, mac: str) -> Automations:
    """The plug's own automations as last read; none if never read."""
    sql = "SELECT auto_off_s, rules FROM automations WHERE plug_mac = ?"
    if (row := db.execute(sql, (mac,)).fetchone()) is None:
        return Automations(None, ())
    return Automations(row[0], tuple(PlugRule(n, on, text) for n, on, text in json.loads(row[1])))


def add_habit(
    db: sqlite3.Connection, *, site_id: int, mac: str, habit: Habit, now: datetime
) -> int:
    """Save a habit for a plug; return its id."""
    if habit.source not in SOURCES:
        raise ValueError(f"habit source must be one of {', '.join(SOURCES)}, not {habit.source!r}")
    with db:
        cur = db.execute(
            "INSERT INTO habits (site_id, plug_mac, text, days, start, end_hm, source, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (site_id, mac, habit.text, habit.days, habit.start, habit.end, habit.source,
             now.astimezone(UTC).isoformat()),
        )  # fmt: skip
    return cur.lastrowid or 0


def habits(db: sqlite3.Connection, *, mac: str) -> list[tuple[int, Habit]]:
    """A plug's active habits as (id, habit), oldest first."""
    sql = """SELECT id, text, days, start, end_hm, source FROM habits
             WHERE plug_mac = ? AND ended IS NULL ORDER BY id"""
    return [(hid, Habit(*row)) for hid, *row in db.execute(sql, (mac,))]


def end_habit(db: sqlite3.Connection, *, habit_id: int, now: datetime) -> None:
    """The owner says a habit is no longer true. The row stays, for the audit trail."""
    with db:
        db.execute("UPDATE habits SET ended = ? WHERE id = ?",
                   (now.astimezone(UTC).isoformat(), habit_id))  # fmt: skip


def add_question(
    db: sqlite3.Connection, *, site_id: int, event_id: int, mac: str, decision: Decision,
    send_at: datetime, now: datetime,
) -> int | None:  # fmt: skip
    """Save an ask or a proposal for the owner; return its id. Its event is marked done in
    the same transaction, so a restart never asks about the same event twice. None: another
    run handled the event first, so nothing is saved."""
    choices = json.dumps([c.__dict__ for c in decision.choices])
    with db:
        taken = db.execute("UPDATE events SET status = 'done' WHERE id = ? AND status = 'new'",
                           (event_id,)).rowcount  # fmt: skip
        if taken == 0:
            return None
        cur = db.execute(
            "INSERT INTO questions (site_id, event_id, plug_mac, action, text, choices, send_at,"
            " created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (site_id, event_id, mac, decision.action, decision.text, choices,
             send_at.astimezone(UTC).isoformat(), now.astimezone(UTC).isoformat()),
        )  # fmt: skip
    return cur.lastrowid or 0


QUESTION = "SELECT id, event_id, plug_mac, action, text, choices, status, send_at, answer"


def questions(db: sqlite3.Connection, *, site_id: int, status: str | None = None) -> list[Question]:
    """Questions, oldest first: all of them, or one status."""
    sql = f"""{QUESTION} FROM questions
              WHERE site_id = ? AND status = COALESCE(?, status) ORDER BY id"""
    return [_question(*row) for row in db.execute(sql, (site_id, status))]


def get_question(db: sqlite3.Connection, *, question_id: int) -> Question | None:
    row = db.execute(f"{QUESTION} FROM questions WHERE id = ?", (question_id,)).fetchone()
    return _question(*row) if row else None


def count_questions(
    db: sqlite3.Connection, *, site_id: int, start: datetime, end: datetime,
    mac: str | None = None,
) -> int:  # fmt: skip
    """Questions due to reach the owner from start to end: all plugs, or one."""
    sql = """SELECT COUNT(*) FROM questions WHERE site_id = ? AND send_at >= ? AND send_at < ?
             AND plug_mac = COALESCE(?, plug_mac)"""
    return db.execute(sql, (site_id, *utc_bounds(start, end), mac)).fetchone()[0]


def answer_question(
    db: sqlite3.Connection, *, question_id: int, answer: int, now: datetime
) -> bool:
    """Record the answer if the question is still open. False if it was not (a second tap)."""
    with db:
        cur = db.execute("UPDATE questions SET status = 'answered', answer = ?, answered = ?"
                         " WHERE id = ? AND status = 'open'",
                         (answer, now.astimezone(UTC).isoformat(), question_id))  # fmt: skip
    return cur.rowcount == 1


def ask_again(db: sqlite3.Connection, *, question_id: int, at: datetime) -> None:
    """Remind later: the question is open again from `at`."""
    with db:
        db.execute("UPDATE questions SET status = 'open', send_at = ?, sent = NULL WHERE id = ?",
                   (at.astimezone(UTC).isoformat(), question_id))  # fmt: skip


def expire_questions(db: sqlite3.Connection, *, site_id: int, before: datetime) -> int:
    """Open questions due before `before` are stale: expire them. How many."""
    with db:
        cur = db.execute("UPDATE questions SET status = 'expired' WHERE site_id = ?"
                         " AND status = 'open' AND send_at < ?",
                         (site_id, before.astimezone(UTC).isoformat()))  # fmt: skip
    return cur.rowcount


def _question(qid: int, eid: int, mac: str, action: str, text: str, choices: str, status: str,
              send_at: str, answer: int | None) -> Question:  # fmt: skip
    d = Decision(action, text, tuple(Choice(**c) for c in json.loads(choices)))
    return Question(qid, eid, mac, d, status, datetime.fromisoformat(send_at), answer)


def due_questions(db: sqlite3.Connection, *, site_id: int, now: datetime) -> list[Question]:
    """Open questions due by now and not sent yet, oldest first."""
    sql = f"""{QUESTION} FROM questions WHERE site_id = ? AND status = 'open' AND sent IS NULL
              AND send_at <= ? ORDER BY id"""
    return [_question(*row) for row in db.execute(sql, (site_id, now.astimezone(UTC).isoformat()))]


def mark_sent(db: sqlite3.Connection, *, question_ids: list[int], now: datetime) -> None:
    with db:
        db.executemany("UPDATE questions SET sent = ? WHERE id = ?",
                       [(now.astimezone(UTC).isoformat(), q) for q in question_ids])  # fmt: skip


def close_question(db: sqlite3.Connection, *, question_id: int) -> None:
    """Expire one open question: it no longer needs an answer (e.g. off by morning)."""
    with db:
        db.execute("UPDATE questions SET status = 'expired' WHERE id = ? AND status = 'open'",
                   (question_id,))  # fmt: skip


def audit_since(
    db: sqlite3.Connection, *, site_id: int, action: str, since: datetime
) -> list[dict[str, str]]:
    """The detail of each audit row of one action since a time, oldest first."""
    sql = """SELECT detail FROM audit WHERE site_id = ? AND action = ? AND ts_utc >= ?
             ORDER BY id"""
    rows = db.execute(sql, (site_id, action, since.astimezone(UTC).isoformat()))
    return [json.loads(d) for (d,) in rows]


def count_events(db: sqlite3.Connection, *, site_id: int, status: str) -> int:
    sql = "SELECT COUNT(*) FROM events WHERE site_id = ? AND status = ?"
    return db.execute(sql, (site_id, status)).fetchone()[0]


def _event(kind: str, mac: str | None, start: str, facts: str) -> Event:
    return Event(kind, mac, datetime.fromisoformat(start), json.loads(facts))


def last_run(db: sqlite3.Connection, *, site_id: int, job: str) -> datetime | None:
    """When a job last ran, or None if it never has."""
    sql = "SELECT value FROM settings WHERE site_id = ? AND key = ?"
    row = db.execute(sql, (site_id, f"last_run:{job}")).fetchone()
    return datetime.fromisoformat(row[0]) if row else None


def set_last_run(db: sqlite3.Connection, *, site_id: int, job: str, at: datetime) -> None:
    with db:
        db.execute(
            "INSERT OR REPLACE INTO settings (site_id, key, value) VALUES (?, ?, ?)",
            (site_id, f"last_run:{job}", at.isoformat()),
        )


def last_event_start(
    db: sqlite3.Connection, *, site_id: int, kind: str, plug_mac: str | None
) -> datetime | None:
    """When the latest event of this kind for this plug started (plug None = the site)."""
    sql = "SELECT MAX(start_utc) FROM events WHERE site_id = ? AND kind = ? AND plug_mac IS ?"
    (start,) = db.execute(sql, (site_id, kind, plug_mac)).fetchone()
    return datetime.fromisoformat(start) if start else None


# chats: the setup chat (and later the main chat)


def open_chat(db: sqlite3.Connection, *, site_id: int, mac: str, kind: str, now: datetime) -> int:
    """The plug's open chat of this kind, or a new one. Returns its id."""
    with db:
        row = db.execute("SELECT id FROM chats WHERE plug_mac = ? AND kind = ? AND status = 'open'"
                         " ORDER BY id DESC LIMIT 1", (mac, kind)).fetchone()  # fmt: skip
        if row:
            return row[0]
        cur = db.execute("INSERT INTO chats (site_id, plug_mac, kind, created) VALUES (?, ?, ?, ?)",
                         (site_id, mac, kind, now.astimezone(UTC).isoformat()))  # fmt: skip
    return cur.lastrowid or 0


def add_turn(db: sqlite3.Connection, *, chat_id: int, site_id: int, role: str, text: str,
             now: datetime) -> int:  # fmt: skip
    with db:
        at = now.astimezone(UTC).isoformat()
        cur = db.execute("INSERT INTO chat_turns (chat_id, site_id, role, text, ts_utc)"
                         " VALUES (?, ?, ?, ?, ?)", (chat_id, site_id, role, text, at))  # fmt: skip
    return cur.lastrowid or 0


def turns(db: sqlite3.Connection, *, chat_id: int) -> list[tuple[int, str, str]]:
    """A chat's messages as (id, role, text), oldest first."""
    sql = "SELECT id, role, text FROM chat_turns WHERE chat_id = ? ORDER BY id"
    return [tuple(r) for r in db.execute(sql, (chat_id,))]  # type: ignore[misc]


def set_draft(db: sqlite3.Connection, *, chat_id: int, draft: dict[str, Any] | None) -> None:
    with db:
        db.execute("UPDATE chats SET draft = ? WHERE id = ?",
                   (json.dumps(draft) if draft is not None else None, chat_id))  # fmt: skip


@dataclass(frozen=True)
class Chat:
    plug_mac: str
    status: str  # open, saved or closed
    draft: dict[str, Any] | None  # the card the agent proposed last; None: none yet


def chat(db: sqlite3.Connection, *, chat_id: int) -> Chat | None:
    """One chat, or None."""
    row = db.execute(
        "SELECT plug_mac, status, draft FROM chats WHERE id = ?", (chat_id,)
    ).fetchone()
    return Chat(row[0], row[1], json.loads(row[2]) if row[2] else None) if row else None


def close_chat(db: sqlite3.Connection, *, chat_id: int, status: str) -> None:
    """saved: the owner saved the draft. closed: started again, or left."""
    with db:
        db.execute(
            "UPDATE chats SET status = ? WHERE id = ? AND status = 'open'", (status, chat_id)
        )
