import http.client
import json
import sqlite3
import sys
import time
import tomllib
import urllib.request
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

# ts_utc, plug, plug_time, 7 energy readings, error (None when the read worked)
Row = tuple[str, str, str | None, *tuple[float | None, ...], str | None]


def open_db(path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    return db


def load_plugs(config_path: str | Path) -> dict[str, str]:
    with open(config_path, "rb") as f:
        return tomllib.load(f)["plugs"]


def read_plug(name: str, ip: str, ts_utc: str) -> Row:
    url = f"http://{ip}/cm?cmnd=Status%208"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as resp:
            sns = json.load(resp)["StatusSNS"]
        e = sns["ENERGY"]
        return (
            ts_utc,
            name,
            sns["Time"],
            e["Power"],
            e["ApparentPower"],
            e["ReactivePower"],
            e["Factor"],
            e["Voltage"],
            e["Current"],
            e["Total"],
            None,
        )
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as err:
        return (
            ts_utc,
            name,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            f"{type(err).__name__}: {err}",
        )


def poll_all(plugs: dict[str, str], ts_utc: str) -> list[Row]:
    with ThreadPoolExecutor(max_workers=len(plugs)) as pool:
        return list(pool.map(lambda p: read_plug(p[0], p[1], ts_utc), plugs.items()))


def main(db_path: str, config_path: str) -> None:
    plugs = load_plugs(config_path)
    db = open_db(db_path)
    tick = 0
    while True:
        tick = max(tick + INTERVAL_S, (time.time() // INTERVAL_S + 1) * INTERVAL_S)
        time.sleep(max(0, tick - time.time()))
        ts_utc = datetime.fromtimestamp(tick, UTC).isoformat()
        rows = poll_all(plugs, ts_utc)
        with db:
            db.executemany(INSERT, rows)
        failed = [r[1] for r in rows if r[10]]
        print(ts_utc, f"ok={len(rows) - len(failed)}", f"failed={failed}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
