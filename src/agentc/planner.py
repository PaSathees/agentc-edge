"""The weekly Planner's code half (ADR-010, Phase 5b): what a timer change would have done,
and whether a plan item may reach the owner.

The model chooses and explains; code measures. what_if replays the plug's last 14 days of
real readings twice, minute by minute: as its timers ran, and as they would run after the
change. Minutes the change would switch off save what the plug really drew then; minutes
it would switch on cost about its standby. It also counts the days the device was in use
in the new off time: a change that would have cut real use is never ready to do, only an
option to talk over.
"""

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from sqlite3 import Connection
from zoneinfo import ZoneInfo

from agentc import learn, readings
from agentc.agent_tools import worth
from agentc.checks import breaks
from agentc.config import Config, Device
from agentc.devices.base import DAY_SETS, Timer, days_words, describe
from agentc.house import Card, runs_always

DAYS = 14  # days of readings a change is replayed on
MINUTES = 24 * 60
DISCUSS_DAYS = 3  # in use in the new off time on 1 to this many days: an option to talk over;
# more: blocked. On 0 days it is ready: the owner can just say Do it
ITEMS = 3  # plan items a week, at most
SMALL = 0.005  # kWh a month: less than this is no saving at all
NOT_NOW = timedelta(days=30)  # a "Not now" keeps the same change away this long
DISRUPTION_WH = 100  # one unit of disruption counts as this many Wh a month to the solver
KINDS = ("move_timer", "add_pair", "remove_timer")


@dataclass(frozen=True)
class Change:
    """One change to a plug's timers, as the Planner proposes it.

    move_timer: timer `slot` to `time`, same days and action; `from_time` is its time when
    planned, so a later hand change is never written over. add_pair: off at `off`, on at
    `on`, on `days` (a DAY_SETS key), in free slots the device allows. remove_timer: timer
    `slot` is cleared."""

    kind: str
    slot: int | None = None
    time: str | None = None
    off: str | None = None
    on: str | None = None
    days: str | None = None
    from_time: str | None = None

    def to_json(self) -> str:
        return json.dumps({k: v for k, v in asdict(self).items() if v is not None},
                          sort_keys=True)  # fmt: skip

    def apply(self, timers: Sequence[Timer]) -> list[Timer]:
        """The plug's timers after this change. Slot 0 marks a pair's not-yet-chosen slots."""
        if self.kind == "add_pair" and self.off and self.on and self.days in DAY_SETS:
            bits = DAY_SETS[self.days]
            return [*timers, Timer(0, True, 0, self.on, 0, bits, True, 1, 1),
                    Timer(0, True, 0, self.off, 0, bits, True, 1, 0)]  # fmt: skip
        out = []
        for t in timers:
            if t.slot == self.slot and self.kind == "move_timer" and self.time:
                t = Timer(t.slot, t.enable, t.mode, self.time, t.window, t.days, t.repeat,
                          t.output, t.action)  # fmt: skip
            if t.slot == self.slot and self.kind == "remove_timer":
                continue
            out.append(t)
        return out


@dataclass(frozen=True)
class WhatIf:
    """What a change would have done over the days replayed, per 30 days."""

    saved_kwh: float  # drawn in the new off time
    added_kwh: float  # standby in the new on time
    used_days: int  # days the device was in use in the new off time
    seen_days: int  # days with readings
    worth: str  # the net saving priced: "0.9 kWh, LKR 40.1, 0.4 kgCO2e"

    @property
    def net_kwh(self) -> float:
        return self.saved_kwh - self.added_kwh

    def text(self) -> str:
        if not self.seen_days:
            return "No readings in the last 14 days, so no saving can be worked out."
        use = f"In use in the new off time on {self.used_days} of {self.seen_days} days."
        if abs(self.net_kwh) < SMALL:
            return f"No saving: it uses about the same. {use}"
        verb = "Saves" if self.net_kwh > 0 else "Costs"
        return f"{verb} about {self.worth} a month. {use}"


def relay(timers: Sequence[Timer], day: date) -> list[bool | None]:
    """The relay minute by minute on one local day, as these timers drive it: True on,
    False off, None when no timer has acted yet (the owner's own doing). Each day starts
    as the last timer of the days before left it, looking back a week."""
    state: bool | None = None
    for back in range(7, 0, -1):
        for _, on in _events(timers, day - timedelta(days=back)):
            state = on
    events = dict(_events(timers, day))
    out = []
    for minute in range(MINUTES):
        state = events.get(minute, state)
        out.append(state)
    return out


