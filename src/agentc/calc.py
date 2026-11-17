"""All the maths: usage profiles; kWh, LKR and kgCO2e. Done in code, never by the AI (FR-02, FR-23).

Numbers that reach the owner or a model are computed here, never by a model.
"""

import itertools
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from agentc.policy import DEFAULT, Policy

BILL_DAYS = 30  # tariff bands are set for a 30-day bill


@dataclass(frozen=True)
class ProfileHour:
    hour: int  # hour of the week in site local time: 0 = Monday 00:00, 167 = Sunday 23:00
    median_w: float
    on_share: float  # share of the minutes above the policy's on_w, 0 to 1
    days_used: int  # how many separate days fed this hour


def build_profile(
    readings: list[tuple[datetime, float]], *, tz: str, policy: Policy = DEFAULT
) -> list[ProfileHour] | None:
    """A plug's usual power for each hour of the week, or None under min_days of data (FR-02)."""
    zone = ZoneInfo(tz)
    watts: dict[int, list[float]] = defaultdict(list)  # hour of week -> every reading in it
    days: dict[int, set[str]] = defaultdict(set)  # hour of week -> the dates that fed it
    for ts, w in readings:
        local = ts.astimezone(zone)
        hour = local.weekday() * 24 + local.hour  # weekday(): Monday = 0
        watts[hour].append(w)
        days[hour].add(local.date().isoformat())
    if len(set().union(*days.values())) < policy.min_days:
        return None
    return [
        ProfileHour(
            hour=h,
            median_w=statistics.median(ws),
            on_share=sum(w > policy.on_w for w in ws) / len(ws),
            days_used=len(days[h]),
        )
        for h, ws in sorted(watts.items())
    ]


def kwh(counter: list[tuple[datetime, float]]) -> float:
    """Energy used, from a plug's meter readings (oldest first).

    Adds only the rises: after a power cut the meter restarts from its last save,
    so a drop is skipped, not subtracted.
    """
    return sum(max(b - a, 0.0) for (_, a), (_, b) in itertools.pairwise(counter))


@dataclass(frozen=True)
class Band:
    up_to: float | None  # kWh in the month where this band ends; None = no limit
    rate: float  # LKR per kWh
    fixed: float = 0.0  # LKR a month, charged when this is the highest band reached


@dataclass(frozen=True)
class Schedule:
    up_to: float | None  # used when the month's total is at most this; None = no limit
    bands: tuple[Band, ...]


def month_lkr(
    month_kwh: float, tariff: tuple[Schedule, ...], *, days: int = BILL_DAYS, levy: float = 0.0
) -> float:
    """The bill under a block tariff: the total picks the schedule, then band by band.

    days: the bill's length. The bands grow with it: on a 35-day bill a 60-unit
    band ends at 70. The fixed charge stays the same.
    levy: a tax on the whole bill, e.g. 0.025 for 2.5%. The tax is counted in the
    total it is charged on, so total = charge / (1 - levy).
    """
    scale = days / BILL_DAYS
    schedule = next((s for s in tariff if s.up_to is None or month_kwh <= s.up_to * scale), None)
    if schedule is None:
        raise ValueError("the last schedule must have no limit (up_to=None)")
    cost, low = 0.0, 0.0
    for band in schedule.bands:
        high = math.inf if band.up_to is None else band.up_to * scale
        cost += (min(month_kwh, high) - low) * band.rate  # the units that fall in this band
        if month_kwh <= high:
            return (cost + band.fixed) / (1 - levy)
        low = high
    raise ValueError("the last band must have no limit (up_to=None)")


@dataclass(frozen=True)
class Period:
    start: str  # local "HH:MM" it begins; it runs until the next period starts
    rate: float  # LKR per kWh


@dataclass(frozen=True)
class TimeOfUse:
    periods: tuple[Period, ...]
    fixed: float = 0.0  # LKR a month


def tou_rate(tariff: TimeOfUse, at: datetime, *, tz: str) -> float:
    """LKR per kWh at a moment: the period that began most recently, wrapping past midnight."""
    hhmm = at.astimezone(ZoneInfo(tz)).strftime("%H:%M")
    started = [p for p in tariff.periods if p.start <= hhmm]
    # None started yet today (e.g. 02:00): still in the period that began last, yesterday
    return max(started or tariff.periods, key=lambda p: p.start).rate


def month_tou_lkr(counter: list[tuple[datetime, float]], tariff: TimeOfUse, *, tz: str) -> float:
    """A month's bill under time-of-use: each rise in the meter priced at the rate when it began."""
    used = (
        max(b - a, 0.0) * tou_rate(tariff, t, tz=tz)
        for (t, a), (_, b) in itertools.pairwise(counter)
    )
    return tariff.fixed + sum(used)


def saving_lkr(
    month_kwh: float, saved_kwh: float, tariff: tuple[Schedule, ...], *, levy: float = 0.0
) -> float:
    """What saving saved_kwh is worth in a month that would otherwise use month_kwh."""
    less = max(month_kwh - saved_kwh, 0.0)
    return month_lkr(month_kwh, tariff, levy=levy) - month_lkr(less, tariff, levy=levy)


@dataclass(frozen=True)
class Factor:
    id: str  # cited by every number that uses it, e.g. "grid-2025"
    value: float  # kgCO2e per kWh
    source: str  # publisher, year and page, so anyone can check it
    valid_from: str  # ISO date the factor applies from


def kgco2e(kwh: float, factor: Factor) -> float:
    """Location-based Scope 2: energy times the grid's average emission factor."""
    return kwh * factor.value
