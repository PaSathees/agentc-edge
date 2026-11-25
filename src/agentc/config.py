"""config.toml: the site, its plugs, file paths and tariff. The real file is never committed.

Only [site] (name, time zone) and [plugs] are needed to start; the rest is optional.
"""

import os
import secrets
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agentc.calc import Band, Factor, Period, Schedule, TimeOfUse, month_lkr
from agentc.policy import DEFAULT, Policy, override

Tariff = tuple[Schedule, ...] | TimeOfUse

Effort = Literal["low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True)
class Tier:
    """A model size: small for the housemate and chat, mid for the Planner (ADR-005)."""

    model: str
    usd_in: float  # USD per million tokens in, from the provider's price page
    usd_out: float  # USD per million tokens out
    effort: Effort  # how hard the model thinks; more effort costs more tokens


@dataclass(frozen=True)
class Budget:
    month_usd: float  # all model calls in a calendar month (NFR-01)
    run_usd: float  # one agent run
    plan_usd: float = 0.25  # one weekly Planner run: it reads the whole house on a bigger model


TIERS = {
    "small": Tier("claude-haiku-5-5", 0.10, 0.50, "low"),
    "mid": Tier("claude-sonnet-5-5", 2.0, 10.0, "medium"),
}
BUDGET = Budget(month_usd=3.0, run_usd=0.05, plan_usd=0.25)
ACCESS = ("none", "advice", "write")  # never changed; proposals only; written after approval
SLOTS = tuple(range(9, 17))  # timers the system may use; 1 to 8 stay the owner's


@dataclass(frozen=True)
class Owner:
    """Where messages to the owner go (FR-31: questions on the phone), from .env.

    AGENTC_HUB is how the phone reaches the hub: http://192.168.1.50 at home, http://100.x.y.z
    with Tailscale, or https://NAME.ts.net behind Tailscale Serve."""

    hub: str  # no port, no trailing slash
    topic: str = ""  # the ntfy topic; empty: made from the secret, so nobody can guess it
    ntfy_url: str = "http://127.0.0.1:8090"  # where this app publishes: ntfy in the same stack

    @property
    def inbox_url(self) -> str:
        """The inbox and the buttons: port 8765 at home; Tailscale Serve puts it on 443."""
        return self.hub if self.hub.startswith("https://") else f"{self.hub}:8765"

    @property
    def phone_ntfy(self) -> str:
        """The ntfy server as the phone adds it: 8090 at home, 8443 through Tailscale Serve."""
        return f"{self.hub}:8443" if self.hub.startswith("https://") else f"{self.hub}:8090"


def logins() -> tuple[str, frozenset[str]]:
    """Who may open the web pages, from .env: the password, and the Tailscale logins.
    With neither, the pages are open, so nothing is ever written to a plug."""
    users = os.environ.get("AGENTC_TAILSCALE_USER", "")
    return (os.environ.get("AGENTC_PASSWORD", ""),
            frozenset(u.strip().lower() for u in users.split(",") if u.strip()))  # fmt: skip


def owner() -> Owner | None:
    """The phone's settings from the environment (.env). None: AGENTC_HUB not set."""
    if not (hub := os.environ.get("AGENTC_HUB", "").rstrip("/")):
        return None
    return Owner(hub, os.environ.get("AGENTC_TOPIC", ""),
                 os.environ.get("AGENTC_NTFY_URL", "http://127.0.0.1:8090"))  # fmt: skip


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
    policy: Policy = DEFAULT  # the defaults, then learned, then the site's, then its own
    own: frozenset[str] = frozenset()  # policy names set in this device's own section
    learned: dict[str, str] = field(default_factory=dict)  # names learned, with why


@dataclass(frozen=True)
class Config:
    site: str
    tz: str  # IANA name, e.g. "Asia/Colombo"
    plugs: dict[str, str]  # name -> IP
    tariff: Tariff | None  # None: costs in kWh only
    tariff_source: str  # where the rates came from, shown beside every LKR figure
    levy: float  # tax on the whole bill, e.g. 0.025 for 2.5%; 0 if none
    month_kwh: float  # the whole house's units in a 30-day month, from the bill
    factor: Factor | None  # the grid's kgCO2e per kWh, with its source; None: no carbon
    tiers: dict[str, Tier]
    budget: Budget
    devices: dict[str, Device]  # plug name -> device; a plug not listed is a Device of its name
    policy: Policy  # the site's thresholds: the defaults, with [policy] on top
    own: frozenset[str]  # policy names set in [policy]
    app_db: Path
    energy_db: Path
    owner: Owner | None = None  # None: no AGENTC_HUB, so no messages; questions wait in the inbox


def load(path: str | Path) -> Config:
    """Read config.toml; a bad time zone or tariff fails here, not on the first bill."""
    path = Path(path)
    with path.open("rb") as f:
        raw = tomllib.load(f)
    site, paths, tariff, carbon = (
        raw["site"],
        raw.get("paths", {}),
        raw.get("tariff"),
        raw.get("carbon"),
    )
    data = Path(os.environ.get("AGENTC_DATA", path.parent))  # the data folder: the database files
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
        tariff=_tariff(tariff) if tariff else None,
        tariff_source=tariff["source"] if tariff else "",
        levy=tariff.get("levy", 0.0) if tariff else 0.0,
        month_kwh=(tariff or {}).get(
            "month_kwh", site.get("month_kwh", 0.0)
        ),  # under [site] in older configs
        factor=Factor(carbon["id"], carbon["factor"], carbon["source"], carbon["valid_from"])
        if carbon
        else None,
        tiers={**TIERS, **{k: _tier(t) for k, t in llm.get("tiers", {}).items()}},
        budget=Budget(
            llm.get("month_usd", BUDGET.month_usd),
            llm.get("run_usd", BUDGET.run_usd),
            llm.get("plan_usd", BUDGET.plan_usd),
        ),
        devices={plug: _device(plug, d, site_policy) for plug, d in raw.get("devices", {}).items()},
        policy=site_policy,
        own=frozenset(raw.get("policy", {})),
        app_db=data / paths.get("app_db", "app.db"),
        energy_db=data / paths.get("energy_db", "energy.db"),
        owner=owner(),
    )


