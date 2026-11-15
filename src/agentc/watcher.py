"""The Watcher: checks the readings and finds events. Pure code; it never calls a model."""

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agentc.calc import ProfileHour

MIN_DAYS = 3  # an hour with fewer days than this uses the same hour across the week
RARELY_ON = 0.2  # alert only at hours the plug is on under 20% of the time
FACTOR = 2.0  # unusual = more than 2 x usual ...
MORE_W = 10.0  # ... and at least 10 W more, so 0 W to 3 W is not an alert
RUN = timedelta(minutes=30)  # how long it must last
MAX_GAP = timedelta(minutes=5)  # a longer gap in readings restarts the count


@dataclass(frozen=True)
class Event:
    kind: str  # unusual_use, offline, power_cut or timer_mismatch
    plug_mac: str | None  # None for an event about the whole site, like a power cut
    start: datetime
    facts: dict[str, float | str]  # the numbers an agent may quote later


@dataclass(frozen=True)
class Usual:
    watts: float  # the middle value for this hour
    on_share: float  # share of minutes the plug is on


@dataclass(frozen=True)
class Run:
    start: datetime
    end: datetime  # the last unusual reading
    peak_w: float


def usual_week(profile: list[ProfileHour]) -> dict[int, Usual]:
    """The usual watts and on share for each hour of the week (0 = Monday 00:00)."""
    by_hour = {p.hour: p for p in profile}
    usual: dict[int, Usual] = {}
    for hour in range(24):
        week = [by_hour[d * 24 + hour] for d in range(7) if d * 24 + hour in by_hour]
        if not week:
            continue  # no data at this hour on any day: no usual, so no alert
        pooled = Usual(
            statistics.median(p.median_w for p in week), statistics.mean(p.on_share for p in week)
        )
        for p in week:
            usual[p.hour] = Usual(p.median_w, p.on_share) if p.days_used >= MIN_DAYS else pooled
    return usual


def is_unusual(watts: float, usual: Usual | None, *, factor: float = FACTOR) -> bool:
    """Power well above usual, at an hour the plug is rarely on."""
    if usual is None:
        return False
    return (
        usual.on_share < RARELY_ON
        and watts > factor * usual.watts
        and watts - usual.watts >= MORE_W
    )


def unusual_runs(
    readings: list[tuple[datetime, float]], usual: dict[int, Usual], *, tz: str,
    factor: float = FACTOR,
) -> list[Run]:  # fmt: skip
    """Stretches of unusual power that last 30 min or more, oldest first."""
    zone = ZoneInfo(tz)
    runs: list[Run] = []
    stretch: list[tuple[datetime, float]] = []
    for at, watts in readings:
        local = at.astimezone(zone)
        if not is_unusual(watts, usual.get(local.weekday() * 24 + local.hour), factor=factor):
            stretch = _close(stretch, runs)  # a normal reading ends the stretch
            continue
        if stretch and at - stretch[-1][0] > MAX_GAP:
            stretch = _close(stretch, runs)  # too long without readings: start again
        stretch.append((at, watts))
    _close(stretch, runs)
    return runs


def _close(stretch: list[tuple[datetime, float]], runs: list[Run]) -> list[tuple[datetime, float]]:
    """Keep the stretch as a run if it lasted long enough; return a new, empty stretch."""
    if stretch and stretch[-1][0] - stretch[0][0] >= RUN:
        runs.append(Run(start=stretch[0][0], end=stretch[-1][0], peak_w=max(w for _, w in stretch)))
    return []


def unusual_use(mac: str, run: Run) -> Event:
    minutes = round((run.end - run.start).total_seconds() / 60)
    return Event("unusual_use", mac, run.start, {"peak_w": run.peak_w, "minutes": minutes})


def dedupe_key(event: Event, *, tz: str) -> str:
    """Same plug, kind and local day = same event, saved once (FR-13)."""
    day = event.start.astimezone(ZoneInfo(tz)).date().isoformat()
    return f"{event.plug_mac or 'site'}|{event.kind}|{day}"
