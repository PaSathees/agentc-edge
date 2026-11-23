"""The owner loop's rules: when a question may reach the owner, and what a tap does (FR-36).

Code only, no AI. Quiet hours hold small night questions until morning; a tap on "that's
normal" becomes a habit with a scope code builds from the signal, shown back to the owner.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from agentc.house import Habit

QUIET_FROM, QUIET_TO = time(23, 30), time(7, 0)  # night: ask only above night_wake_w
DAY_WINDOW = timedelta(hours=3)  # a habit from a daytime signal covers this long
WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@dataclass(frozen=True)
class Choice:
    label: str
    does: str  # one of agent.DOES
    off: str | None = None  # set_timer only
    on: str | None = None
    days: str | None = None


@dataclass(frozen=True)
class Decision:
    action: str  # one of agent.ACTIONS
    text: str
    choices: tuple[Choice, ...] = ()


@dataclass(frozen=True)
class Question:
    id: int
    event_id: int
    plug_mac: str
    decision: Decision  # action ask or propose
    status: str  # open, answered or expired
    send_at: datetime  # when it may reach the owner (UTC)
    answer: int | None = None  # the choice picked, from 1


@dataclass(frozen=True)
class Outcome:
    """What came of one signal."""

    event_id: int
    device: str
    decision: Decision | None  # None: skipped by code, or no decision passed the checks
    why: str  # skipped or dropped: why; asked: when it reaches the owner; else ""
    looked: bool = True  # False: code skipped it, no model call
    question_id: int | None = None


def held_for_morning(q: Question, *, tz: str) -> bool:
    """Did this question come up at night and wait for 07:00? (A reminder is never due at
    exactly 07:00:00.)"""
    return q.send_at.astimezone(ZoneInfo(tz)).time() == QUIET_TO


def quiet(local: datetime) -> bool:
    """Is it night: 23:30 to 07:00?"""
    return local.time() >= QUIET_FROM or local.time() < QUIET_TO


def send_at(local: datetime, watts: float, *, night_wake_w: float) -> datetime:
    """When a question may reach the owner: now, or 07:00 if it is night and the device
    draws night_wake_w or less. Waking someone for 5 W is worse than the waste."""
    if not quiet(local) or watts > night_wake_w:
        return local
    morning = local.replace(hour=QUIET_TO.hour, minute=0, second=0, microsecond=0)
    return morning if local.time() < QUIET_TO else morning + timedelta(days=1)


def habit(device: str, start: datetime) -> Habit:
    """The habit a "that's normal" tap saves for a signal that started at `start` (local).

    From the start of its hour, on that weekday: to 07:00 for a night signal, else for
    DAY_WINDOW. Too wide hides waste; too narrow keeps asking.
    """
    begin = start.replace(minute=0, second=0, microsecond=0)
    if quiet(start):
        end = begin.replace(hour=QUIET_TO.hour)
        if start.time() >= QUIET_FROM:
            end += timedelta(days=1)
    else:
        end = begin + DAY_WINDOW
    days = "".join("1" if d == (begin.weekday() + 1) % 7 else "0" for d in range(7))
    text = f"{device} is normal on {WEEK[begin.weekday()]}s, {begin:%H:%M} to {end:%H:%M}"
    return Habit(text, days, f"{begin:%H:%M}", f"{end:%H:%M}", "tap")
