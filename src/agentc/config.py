"""config.toml: the site, its plugs, file paths and tariff. The real file is never committed."""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agentc.calc import Band, Period, Schedule, TimeOfUse, month_lkr

Tariff = tuple[Schedule, ...] | TimeOfUse


@dataclass(frozen=True)
class Config:
    site: str
    tz: str  # IANA name, e.g. "Asia/Colombo"
    plugs: dict[str, str]  # name -> IP
    tariff: Tariff
    tariff_source: str  # where the rates came from, shown beside every LKR figure
    app_db: Path
    energy_db: Path


def load(path: str | Path) -> Config:
    """Read config.toml; a bad time zone or tariff fails here, not on the first bill."""
    path = Path(path)
    with path.open("rb") as f:
        raw = tomllib.load(f)
    site, paths, tariff = raw["site"], raw.get("paths", {}), raw["tariff"]
    try:
        ZoneInfo(site["tz"])
    except ZoneInfoNotFoundError:
        raise ValueError(f"unknown time zone {site['tz']!r} in {path}") from None
    return Config(
        site=site["name"],
        tz=site["tz"],
        plugs=raw["plugs"],
        tariff=_tariff(tariff),
        tariff_source=tariff["source"],
        app_db=path.parent / paths.get("app_db", "app.db"),
        energy_db=path.parent / paths.get("energy_db", "energy.db"),
    )


def _tariff(t: dict[str, Any]) -> Tariff:
    if t["kind"] == "time_of_use":
        periods = tuple(Period(p["start"], p["rate"]) for p in t["periods"])
        return TimeOfUse(periods, fixed=t.get("fixed", 0.0))
    if t["kind"] != "block":
        raise ValueError(f"tariff kind must be 'block' or 'time_of_use', not {t['kind']!r}")
    schedules = tuple(
        Schedule(s.get("up_to"), tuple(Band(b.get("up_to"), b["rate"], b.get("fixed", 0.0))
                                       for b in s["bands"]))
        for s in t["schedules"]
    )  # fmt: skip
    month_lkr(1e9, schedules)  # a table that can't price a huge month raises now
    return schedules
