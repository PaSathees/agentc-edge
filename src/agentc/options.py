"""Fixes code can offer for an event, each with its saving. The AI only picks one (FR-15).

A fix is a pair of plug timers: off after the latest use, on again before the first use.
Every time and number here comes from the readings, so the AI never has to work one out.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from agentc.calc import Factor, TimeOfUse, saving_lkr, tou_rate
from agentc.config import Tariff
from agentc.devices.base import Timer
from agentc.policy import DEFAULT, Policy
from agentc.watcher import MAX_GAP, Run, Usual, stretches

DAY_START = 6 * 60  # a day runs from 06:00 to 06:00, so use after midnight counts as late
DAY = 24 * 60
EVERY_DAY, WEEKDAYS, WEEKEND = "1111111", "0111110", "1000001"  # Sunday first, as Tasmota


@dataclass(frozen=True)
class Change:
    slot: int  # the plug timer to write
    action: str  # "off" or "on"
    time: str  # local HH:MM
    days: str  # 7 characters, Sunday first; "1" = on that day


@dataclass(frozen=True)
class Option:
    id: str  # "A", "B": what the AI answers with
    changes: tuple[Change, ...]
    kwh: float  # saved a week
    lkr: float
    kg: float


@dataclass(frozen=True)
class Proposal:
    """A suggested change the owner approves or rejects (FR-14). Its numbers come from an Option."""

    plug_mac: str
    device: str  # the owner's name for what is plugged in
    changes: tuple[Change, ...]
    reason: str  # the AI's 1 or 2 sentences
    kwh: float  # saved a week
    lkr: float
    kg: float
    evidence: str  # what the numbers rest on, e.g. the event and the days of readings
    source: str  # responder, planner, assistant or owner
    expires: datetime  # not approved by then: it lapses


@dataclass(frozen=True)
class Money:
    tariff: Tariff
    month_kwh: float  # the whole house's units in 30 days: sets which band a saving comes off
    levy: float
    factor: Factor


@dataclass(frozen=True)
class Use:
    latest: int  # minutes after 06:00 the plug was last in use, over the days given
    first: int  # minutes after 06:00 it was first in use


def minutes(local: datetime) -> int:
    """Minutes after 06:00 on the day it belongs to: 05:59 is 1439, the end of that day."""
    return (local.hour * 60 + local.minute - DAY_START) % DAY


def of(hhmm: str) -> int:
    """Local HH:MM as minutes after 06:00."""
    h, m = map(int, hhmm.split(":"))
    return (h * 60 + m - DAY_START) % DAY


def clock(m: int) -> str:
    """Minutes after 06:00 as local HH:MM."""
    m = (m + DAY_START) % DAY
    return f"{m // 60:02d}:{m % 60:02d}"


def use(
    readings: list[tuple[datetime, float]], usual: dict[int, Usual], *, tz: str,
    policy: Policy = DEFAULT,
) -> Use | None:  # fmt: skip
    """When the plug is in use: use_w above standby for use_min. None if it never was.

    Power left on below that level is waste, not use, so it never moves the times.
    """
    zone = ZoneInfo(tz)
    base = min((u.watts for u in usual.values()), default=0.0)  # standby
    found = stretches(readings, lambda _, w: w - base >= policy.use_w,
                      timedelta(minutes=policy.use_min))  # fmt: skip
    used = [minutes(at.astimezone(zone)) for s in found for at, _ in s]
    return Use(max(used), min(used)) if used else None


def window(u: Use, policy: Policy = DEFAULT) -> tuple[int, int] | None:
    """Off and on times: a margin after the latest use and before the first.

    None if there is no gap, or if it would keep the plug off longer than max_off_h.
    """
    step, margin = policy.step_min, policy.margin_min
    off = math.ceil(u.latest / step) * step + margin
    on = u.first // step * step - margin
    if off >= on + DAY or (on - off) % DAY > policy.max_off_h * 60:
        return None
    return off, on


def offer(
    run: Run, readings: list[tuple[datetime, float]], u: Use, *, tz: str, days: int,
    timers: list[Timer], slots: Sequence[int], money: Money, policy: Policy = DEFAULT,
) -> list[Option]:  # fmt: skip
    """Fixes for one unusual run: A every day, B only on the days like the run's.

    readings: the last `days` days, oldest first. u: their use. Empty when no timer
    would cut the run.
    """
    zone = ZoneInfo(tz)
    times = window(u, policy)
    if times is None:
        return []
    off, on = times
    off_at = _off_at(run, off, zone)
    if not (run.start < off_at + timedelta(minutes=(on - off) % DAY) and run.end > off_at):
        return []  # the timers would not have stopped this run
    has_on = any(_is_on(t, off, u.first) for t in timers)
    free = [s for s in slots if s not in {t.slot for t in timers if t.enable}]
    if len(free) < (1 if has_on else 2):
        return []  # no allowed timer slots left on this plug
    group = WEEKEND if off_at.weekday() >= 5 else WEEKDAYS
    found = []
    for oid, which in (("A", EVERY_DAY), ("B", group)):
        changes = [Change(free[0], "off", clock(off), which)]
        if not has_on:
            changes.append(Change(free[1], "on", clock(on), which))
        kwh = saved(readings, off, on, which, zone) * 7 / days
        kg = kwh * money.factor.value
        found.append(Option(oid, tuple(changes), kwh, lkr(kwh, money, at=off), kg))
    return [o for o in found if o.kwh > 0]


def _off_at(run: Run, off: int, zone: ZoneInfo) -> datetime:
    """When the off timer would fire in the run's day (06:00 to 06:00)."""
    start = run.start.astimezone(zone)
    return start - timedelta(minutes=minutes(start)) + timedelta(minutes=off)


def _is_on(t: Timer, off: int, first: int) -> bool:
    """An enabled clock timer that already turns the plug on before its first use."""
    if not (t.enable and t.mode == 0 and t.action == 1):
        return False
    return (of(t.time) - off) % DAY <= (first - off) % DAY


def saved(
    readings: list[tuple[datetime, float]], off: int, on: int, days: str, zone: ZoneInfo
) -> float:
    """kWh the plug used while the timers would have kept it off, on those days."""
    total = 0.0
    for (at, w), (nxt, _) in zip(readings, readings[1:], strict=False):
        local = at.astimezone(zone)
        if (minutes(local) - off) % DAY >= (on - off) % DAY:
            continue  # outside the off window
        fired = local - timedelta(minutes=(minutes(local) - off) % DAY)  # when the off timer ran
        if days[(fired.weekday() + 1) % 7] != "1":
            continue
        total += w * min(nxt - at, MAX_GAP).total_seconds() / 3600 / 1000
    return total


def lkr(week_kwh: float, money: Money, *, at: int) -> float:
    """LKR a week for week_kwh saved a week. Time-of-use: the rate when the timer fires."""
    if isinstance(money.tariff, TimeOfUse):
        h, m = divmod((at + DAY_START) % DAY, 60)
        rate = tou_rate(money.tariff, datetime(2000, 1, 1, h, m, tzinfo=UTC), tz="UTC")
        return week_kwh * rate / (1 - money.levy)
    month = saving_lkr(money.month_kwh, week_kwh * 30 / 7, money.tariff, levy=money.levy)
    return month * 7 / 30
