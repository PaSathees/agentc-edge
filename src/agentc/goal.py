"""The monthly carbon goal for the metered plugs (FR-22, FR-10; ADR-003 revised).

Code only. The month so far is what the plugs drew, from their readings. The rest of the
month is each plug's average power for each hour of the week over the last 14 days, for
the hours still to come. Status: on track if the projection is under at_risk x goal, at
risk up to the goal, off track over it. The owner sets the goal in config.toml; no agent
can change it.
"""

import calendar
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from sqlite3 import Connection
from zoneinfo import ZoneInfo

from agentc import readings
from agentc.agent_tools import used_kwh
from agentc.calc import kgco2e
from agentc.config import Config

HISTORY = timedelta(days=14)  # the average week comes from this many days


@dataclass(frozen=True)
class Status:
    month: str  # "2026-11"
    used_kwh: float  # so far this month
    used_kg: float
    projected_kwh: float  # by the month's end
    projected_kg: float
    goal_kg: float
    state: str  # on track, at risk or off track
    at: str  # when this was worked out (UTC ISO)

    @property
    def gap_kg(self) -> float:
        """How far the projection is over the goal; below 0, how far under."""
        return self.projected_kg - self.goal_kg

    def text(self, *, tz: str) -> str:
        """One line for the owner: "November: 1.2 of 4.0 kgCO2e so far (2.9 kWh); heading
        to 3.8 kgCO2e by 30 Nov: on track." """
        y, m = map(int, self.month.split("-"))
        name, last = calendar.month_name[m], calendar.monthrange(y, m)[1]
        over = f", {self.gap_kg:.2f} over" if self.gap_kg > 0 else ""  # 0.04, never "0.0 over"
        return (f"{name}: {self.used_kg:.1f} of {self.goal_kg:.1f} kgCO2e so far "
                f"({self.used_kwh:.1f} kWh); heading to {self.projected_kg:.1f} kgCO2e by "
                f"{last} {calendar.month_abbr[m]}{over}: {self.state}.")  # fmt: skip

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "Status":
        return cls(**json.loads(raw))


def month_name(month: str) -> str:
    """ "2026-11" as "November" """
    return calendar.month_name[int(month.split("-")[1])]


def month(energy: Connection, cfg: Config, *, now: datetime) -> tuple[str, float, float]:
    """The month ("2026-11"), its kWh so far on all plugs, and its kWh by the month's end."""
    local = now.astimezone(ZoneInfo(cfg.tz))
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1)
    used = sum(used_kwh(energy, plug, start, now) for plug in cfg.plugs)
    expected = sum(_expected(energy, plug, now=now, end=end, tz=cfg.tz) for plug in cfg.plugs)
    return f"{start:%Y-%m}", used, used + expected


def status(energy: Connection, cfg: Config, *, now: datetime) -> Status | None:
    """The month's status, or None without a goal (or without a carbon factor to count it)."""
    if cfg.goal is None or cfg.factor is None:
        return None
    name, used, projected = month(energy, cfg, now=now)
    used_kg, projected_kg = kgco2e(used, cfg.factor), kgco2e(projected, cfg.factor)
    goal = cfg.goal.month_kg
    state = ("off track" if projected_kg > goal else
             "at risk" if projected_kg > cfg.goal.at_risk * goal else "on track")  # fmt: skip
    return Status(name, used, used_kg, projected, projected_kg, goal, state, now.isoformat())


def _expected(energy: Connection, plug: str, *, now: datetime, end: datetime, tz: str) -> float:
    """kWh the plug will likely draw from now to `end`: its average watts for each hour of
    the week over the last 14 days, hour by hour. A plug with no readings adds nothing."""
    zone = ZoneInfo(tz)
    sums: dict[int, float] = defaultdict(float)
    counts: dict[int, int] = defaultdict(int)
    for at, w in readings.power(energy, plug=plug, start=now - HISTORY, end=now):
        local = at.astimezone(zone)
        how = local.weekday() * 24 + local.hour
        sums[how] += w
        counts[how] += 1
    if not counts:
        return 0.0
    overall = sum(sums.values()) / sum(counts.values())  # an hour never seen: the plug's average
    # Hours are stepped on the local clock. Right for zones without daylight saving (like
    # Asia/Colombo); a zone with it would be off by an hour on its two change days.
    kwh, hour = 0.0, now.astimezone(zone)
    while hour < end:
        nxt = min((hour + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0), end)
        how = hour.weekday() * 24 + hour.hour
        watts = sums[how] / counts[how] if counts.get(how) else overall
        kwh += watts * (nxt - hour).total_seconds() / 3600 / 1000
        hour = nxt
    return kwh
