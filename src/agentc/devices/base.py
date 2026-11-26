"""What every plug adapter returns. Tasmota plugs now; MQTT or Modbus later."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class PlugError(Exception):
    """The plug did not answer, or answered with something we can't read."""


@dataclass(frozen=True)
class Info:
    mac: str
    hostname: str


DAY_SETS = {"every_day": "1111111", "weekdays": "0111110", "weekends": "1000001"}  # Sunday first


@dataclass(frozen=True)
class Timer:
    """One of a plug's 16 timers, fields as Tasmota names them."""

    slot: int
    enable: bool
    mode: int  # 0 time, 1 sunrise, 2 sunset
    time: str  # HH:MM, plug local time
    window: int  # random offset, minutes
    days: str  # 7 chars, Sunday first; "1" or a letter = on
    repeat: bool
    output: int
    action: int  # 0 off, 1 on, 2 toggle, 3 rule


@dataclass(frozen=True)
class PlugRule:
    """A rule stored on the plug (Tasmota Rule1 to Rule3), e.g. a boot rule."""

    n: int  # 1, 2 or 3
    on: bool  # a rule can be set but switched off
    text: str  # the rule as Tasmota holds it


@dataclass(frozen=True)
class Automations:
    """What the plug does on its own, apart from its timers."""

    auto_off_s: float | None  # after switching on, it switches off after this long (PulseTime)
    rules: tuple[PlugRule, ...]  # only the rules that hold text


@dataclass(frozen=True)
class TimerSet:
    enabled: bool  # the plug's global Timers switch: off means no timer fires
    timers: list[Timer]


@dataclass(frozen=True)
class Clock:
    utc: datetime  # the plug's own idea of now
    timezone: str  # e.g. "+05:30"


class Plug(Protocol):
    """The plug hardware, through its adapter. Not the device plugged into it."""

    def info(self) -> Info: ...
    def timers(self) -> TimerSet: ...
    def clock(self) -> Clock: ...
    def automations(self) -> Automations: ...
    def power(self) -> bool: ...  # is the relay on?
    def switch(self, on: bool) -> None: ...  # the one direct command (ADR-008)
    def set_timer(self, t: Timer) -> None: ...  # one of the 16 timers, all its fields


# a timer in plain words, for the owner and the agent

ACTIONS = {0: "Off", 1: "On", 2: "Toggle", 3: "Rule"}
WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def describe(t: Timer) -> str:
    """A timer as the owner reads it: 'On at 07:30, Monday to Friday'."""
    if not t.enable:
        return "Not set"
    when = {1: "at sunrise", 2: "at sunset"}.get(t.mode, f"at {t.time}")
    if t.mode in (1, 2) and t.time != "00:00":
        when += f" (offset {t.time})"  # for sunrise and sunset, Time is an offset
    parts = [f"{ACTIONS.get(t.action, '?')} {when}", days_words(t.days)]
    if not t.repeat:
        parts.append("once")
    if t.window:
        parts.append(f"up to {t.window} min later")
    return ", ".join(parts)


def day_bits(days: str) -> str | None:
    """A timer's days as Tasmota's 7 chars, Sunday first: a DAY_SETS name, or the 7 chars
    themselves ("1111100": Sunday to Thursday). None if neither, or no day at all."""
    bits = DAY_SETS.get(days, days)
    return bits if len(bits) == 7 and set(bits) <= {"0", "1"} and "1" in bits else None  # noqa: PLR2004


def days_words(days: str) -> str:
    """Tasmota's 7 chars, Sunday first ('0111110') -> 'Monday to Friday'. A run of 3 or more
    days may go round the week's end: '1111100' -> 'Sunday to Thursday'. A set's name works
    too ('weekdays'); anything else is said as it is."""
    days = DAY_SETS.get(days, days)
    if len(days) != 7:  # noqa: PLR2004
        return days
    on_sun_first = [c not in "0-" for c in days]
    # Re-index Monday first: Monday is position 1 in Tasmota's string, Sunday wraps to 6
    on = [i for i in range(7) if on_sun_first[(i + 1) % 7]]
    if len(on) == 7:  # noqa: PLR2004
        return "every day"
    if on == [5, 6]:
        return "weekends"
    if not on:
        return "no days"
    if len(on) >= 3:  # noqa: PLR2004
        for start in on:  # the day the run starts: the one whose day before is off
            if (start - 1) % 7 not in on and {(start + k) % 7 for k in range(len(on))} == set(on):
                return f"{WEEK[start]} to {WEEK[(start + len(on) - 1) % 7]}"
    names = [WEEK[i] for i in on]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
