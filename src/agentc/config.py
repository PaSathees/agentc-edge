"""config.toml: the site, its plugs, file paths and tariff. The real file is never committed."""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agentc.calc import Band, Factor, Period, Schedule, TimeOfUse, month_lkr
from agentc.policy import DEFAULT, Policy, override

Tariff = tuple[Schedule, ...] | TimeOfUse

Effort = Literal["low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True)
class Tier:
    """A model size: small for the Responder and Assistant, mid for the Planner (ADR-005)."""

    model: str
    usd_in: float  # USD per million tokens in, from the provider's price page
    usd_out: float  # USD per million tokens out
    effort: Effort  # how hard the model thinks; more effort costs more tokens


@dataclass(frozen=True)
class Budget:
    month_usd: float  # all model calls in a calendar month (NFR-01)
    run_usd: float  # one agent run


TIERS = {
    "small": Tier("claude-haiku-5-5", 0.10, 0.50, "low"),
    "mid": Tier("claude-sonnet-5-5", 2.0, 10.0, "medium"),
}
BUDGET = Budget(month_usd=3.0, run_usd=0.05)
ACCESS = ("none", "advice", "write")  # never changed; proposals only; written after approval
SLOTS = tuple(range(9, 17))  # timers the system may use; 1 to 8 stay the owner's


@dataclass(frozen=True)
class Rule:
    kind: str  # "latest_off": the device may run until `time`; other kinds come later
    time: str  # local HH:MM
    hard: bool  # a hard rule blocks a draft; a soft one adds a warning
    text: str  # the owner's own words, shown with a warning


@dataclass(frozen=True)
class Device:
    """What is plugged into a plug, and what the system may do with it (FR-04)."""

    name: str  # the owner's name; the only name a model sees
    access: str = "advice"  # new devices start at advice
    slots: tuple[int, ...] = SLOTS
    rules: tuple[Rule, ...] = ()
    policy: Policy = DEFAULT  # the site's, with this device's own values on top


@dataclass(frozen=True)
class Config:
    site: str
    tz: str  # IANA name, e.g. "Asia/Colombo"
    plugs: dict[str, str]  # name -> IP
    tariff: Tariff
    tariff_source: str  # where the rates came from, shown beside every LKR figure
    levy: float  # tax on the whole bill, e.g. 0.025 for 2.5%; 0 if none
    month_kwh: float  # the whole house's units in a 30-day month, from the bill
    factor: Factor  # the grid's kgCO2e per kWh, with its source
    tiers: dict[str, Tier]
    budget: Budget
    devices: dict[str, Device]  # plug name -> device; a plug not listed is a Device of its name
    policy: Policy  # the site's thresholds: the defaults, with [policy] on top
    app_db: Path
    energy_db: Path


def load(path: str | Path) -> Config:
    """Read config.toml; a bad time zone or tariff fails here, not on the first bill."""
    path = Path(path)
    with path.open("rb") as f:
        raw = tomllib.load(f)
    site, paths, tariff, carbon = raw["site"], raw.get("paths", {}), raw["tariff"], raw["carbon"]
    llm = raw.get("llm", {})  # model calls: optional, defaults above
    site_policy = override(DEFAULT, raw.get("policy", {}), where="[policy]")
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
        levy=tariff.get("levy", 0.0),
        month_kwh=site["month_kwh"],
        factor=Factor(carbon["id"], carbon["factor"], carbon["source"], carbon["valid_from"]),
        tiers={**TIERS, **{k: _tier(t) for k, t in llm.get("tiers", {}).items()}},
        budget=Budget(llm.get("month_usd", BUDGET.month_usd), llm.get("run_usd", BUDGET.run_usd)),
        devices={plug: _device(plug, d, site_policy) for plug, d in raw.get("devices", {}).items()},
        policy=site_policy,
        app_db=path.parent / paths.get("app_db", "app.db"),
        energy_db=path.parent / paths.get("energy_db", "energy.db"),
    )


def _device(plug: str, d: dict[str, Any], site: Policy) -> Device:
    if (access := d.get("access", "advice")) not in ACCESS:
        raise ValueError(f"access must be one of {', '.join(ACCESS)}, not {access!r}")
    rules = tuple(Rule(**r) for r in d.get("rules", []))
    policy = override(site, d.get("policy", {}), where=f'[devices."{plug}".policy]')
    return Device(d["name"], access, tuple(d.get("slots", SLOTS)), rules, policy)


def device(cfg: Config, plug: str) -> Device:
    """The device on a plug, by the plug's name. A plug not listed gets the site's policy."""
    return cfg.devices.get(plug, Device(plug, policy=cfg.policy))


def _tier(t: dict[str, Any]) -> Tier:
    if (effort := t["effort"]) not in get_args(Effort):
        raise ValueError(f"effort must be one of {', '.join(get_args(Effort))}, not {effort!r}")
    return Tier(**t)


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
