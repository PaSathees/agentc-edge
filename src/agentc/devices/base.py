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


def days_words(days: str) -> str:
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