def what_if(energy: Connection, cfg: Config, device: Device, plug: str,
            timers: Sequence[Timer], change: Change, *, end: datetime) -> WhatIf:  # fmt: skip
    """Replay the DAYS local days before `end` with and without the change."""
    zone = ZoneInfo(cfg.tz)
    last = end.astimezone(zone).date()
    first = last - timedelta(days=DAYS)
    start = datetime.combine(first, datetime.min.time(), zone)
    rows = readings.power(energy, plug=plug, start=start, end=start + timedelta(days=DAYS))
    watts: dict[date, dict[int, float]] = defaultdict(dict)
    for at, w in rows:
        local = at.astimezone(zone)
        watts[local.date()][local.hour * 60 + local.minute] = w
    lv = learn.levels([w for _, w in rows])
    standby = lv.standby if lv else 0.0
    in_use = (standby if lv else device.policy.on_w) + device.policy.use_w
    after = change.apply(timers)
    saved = added = 0.0
    used: set[date] = set()
    first_off: datetime | None = None
    for n in range(DAYS):
        day = first + timedelta(days=n)
        now, new = relay(timers, day), relay(after, day)
        for minute in range(MINUTES):
            was_on = now[minute] is not False  # no timer acting: as the readings show
            will_be = was_on if new[minute] is None else new[minute]
            if was_on and not will_be:
                w = watts.get(day, {}).get(minute, 0.0)
                saved += w / 60
                if w >= in_use:
                    used.add(day)
                if first_off is None:
                    first_off = datetime.combine(day, datetime.min.time(), zone) + timedelta(
                        minutes=minute
                    )
            elif not was_on and will_be:
                added += standby / 60
    seen = sum(1 for d in watts if first <= d < last)
    month = 30 / seen / 1000 if seen else 0.0
    net = (saved - added) * month
    return WhatIf(saved * month, added * month, len(used), seen,
                  worth(cfg, abs(net), first_off or start, digits=2))  # fmt: skip


def check_item(change: Change, *, device: Device, card: Card, timers: Sequence[Timer],
               timers_on: bool, result: WhatIf, refused: Sequence[str]) -> str | None:  # fmt: skip
    """Why a plan item may not reach the owner, or None. Cheap checks first."""
    name = device.name
    if device.access != "write":
        return (f"access: {name} is set to {device.access}. A plan item needs write, set by"
                " the owner in config.toml")  # fmt: skip
    if runs_always(card):
        return f"must run: {name} must always run, so its timers are left alone"
    if change.kind not in KINDS:
        return f"format: a change is one of {', '.join(KINDS)}"
    if why := _shape(change, timers, device):
        return why
    if not timers_on:
        return f"timers: {name}'s plug has its Timers switch off, so no timer fires"
    off = _off_time(change, timers)
    if off and (hard := [r for r in device.rules if r.hard and breaks(off, r)]):
        return f"rule: {hard[0].text}"
    if change.to_json() in refused:
        return f"not now: the owner said Not now to this in the last {NOT_NOW.days} days"
    if result.used_days > DISCUSS_DAYS:
        return (f"used: {name} was in use in the new off time on {result.used_days} of"
                f" {result.seen_days} days")  # fmt: skip
    if result.net_kwh < SMALL:
        return "no saving: it would not use less"
    return None


def ready(result: WhatIf) -> bool:
    """Can the owner just say Do it? Only if it would never have cut real use. Else it is an
    option to talk over in the chat, with no Do it."""
    return result.used_days == 0


def check_plan(changes: Sequence[Change]) -> str | None:
    """Rules for the plan as a whole."""
    if len(changes) > ITEMS:
        return f"format: at most {ITEMS} items a week"
    if len({c.to_json() for c in changes}) < len(changes):
        return "format: the same change twice"
    return None


def _shape(change: Change, timers: Sequence[Timer], device: Device) -> str | None:
    """Is the change well formed for this plug?"""
    by_slot = {t.slot: t for t in timers if t.enable}
    if change.kind in ("move_timer", "remove_timer"):
        t = by_slot.get(change.slot or 0)
        if t is None:
            return f"format: no set timer {change.slot} on this plug"
        if t.mode != 0:
            return "format: only timers set at a clock time can change, not sunrise or sunset"
        if change.kind == "move_timer":
            if not _hhmm(change.time):
                return "format: a time is HH:MM"
            if change.time == t.time:
                return "format: the new time is the same as now"
        return None
    if not (_hhmm(change.off) and _hhmm(change.on)) or change.days not in DAY_SETS:
        return f"format: a pair needs off and on as HH:MM and days from {', '.join(DAY_SETS)}"
    if change.off == change.on:
        return "format: off and on are the same time"
    free = [s for s in device.slots if s not in by_slot]
    if len(free) < 2:
        return f"slots: {device.name} has no 2 free timer slots it allows"
    return None


def _off_time(change: Change, timers: Sequence[Timer]) -> str | None:
    """The new time the change switches the device off, if it does."""
    if change.kind == "add_pair":
        return change.off
    if change.kind == "move_timer":
        t = next(t for t in timers if t.slot == change.slot and t.enable)
        return change.time if t.action == 0 else None
    return None


