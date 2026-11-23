"""Signals and decisions in plain words: as the owner reads them, and as the agent's brief."""

from zoneinfo import ZoneInfo

from agentc.owner import Decision
from agentc.watcher import Event

WHEN = "%a %d %b %H:%M"  # Tue 03 Nov 01:00


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
    return f"{name}: timers changed on the plug (slots {e.facts['slots']})"


def decision_words(d: Decision) -> str:
    """A decision with its numbered choices, e.g. "ask: Switch it off?\n  1. Switch off"."""
    lines = [f"{d.action}: {d.text}"]
    for n, c in enumerate(d.choices, 1):
        timer = f" (off {c.off}, on {c.on}, {c.days})" if c.does == "set_timer" else ""
        lines.append(f"  {n}. {c.label}{timer}")
    return "\n".join(lines)
