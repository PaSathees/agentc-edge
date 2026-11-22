"""The house model: what the system knows about each device, in plain words (FR-30).

It lives in app.db, so the setup chat and chat can propose changes for the owner to
approve. config.toml keeps what no agent may change: names, access, rules, budget.
- Card: what the device is, its room, what is plugged in, why each timer is set.
- Habit: something normal for the owner, from their answers.
`card()` is the text the agent reads about a device and the text `agentc house` shows
the owner, so both see the same thing.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from agentc.config import Device
from agentc.devices.base import Automations, Timer, days_words, describe

SOURCES = ("tap", "chat", "setup")  # where a habit came from
NO_AUTOMATIONS = Automations(None, ())  # a plug never read, or one with none
# What a device is. Code uses the kind: a must-run kind gets no "left on" or "unusual
# use" alerts, because its cycles are normal (a fridge, a router).
KINDS = ("charger", "computer", "media", "kitchen", "fridge", "fan", "heater", "light",
         "router", "printer", "other")  # fmt: skip
MUST_RUN = frozenset({"fridge", "router"})
FIELDS = ("kind", "room", "what", "notes", "must_run")  # plus why.N for timer N


@dataclass(frozen=True)
class Card:
    """What the system knows about the device on one plug. Empty until the owner says."""

    kind: str = "other"  # one of KINDS
    room: str = ""
    what: str = ""  # what is plugged in, in the owner's words
    notes: str = ""  # anything else the agent should know
    must_run: bool | None = None  # None: from the kind (a fridge or router must run)
    why: dict[int, str] = field(default_factory=dict)  # timer slot -> what it is for


def runs_always(c: Card) -> bool:
    """Must this device stay on? The owner's word, else its kind."""
    return c.kind in MUST_RUN if c.must_run is None else c.must_run


def updated(c: Card, fields: Sequence[str]) -> Card:
    """The card with `key=value` changes, as typed by the owner, e.g. room=study, why.3=work.

    A wrong key, kind or timer slot fails here, before anything is saved.
    """
    changes: dict[str, object] = {}
    why = dict(c.why)
    for f in fields:
        key, sep, value = f.partition("=")
        if not sep:
            raise ValueError(f"{f!r}: write it as key=value, e.g. room=study")
        if key.startswith("why."):
            slot = key.removeprefix("why.")
            if not slot.isdigit() or not 1 <= int(slot) <= 16:
                raise ValueError(f"{key}: timer slots are 1 to 16")
            why[int(slot)] = value
        elif key == "kind" and value not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}, not {value!r}")
        elif key == "must_run":
            if value not in ("yes", "no"):
                raise ValueError("must_run is yes or no")
            changes[key] = value == "yes"
        elif key in FIELDS:
            changes[key] = value
        else:
            raise ValueError(f"unknown key {key!r}. Known: {', '.join(FIELDS)}, why.N")
    return replace(c, **changes, why=why)


@dataclass(frozen=True)
class Habit:
    """Something normal for the owner, e.g. "Desk 2 runs late on Mondays" (FR-33)."""

    text: str  # the owner's words, or the question's
    days: str  # 7 chars, Sunday first, as Tasmota: the days the window starts on
    start: str  # local HH:MM
    end: str  # local HH:MM; earlier than start = the window ends the next day
    source: str  # one of SOURCES


def _minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def covers(habit: Habit, local: datetime) -> bool:
    """Is `local` inside the habit's window? A window past midnight belongs to its start day,
    so 01:30 on Tuesday is inside a Monday 23:00 to 02:00 habit."""
    start, end, now = _minutes(habit.start), _minutes(habit.end), _minutes(f"{local:%H:%M}")
    if start < end:
        day, inside = local, start <= now < end
    elif now >= start:
        day, inside = local, True
    else:  # after midnight: the window started the day before
        day, inside = local - timedelta(days=1), now < end
    sunday_first = (day.weekday() + 1) % 7
    return inside and habit.days[sunday_first] == "1"


def card(
    device: Device, c: Card, *, timers: Sequence[Timer], habits: Sequence[Habit],
    automations: Automations = NO_AUTOMATIONS,
) -> str:  # fmt: skip
    """The device in plain words: what it is, its access, rules, timers, what the plug does on
    its own (auto-off, plug rules), and habits."""
    lines = [_what(device.name, c)]
    if c.notes:
        lines.append(f"Notes: {c.notes}")
    lines.append(f"Access: {device.access}. Must run: {'yes' if runs_always(c) else 'no'}.")
    lines += [f"Rule ({'hard' if r.hard else 'soft'}): {r.text}" for r in device.rules]
    set_timers = [t for t in timers if t.enable]
    lines += [
        f"Timer {t.slot}: {describe(t)}. Why: {c.why.get(t.slot, 'not known')}" for t in set_timers
    ] or ["Timers: none."]
    if automations.auto_off_s:
        lines.append(f"Auto-off: after {_duration(automations.auto_off_s)}, set on the plug")
    lines += [f"Plug rule {r.n} ({'on' if r.on else 'off'}): {r.text}" for r in automations.rules]
    lines += [f"Habit: {h.text} ({days_words(h.days)}, {h.start} to {h.end})" for h in habits] or [
        "Habits: none yet."
    ]
    return "\n".join(lines)


def _what(name: str, c: Card) -> str:
    if c.kind == "other" and not (c.what or c.room):
        return f"{name}: nothing known yet. Ask the owner what is plugged in."
    where = f" Where: {c.room}." if c.room else ""
    plugged = f" Plugged in: {c.what}." if c.what else ""
    return f"{name}: {c.kind}.{where}{plugged}"


def _duration(seconds: float) -> str:
    """4 h, 1 h 30 min, 2 min, 5 s."""
    if seconds < 60:
        return f"{seconds:g} s"
    h, m = divmod(round(seconds / 60), 60)
    return " ".join(part for part in (f"{h} h" if h else "", f"{m} min" if m else "") if part)