def before_after(change: Change, timers: Sequence[Timer]) -> tuple[str, str]:
    """The change in the owner's words: what it is now, and what it will be."""
    by_slot = {t.slot: t for t in timers}
    if change.kind == "add_pair":
        days = days_words(DAY_SETS[change.days or "every_day"])
        return "No timer", f"Off at {change.off}, on at {change.on}, {days}"
    t = by_slot[change.slot or 0]
    if change.kind == "remove_timer":
        return describe(t), "No timer"
    moved = Timer(t.slot, t.enable, t.mode, change.time or t.time, t.window, t.days, t.repeat,
                  t.output, t.action)  # fmt: skip
    return describe(t), describe(moved)


def _events(timers: Sequence[Timer], day: date) -> list[tuple[int, bool]]:
    """(minute, switches on?) for each clock timer that acts on this day, in order."""
    sunday_first = (day.weekday() + 1) % 7
    out = []
    for t in timers:
        if not t.enable or t.mode != 0 or t.action not in (0, 1) or not _hhmm(t.time):
            continue
        if t.days[sunday_first] in "0-":
            continue
        h, m = map(int, t.time.split(":"))
        out.append((h * 60 + m, t.action == 1))
    return sorted(out)


def _hhmm(value: str | None) -> bool:
    if not value or len(value) != 5 or value[2] != ":":
        return False
    h, m = value[:2], value[3:]
    return h.isdigit() and m.isdigit() and int(h) < 24 and int(m) < 60


# the week's set of changes, chosen by a solver (ADR-003, 5b4)


@dataclass(frozen=True)
class Candidate:
    """One change code found and priced: a timer change, or an action for the owner."""

    id: str  # "c1", "c2", ...: how the Planner names it
    plug: str
    mac: str
    device: str  # the owner's name for it
    change: Change | None  # None: an action the owner does
    action: str  # the action in the owner's words, or "" for a timer change
    wh: int  # Wh a month it saves, from readings (whole numbers: the solver counts in them)
    cost: int  # disruption: 0 ready, +1 a day it cut real use, +1 a new pair, +2 an action
    spot: str  # what it changes: a timer, a plug's new pair, or the device for an action
    ready: bool
    saving: str  # what_if's words, or the action's


def choose(cands: Sequence[Candidate], *, gap_wh: int, items: int = ITEMS) -> list[Candidate]:
    """The set that best closes the gap: the least (shortfall + DISRUPTION_WH x disruption),
    then the most saving. So a change that cuts real use once must save 100 Wh more than one
    that does not, and an action for the owner (2 units) at least 200 Wh. One change per
    spot, at most `items`. With no gap (on track), only free changes. CP-SAT runs
    single-threaded, so the same candidates give the same set every time."""
    from ortools.sat.python import cp_model  # noqa: PLC0415 (only the weekly job loads it)

    cands = [c for c in cands if c.wh > 0]
    if not cands:
        return []
    m = cp_model.CpModel()
    x = {c.id: m.new_bool_var(c.id) for c in cands}
    for spot in {c.spot for c in cands}:
        m.add_at_most_one(x[c.id] for c in cands if c.spot == spot)
    m.add(sum(x.values()) <= items)
    saved = sum(c.wh * x[c.id] for c in cands)
    gap = max(gap_wh, 0)
    short = m.new_int_var(0, gap, "short")
    m.add(short >= gap - saved)
    cost = sum(c.cost * x[c.id] for c in cands)
    tie = sum(c.wh for c in cands) + 1  # the saving only breaks ties
    m.minimize((short + DISRUPTION_WH * cost) * tie - saved)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = 10
    got = solver.solve(m)
    if got == cp_model.MODEL_INVALID:  # numbers past what the solver can hold: never silent
        raise ValueError("the savings or the gap are too large to weigh")
    if got not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return []  # pragma: no cover (an empty set is always allowed, so never)
    return [c for c in cands if solver.value(x[c.id])]


def shortfall_wh(chosen: Sequence[Candidate], *, gap_wh: int) -> int:
    return max(gap_wh - sum(c.wh for c in chosen), 0)


def idle_wh(energy: Connection, device: Device, plug: str, *, end: datetime) -> tuple[int, int]:
    """Wh a month the plug drew while on but not in use, over the last DAYS days, and the
    days it was seen. Above off (0.5 W) and below its in-use level: a device left idling."""
    rows = readings.power(energy, plug=plug, start=end - timedelta(days=DAYS), end=end)
    lv = learn.levels([w for _, w in rows])
    in_use = (lv.standby if lv else device.policy.on_w) + device.policy.use_w
    wh = sum(w / 60 for _, w in rows if learn.OFF_W < w < in_use)
    if not rows:
        return 0, 0
    span = (rows[-1][0] - rows[0][0] + timedelta(minutes=1)) / timedelta(days=1)  # not dates
    return round(wh * 30 / span), round(span)
