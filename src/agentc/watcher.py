"""Signals: check the readings and find what looks off (saved as events). Code only; it
never calls a model."""

import statistics
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from agentc.calc import ProfileHour
from agentc.devices.base import Timer
from agentc.learn import levels
from agentc.policy import DEFAULT, Policy

MIN_DAYS = 3  # an hour with fewer days than this uses the same hour across the week
MAX_GAP = timedelta(minutes=5)  # a longer gap in readings restarts the count
OFFLINE_ROUNDS = 3  # failed logger rounds in a row before a plug counts as offline
BLACKOUT_ROUNDS = 2  # every plug silent this many rounds in a row = lost contact; 1 is a Wi-Fi blip
HEARTBEAT = timedelta(minutes=5)  # no logger round at all for this long = the logger stopped
PASS_ROUNDS = 7  # rounds each pass reads: 5 since the last pass, plus 2 of overlap
CHANGE = 2.0  # a change point: power when on this many times outside the range seen before
MIN_WINDOWS = 3  # earlier windows with clear levels needed before judging a change
COVER = 0.8  # an automation check needs readings for this share of its minutes
GRACE = timedelta(minutes=5)  # a timer may fire a little late; the override check starts after


@dataclass(frozen=True)
class Event:
    kind: str  # unusual_use, left_on, automation_drift, override, offline, no_contact,
    #            logger_stopped, timer_mismatch or goal_drift
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
    kind: str = "unusual_use"  # or "left_on": a small load at an hour the plug is usually off


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
    """Unusual stretches, oldest first: more_w more for run_min (unusual use), or small_w
    more for left_on_min (left on, FR-09 v2).

    A long stretch holds any unusual one inside it, so that stretch is one run, not two,
    and it is unusual use. Only a stretch that never reached the big rule is left on.
    """
    short = _runs(readings, usual, tz, policy, timedelta(minutes=policy.run_min))
    small = replace(policy, more_w=policy.small_w)  # the same test, with the small load's watts
    long = _runs(readings, usual, tz, small, timedelta(minutes=policy.left_on_min))
    inside = [r for r in short if any(x.start <= r.start <= x.end for x in long)]
    big = {x for x in long if any(x.start <= r.start <= x.end for r in inside)}
    long = [x if x in big else replace(x, kind="left_on") for x in long]
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


def run_event(mac: str, run: Run) -> Event:
    """An unusual or left-on run as an event."""
    minutes = round((run.end - run.start).total_seconds() / 60)
    return Event(run.kind, mac, run.start, {"peak_w": run.peak_w, "minutes": minutes})


def automation_events(
    mac: str, timers: list[Timer], readings: list[tuple[datetime, float]],
    usual: dict[int, Usual], *, now: datetime, tz: str, policy: Policy = DEFAULT,
) -> list[Event]:  # fmt: skip
    """What the plug's own timers did, checked against its power (FR-34).

    Drift: an "on" timer fired, but the device stayed idle for drift_h (on for nothing).
    Override: an "off" timer fired, but the device drew power for override_min after it
    (switched back on, or the timer did not fire). Each timer's latest firing that has
    had its full time is checked; readings must cover most of that time.
    """
    base = min((u.watts for u in usual.values()), default=0.0)  # standby
    found = []
    for t in timers:
        if not (t.enable and t.mode == 0 and t.action in (0, 1)):
            continue  # sunrise, sunset, toggle and rule timers are not checked
        on = t.action == 1
        wait = (
            timedelta(hours=policy.drift_h)
            if on
            else GRACE + timedelta(minutes=policy.override_min)
        )
        if (fired := _fired(t, now - wait, tz)) is None:
            continue
        start = fired if on else fired + GRACE
        seen = [w for at, w in readings if start <= at < fired + wait]
        if len(seen) < COVER * (fired + wait - start).total_seconds() / 60:
            continue  # no data is not "idle"
        facts: dict[str, float | str] = {"slot": t.slot, "time": t.time, "peak_w": max(seen)}
        if on and all(w < base + policy.use_w for w in seen):
            found.append(Event("automation_drift", mac, fired, {**facts, "hours": policy.drift_h}))
        elif not on and all(w > policy.on_w for w in seen):
            found.append(Event("override", mac, fired, facts))
    return found


def change_point(
    mac: str, before: list[list[tuple[datetime, float]]], recent: list[tuple[datetime, float]],
    *, now: datetime,
) -> Event | None:  # fmt: skip
    """A new device on the plug (FR-07): its power when on left the whole range seen before.

    before: earlier windows of the same length as `recent` (2 days each). A device's power
    when on swings by itself (a laptop charger draws less as the battery fills), so the new
    level must be outside the range of every earlier window by CHANGE times or more. At
    least MIN_WINDOWS earlier windows must show clear levels, and so must `recent`: a device
    that is only off lately is not a change.
    """
    known = [lv.active for w in before if (lv := levels([x for _, x in w]))]
    new = levels([x for _, x in recent])
    if new is None or len(known) < MIN_WINDOWS:
        return None
    if min(known) / CHANGE < new.active < max(known) * CHANGE:
        return None
    return Event("change_point", mac, now.astimezone(UTC),
                 {"old_w": round(statistics.median(known), 1),
                  "new_w": round(new.active, 1)})  # fmt: skip


def _fired(t: Timer, upto: datetime, tz: str) -> datetime | None:
    """When the timer last fired at or before `upto` (UTC), if it was set for that day."""
    local = upto.astimezone(ZoneInfo(tz))
    h, m = map(int, t.time.split(":"))
    fired = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if fired > local:
        fired -= timedelta(days=1)
    return fired.astimezone(UTC) if t.days[(fired.weekday() + 1) % 7] not in "0-" else None


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
