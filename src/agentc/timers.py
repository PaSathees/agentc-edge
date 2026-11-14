"""Timer import and plug checks (FR-01 timer import, A-02 plug clocks).

Reads every plug, stores what it has, and returns the problems in plain words.
Nothing here writes to a plug.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from sqlite3 import Connection
from typing import Literal
from zoneinfo import ZoneInfo

from agentc import store
from agentc.devices.base import Clock, Device, DeviceError, Timer
from agentc.devices.tasmota import TasmotaPlug

MAX_DRIFT_S = 120  # a timer firing up to 2 min late does not matter; more does


@dataclass(frozen=True)
class Problem:
    plug: str
    kind: Literal["unreachable", "timers_off", "timezone", "clock"]
    text: str  # what the owner sees


def import_all(
    db: Connection, *, site_id: int, plugs: dict[str, str], now: datetime,
    connect: Callable[[str], Device] = TasmotaPlug,
) -> list[Problem]:  # fmt: skip
    """Read every plug (name -> IP); store plug and timers; return what needs the owner."""
    tz = db.execute("SELECT tz FROM sites WHERE id = ?", (site_id,)).fetchone()[0]
    problems: list[Problem] = []
    for name, ip in plugs.items():
        problems += _import_one(db, site_id, name, ip, connect(ip), tz=tz, now=now)
    return problems


def _import_one(
    db: Connection, site_id: int, name: str, ip: str, device: Device, *, tz: str, now: datetime
) -> list[Problem]:
    try:
        info, timer_set, clock = device.info(), device.timers(), device.clock()
    except DeviceError as err:
        store.plug_failed(db, name=name, error=str(err))
        return [Problem(name, "unreachable", f"{name} did not answer; retried next pass")]
    store.plug_seen(db, site_id=site_id, mac=info.mac, name=name, ip=ip,
                    timers_on=timer_set.enabled, now=now)  # fmt: skip
    store.save_timers(db, site_id=site_id, mac=info.mac, timers=timer_set.timers, now=now)
    problems = check_clock(name, clock, tz=tz, now=now)
    set_slots = [t.slot for t in timer_set.timers if t.enable]
    if not timer_set.enabled and set_slots:  # off with no timers set blocks nothing yet
        text = f"{name} has its Timers switch off, so its timers {set_slots} will not fire"
        problems.insert(0, Problem(name, "timers_off", text))
    return problems


def check_clock(name: str, clock: Clock, *, tz: str, now: datetime) -> list[Problem]:
    """A-02: the plug's zone matches the site's, and its clock is within 2 minutes."""
    problems = []
    expected = utc_offset(tz, now)
    if clock.timezone != expected:
        problems.append(Problem(name, "timezone",
            f"{name} is set to UTC{clock.timezone}, the site is UTC{expected}"))  # fmt: skip
    drift = abs((clock.utc - now).total_seconds())
    if drift > MAX_DRIFT_S:
        problems.append(Problem(name, "clock",
            f"{name}'s clock is {drift / 60:.0f} min off; check its time server"))  # fmt: skip
    return problems


def utc_offset(tz: str, at: datetime) -> str:
    """'Asia/Colombo' -> '+05:30', the way Tasmota writes it."""
    offset = at.astimezone(ZoneInfo(tz)).utcoffset()
    assert offset is not None  # always set for a ZoneInfo time; tells the type checker
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


ACTIONS = {0: "Off", 1: "On", 2: "Toggle", 3: "Rule"}
WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def describe(t: Timer) -> str:
    """A timer as the owner reads it: 'On at 07:30, Monday to Friday'."""
    if not t.enable:
        return "Not set"
    when = {1: "at sunrise", 2: "at sunset"}.get(t.mode, f"at {t.time}")
    if t.mode in (1, 2) and t.time != "00:00":
        when += f" (offset {t.time})"  # for sunrise and sunset, Time is an offset
    parts = [f"{ACTIONS.get(t.action, '?')} {when}", _days(t.days)]
    if not t.repeat:
        parts.append("once")
    if t.window:
        parts.append(f"up to {t.window} min later")
    return ", ".join(parts)


def _days(days: str) -> str:
    """Tasmota's 7 chars, Sunday first ('0111110') -> 'Monday to Friday'."""
    on_sun_first = [c not in "0-" for c in days]
    # Re-index Monday first: Monday is position 1 in Tasmota's string, Sunday wraps to 6
    on = [i for i in range(7) if on_sun_first[(i + 1) % 7]]
    if len(on) == 7:
        return "every day"
    if on == [5, 6]:
        return "weekends"
    if not on:
        return "no days"
    if len(on) >= 3 and on == list(range(on[0], on[-1] + 1)):
        return f"{WEEK[on[0]]} to {WEEK[on[-1]]}"
    names = [WEEK[i] for i in on]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
