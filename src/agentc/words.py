"""Signals and decisions in plain words: as the owner reads them, and as the agent's brief."""

from zoneinfo import ZoneInfo

from agentc.calc import ProfileHour
from agentc.owner import Decision, Outcome
from agentc.watcher import Event

WHEN = "%a %d %b %H:%M"  # Tue 03 Nov 01:00
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def event_words(e: Event, names: dict[str, str], *, tz: str) -> str:
    """An event as the owner reads it. names: plug MAC -> device name."""
    when = e.start.astimezone(ZoneInfo(tz)).strftime(WHEN)
    name = names.get(e.plug_mac or "", "")
    if e.kind == "unusual_use":
        size = f"{e.facts['minutes']} min, peak {e.facts['peak_w']:.0f} W"
        return f"{name}: on at an unusual time from {when}, {size}"
    if e.kind == "left_on":
        size = f"{e.facts['minutes']} min so far, about {e.facts['peak_w']:.0f} W"
        return f"{name}: left on since {when}, {size}"
    if e.kind == "automation_drift":
        day = e.start.astimezone(ZoneInfo(tz)).strftime("%a %d %b")
        return (f"{name}: timer {e.facts['slot']} switched it on at {e.facts['time']} on {day},"
                f" but it did nothing for {e.facts['hours']:g} h")  # fmt: skip
    if e.kind == "override":
        return (f"{name}: timer {e.facts['slot']} should have switched it off at"
                f" {e.facts['time']}, but it still drew {e.facts['peak_w']:.0f} W")  # fmt: skip
    if e.kind == "change_point":
        return (f"{name}: new device? Its power when on went from about"
                f" {e.facts['old_w']:.0f} W to {e.facts['new_w']:.0f} W")  # fmt: skip
    if e.kind == "offline":
        return f"{name}: not answering since {when}"
    if e.kind == "no_contact":
        n = e.facts["plugs"]
        return f"Lost contact with all {n} plugs at {when}: a power cut, or the hub's Wi-Fi is down"
    if e.kind == "logger_stopped":
        return f"No readings since {when}: the logger has stopped"
    if e.kind == "goal_drift":
        return (f"Heading over the carbon goal: about {e.facts['projected_kg']:.2f} of"
                f" {e.facts['goal_kg']:.2f} kgCO2e by the month's end")  # fmt: skip
    return f"{name}: timers changed on the plug (slots {e.facts['slots']})"


def decision_words(d: Decision) -> str:
    """A decision with its numbered choices, e.g. "ask: Switch it off?\n  1. Switch off"."""
    lines = [f"{d.action}: {d.text}"]
    for n, c in enumerate(d.choices, 1):
        timer = f" (off {c.off}, on {c.on}, {c.days})" if c.does == "set_timer" else ""
        lines.append(f"  {n}. {c.label}{timer}")
    return "\n".join(lines)


def week_table(profile: list[ProfileHour]) -> str:
    """Usual watts (the median): one row per hour, one column per day."""
    watts = {p.hour: p.median_w for p in profile}
    lines = ["Hour " + "".join(f"{day:>7}" for day in DAYS)]
    for hour in range(24):
        cells = [watts.get(day * 24 + hour) for day in range(7)]
        lines.append(f"{hour:02d}:00" + "".join(_cell(w) for w in cells))
    return "\n".join(lines)


def _cell(w: float | None) -> str:
    return f"{'-':>7}" if w is None else f"{w:>7.1f}"  # "-" = no readings in that hour


def outcome_words(o: Outcome) -> str:
    """What came of one signal, e.g. "Desk 2: ask: ...", then when it reaches the owner."""
    if o.decision is None:
        return f"{o.device}: {o.why}"
    head = f"{o.device}: {decision_words(o.decision)}"
    if o.question_id is not None:
        return f"{head}\n  Question {o.question_id}, {o.why}"
    return f"{head}\n  ({o.why})" if o.why else head
