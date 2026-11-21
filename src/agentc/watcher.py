"""The Watcher: checks the readings and finds events. Pure code; it never calls a model."""

import statistics
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agentc.calc import ProfileHour
from agentc.devices.base import Timer
from agentc.policy import DEFAULT, Policy

MIN_DAYS = 3  # an hour with fewer days than this uses the same hour across the week
MAX_GAP = timedelta(minutes=5)  # a longer gap in readings restarts the count
OFFLINE_ROUNDS = 3  # failed logger rounds in a row before a plug counts as offline
BLACKOUT_ROUNDS = 2  # every plug silent this many rounds in a row = lost contact; 1 is a Wi-Fi blip
HEARTBEAT = timedelta(minutes=5)  # no logger round at all for this long = the logger stopped
PASS_ROUNDS = 17  # rounds each pass reads: 15 since the last pass, plus 2 of overlap


@dataclass(frozen=True)
class Event:
    kind: str  # unusual_use, offline, no_contact, logger_stopped or timer_mismatch
    plug_mac: str | None  # None for an event about the whole site, like lost contact
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


def is_unusual(watts: float, usual: Usual | None, policy: Policy = DEFAULT) -> bool:
    """Power well above usual, at an hour the plug is rarely on."""
    if usual is None:
        return False
    return (
        usual.on_share < policy.rarely_on
        and watts > policy.factor * usual.watts
        and watts - usual.watts >= policy.more_w
    )


def unusual_runs(
    readings: list[tuple[datetime, float]], usual: dict[int, Usual], *, tz: str,
    policy: Policy = DEFAULT,
) -> list[Run]:  # fmt: skip
    """Unusual stretches, oldest first: more_w more for run_min, or small_w for long_run_min.

    A long stretch holds any short one inside it, so that stretch is one run, not two.
    """
    short = _runs(readings, usual, tz, policy, timedelta(minutes=policy.run_min))
    small = replace(policy, more_w=policy.small_w)  # the same test, with the small load's watts
    long = _runs(readings, usual, tz, small, timedelta(minutes=policy.long_run_min))
    inside = [r for r in short if any(x.start <= r.start <= x.end for x in long)]
    return sorted([r for r in short if r not in inside] + long, key=lambda r: r.start)


def _runs(
    readings: list[tuple[datetime, float]], usual: dict[int, Usual], tz: str, policy: Policy,
    length: timedelta,
) -> list[Run]:  # fmt: skip
    """Stretches of unusual power that last `length` or more, oldest first."""
    zone = ZoneInfo(tz)

    def high(at: datetime, watts: float) -> bool:
        local = at.astimezone(zone)
        hour = usual.get(local.weekday() * 24 + local.hour)
        return is_unusual(watts, hour, policy)

    return [Run(s[0][0], s[-1][0], max(w for _, w in s)) for s in stretches(readings, high, length)]


def stretches(
    readings: list[tuple[datetime, float]], high: Callable[[datetime, float], bool],
    length: timedelta,
) -> list[list[tuple[datetime, float]]]:  # fmt: skip
    """Unbroken stretches of high readings that last `length` or more, oldest first.

    A reading that is not high ends a stretch, and so does a gap over 5 minutes.
    """
    found: list[list[tuple[datetime, float]]] = []
    last = None  # when the previous high reading was; None after a reading that is not high
    for at, watts in readings:
        if not high(at, watts):
            last = None
            continue
        if last is None or at - last > MAX_GAP:
            found.append([])  # start a new stretch
        found[-1].append((at, watts))
        last = at
    return [s for s in found if s[-1][0] - s[0][0] >= length]


def unusual_use(mac: str, run: Run) -> Event:
    minutes = round((run.end - run.start).total_seconds() / 60)
    return Event("unusual_use", mac, run.start, {"peak_w": run.peak_w, "minutes": minutes})


def dedupe_key(event: Event, *, tz: str) -> str:
    """Same plug, kind and local day = same event, saved once (FR-13).

    A logger stop is keyed by its minute: it can stop twice in a day, and its start (the
    last reading) stays the same for as long as it is stopped.
    """
    local = event.start.astimezone(ZoneInfo(tz))
    day = local.strftime("%Y-%m-%dT%H:%M") if event.kind == "logger_stopped" else local.date()
    return f"{event.plug_mac or 'site'}|{event.kind}|{day}"


def offline_events(
    rounds: list[tuple[datetime, dict[str, bool]]], macs: dict[str, str]
) -> list[Event]:
    """Lost contact if every plug was silent 2 rounds in a row; else plugs down 3 rounds.

    From inside the hub, a power cut and the hub's own Wi-Fi going down look the same,
    so the event says "lost contact", not "power cut".

    rounds: the logger's rounds since the last pass, oldest first; each maps plug -> answered.
    macs: plug name -> MAC, for the plugs the app knows.
    """
    if len(rounds) < OFFLINE_ROUNDS:
        return []
    silent = [not any(answered.values()) for _, answered in rounds]
    for i in range(len(rounds) - BLACKOUT_ROUNDS + 1):
        if all(silent[i : i + BLACKOUT_ROUNDS]):  # the whole house: one event, not one per plug
            return [Event("no_contact", None, rounds[i][0], {"plugs": len(rounds[i][1])})]
    last = rounds[-OFFLINE_ROUNDS:]
    down = [p for p in last[-1][1] if not any(answered.get(p, False) for _, answered in last)]
    return [
        Event("offline", macs[p], last[0][0], {"plug": p, "rounds": OFFLINE_ROUNDS})
        for p in down
        if p in macs
    ]


def logger_stopped(last_round: datetime | None, *, now: datetime) -> Event | None:
    """No logger round at all for HEARTBEAT: the logger itself stopped (FR-35).

    Checked from the app, outside the logger, because a stopped program can't say it
    stopped. A round with failed reads still counts: the logger is running.
    last_round: the newest round before now; None if the logger never ran.
    """
    if last_round is None or now - last_round <= HEARTBEAT:
        return None
    minutes = round((now - last_round).total_seconds() / 60)
    return Event("logger_stopped", None, last_round, {"minutes": minutes})


def timer_mismatch(
    mac: str, saved: list[Timer], read: list[Timer], *, now: datetime
) -> Event | None:
    """The plug's timers differ from the ones we saved last time."""
    if not saved or saved == read:
        return None
    slots = [r.slot for s, r in zip(saved, read, strict=False) if s != r]
    return Event("timer_mismatch", mac, now, {"slots": ", ".join(map(str, slots))})
