"""Public homes: the Watcher and the fixes with fixed values and with learned ones (B3b).

Data: REFIT Electrical Load Measurements (Cleaned), Zenodo record 5063428, CC BY 4.0.
Download CLEAN_House1.csv, CLEAN_House4.csv and CLEAN_House11.csv into data/refit/
(gitignored), then run: uv run python evals/refit.py

For each appliance it prints its learned levels, then for fixed and learned values:
unusual runs a week, and how many of them get a fix offered.
"""

import csv
import sys
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentc import calc, config, learn, responder, watcher
from agentc.config import Device
from agentc.options import Money
from agentc.policy import DEFAULT, Policy

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "refit"
TZ = "Europe/London"
START = datetime(2014, 9, 1, tzinfo=UTC)  # a month every house has
DAYS = 28
CHANNELS = {  # Appliance1 to Appliance9, from NILMTK's REFIT metadata
    1: ["Fridge", "Freezer (1)", "Freezer (2)", "Washer dryer", "Washing machine",
        "Dishwasher", "Computer", "TV site", "Electric heater"],
    4: ["Fridge", "Freezer", "Fridge-freezer", "Tumble dryer", "Washing machine (1)",
        "Washing machine (2)", "TV site", "Microwave", "Kettle"],
    11: ["Fridge", "Fridge-freezer", "Washing machine", "Dishwasher", "Computer site",
         "Microwave", "Kettle", "Router", "Hi-fi"],
}  # fmt: skip


def minutes(house: int) -> dict[int, list[tuple[datetime, float]]]:
    """Each channel's readings in the month, as one value a minute (the mean of that minute)."""
    first, last = START.timestamp(), (START + timedelta(days=DAYS)).timestamp()
    sums: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    with (DATA / f"CLEAN_House{house}.csv").open() as f:
        for row in csv.DictReader(f):
            t = int(row["Unix"])
            if t < first:
                continue
            if t >= last:
                break
            for ch in range(1, 10):
                sums[ch][t // 60].append(float(row[f"Appliance{ch}"]))
    return {ch: [(_at(m), sum(ws) / len(ws)) for m, ws in sorted(by.items())]
            for ch, by in sums.items()}  # fmt: skip


def _at(minute: int) -> datetime:
    return datetime.fromtimestamp(minute * 60, UTC)


def score(readings: list[tuple[datetime, float]], name: str, policy: Policy) -> tuple[float, int]:
    """Unusual runs a week, and how many get a fix offered, with this policy."""
    cfg = config.load(ROOT / "config.example.toml")
    money = Money(cfg.tariff, cfg.month_kwh, cfg.levy, cfg.factor)
    usual = watcher.usual_week(calc.build_profile(readings, tz=TZ, policy=policy) or [])
    runs = watcher.unusual_runs(readings, usual, tz=TZ, policy=policy)
    device = Device(name, policy=policy)
    fixed = [
        r for r in runs
        if responder.gather(r, mac=name, device=device, readings=readings, usual=usual,
                            timers=[], money=money, rejected=[], tz=TZ, days=DAYS)
    ]  # fmt: skip
    return len(runs) * 7 / DAYS, len(fixed)


def main() -> int:
    print("| House | Appliance | Levels (standby, active W) | Runs a week: fixed, learned"
          " | Runs with a fix: fixed, learned |")  # fmt: skip
    print("|---|---|---|---|---|")
    for house, names in CHANNELS.items():
        if not (DATA / f"CLEAN_House{house}.csv").exists():
            print(f"House {house}: no data file; see the docstring", file=sys.stderr)
            return 1
        for ch, readings in sorted(minutes(house).items()):
            name = names[ch - 1]
            lv = learn.levels([w for _, w in readings])
            values = {k: v for k, (v, _) in learn.values(lv).items()} if lv else {}
            learned = replace(DEFAULT, **values)
            a, fa = score(readings, name, DEFAULT)
            b, fb = score(readings, name, learned)
            levels = f"{lv.standby:g}, {lv.active:g}" if lv else "one level"
            print(f"| {house} | {name} | {levels} | {a:.1f}, {b:.1f} | {fa}, {fb} |", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
