"""The logger: one reading from every plug each minute into energy.db (`agentc log`).

Standard library only and apart from the rest of the app, so it keeps recording when
anything else breaks. The app only ever reads energy.db (ADR-006).
"""

import http.client
import json
import sqlite3
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    ts_utc          TEXT NOT NULL,
    plug            TEXT NOT NULL,
    plug_time       TEXT,
    power_w         REAL,
    apparent_va     REAL,
    reactive_var    REAL,
    factor          REAL,
    voltage_v       REAL,
    current_a       REAL,
    total_kwh       REAL,
    error           TEXT,
    PRIMARY KEY (ts_utc, plug)
)
"""
TIMEOUT_S = 5
INTERVAL_S = 60
INSERT = "INSERT OR IGNORE INTO readings VALUES (?,?,?,?,?,?,?,?,?,?,?)"
ENERGY = ("Power", "ApparentPower", "ReactivePower", "Factor", "Voltage", "Current", "Total")

# ts_utc, plug, plug_time, 7 energy readings, error (None when the read worked)
Row = tuple[str, str, str | None, *tuple[float | None, ...], str | None]


def open_db(path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    return db


def read_plug(name: str, ip: str, ts_utc: str, *, timeout_s: float = TIMEOUT_S) -> Row:
    """One plug's energy readings (Tasmota `Status 8`). A failed read is a row with an error."""
    url = f"http://{ip}/cm?cmnd=Status%208"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            sns = json.load(resp)["StatusSNS"]
        e = sns["ENERGY"]
        return (ts_utc, name, sns["Time"], *(e[k] for k in ENERGY), None)
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as err:
        return (ts_utc, name, None, *([None] * len(ENERGY)), f"{type(err).__name__}: {err}")


def poll_all(plugs: dict[str, str], ts_utc: str, *, timeout_s: float = TIMEOUT_S) -> list[Row]:
    """Every plug at once, so one slow plug never delays the minute."""
    with ThreadPoolExecutor(max_workers=max(len(plugs), 1)) as pool:
        return list(pool.map(lambda p: read_plug(p[0], p[1], ts_utc, timeout_s=timeout_s),
                             plugs.items()))  # fmt: skip


def run(
    db_path: str | Path, plugs: dict[str, str], *, rounds: int | None = None,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
) -> None:  # fmt: skip
    """Read every plug on each whole minute, forever (or `rounds` times, for tests)."""
    db, tick, done = open_db(db_path), 0.0, 0
    while rounds is None or done < rounds:
        tick = max(tick + INTERVAL_S, (clock() // INTERVAL_S + 1) * INTERVAL_S)
        sleep(max(0.0, tick - clock()))
        ts_utc = datetime.fromtimestamp(tick, UTC).isoformat()
        rows = poll_all(plugs, ts_utc)
        with db:
            db.executemany(INSERT, rows)
        failed = [r[1] for r in rows if r[-1]]
        print(ts_utc, f"ok={len(rows) - len(failed)}", f"failed={failed}", flush=True)
        done += 1