def secret(cfg: Config) -> str:
    """The key that signs the phone's buttons: AGENTC_SECRET if set, else a file in the data
    folder, made on first use. So nobody has to make one."""
    if s := os.environ.get("AGENTC_SECRET", ""):
        return s
    path = cfg.app_db.parent / "secret"
    try:  # made in one step, readable by the owner only; a second process just reads it
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    with os.fdopen(fd, "w") as f:
        f.write(key := secrets.token_hex(32))
    return key


def _device(plug: str, d: dict[str, Any], site: Policy) -> Device:
    if (access := d.get("access", "advice")) not in ACCESS:
        raise ValueError(f"access must be one of {', '.join(ACCESS)}, not {access!r}")
    slots = tuple(d.get("slots", SLOTS))
    if len(set(slots)) != len(slots) or not all(isinstance(s, int) and 1 <= s <= 16 for s in slots):
        raise ValueError(f'[devices."{plug}"] slots must be different numbers from 1 to 16')
    rules = tuple(Rule(**r) for r in d.get("rules", []))
    own = d.get("policy", {})
    policy = override(site, own, where=f'[devices."{plug}".policy]')
    return Device(d["name"], access, slots, rules, policy, frozenset(own))


def device(
    cfg: Config, plug: str, learned: Mapping[str, tuple[float, str]] | None = None
) -> Device:
    """The device on a plug, by the plug's name. A plug not listed gets the site's policy.

    learned: the plug's learned values (name -> value, why). Each fills in only what the
    owner did not set, in [policy] or in the device's own section.
    """
    d = cfg.devices.get(plug, Device(plug, policy=cfg.policy))
    fill = {k: v for k, v in (learned or {}).items() if k not in cfg.own | d.own}
    if not fill:
        return d
    policy = replace(d.policy, **{k: value for k, (value, _) in fill.items()})
    return replace(d, policy=policy, learned={k: why for k, (_, why) in fill.items()})


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
