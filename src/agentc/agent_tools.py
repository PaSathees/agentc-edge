"""The housemate agent's read tools (FR-15 v2, ADR-007).

Each tool answers in a few lines of plain words, worked out by code: the agent never gets
raw minute readings to scan, and never does a sum itself. No tool changes anything. A
bad argument comes back as "Error: ..." text, so the agent can try again.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from sqlite3 import Connection
from typing import Any
from zoneinfo import ZoneInfo

from agentc import config, house, readings, store, watcher
from agentc.calc import TimeOfUse, kgco2e, saving_lkr, tou_rate
from agentc.config import Config

WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SAME = 0.3  # a reading within this share of a segment's level (or 2 W) stays in the segment
NOW_WINDOW = timedelta(minutes=5)  # "now" is the last reading this recent
PAST = timedelta(days=14)  # how far back past_signals looks
LOCAL = "%Y-%m-%d %H:%M"  # how the agent writes a time for the cost tool
KINDS = {
    "left_on": "left on", "unusual_use": "unusual use",
    "automation_drift": "a timer switched it on for nothing", "override": "on after its off timer",
    "change_point": "new device?", "offline": "not answering", "timer_mismatch": "timers changed",
}  # fmt: skip


def segments(readings: list[tuple[datetime, float]], *, tz: str) -> str:
    """Readings as level stretches: "Mon 01:37 to 01:50: about 30 W". Gaps over 5 min shown."""
    if not readings:
        return "No readings."
    zone, lines = ZoneInfo(tz), []
    start, level, last, n = readings[0][0], readings[0][1], readings[0][0], 1

    def close(until: datetime) -> None:
        a, b = start.astimezone(zone), until.astimezone(zone)
        lines.append(f"{a:%a %H:%M} to {b:%H:%M}: about {level:.0f} W")

    for at, w in readings[1:]:
        if at - last > watcher.MAX_GAP:
            close(last)
            gap_a, gap_b = (last + timedelta(minutes=1)).astimezone(zone), at - timedelta(minutes=1)
            lines.append(f"{gap_a:%a %H:%M} to {gap_b.astimezone(zone):%H:%M}: no readings")
            start, level, n = at, w, 1
        elif abs(w - level) > max(2.0, SAME * level):
            close(last)
            start, level, n = at, w, 1
        else:
            level, n = (level * n + w) / (n + 1), n + 1
        last = at
    close(last)
    return "\n".join(lines)


@dataclass(frozen=True)
class Tools:
    app: Connection
    energy: Connection
    cfg: Config
    site_id: int
    now: datetime

    def run(self, name: str, args: dict[str, Any]) -> str:
        """Run one tool by name. Anything wrong comes back as an "Error: ..." line."""
        tools: dict[str, Callable[..., str]] = {
            "device": self.device, "power": self.power, "usual": self.usual,
            "house_now": self.house_now, "past_signals": self.past_signals, "cost": self.cost,
        }  # fmt: skip
        if name not in tools:
            return f"Error: no tool named {name!r}"
        try:
            return tools[name](**args)
        except LookupError as err:
            return f"Error: {err.args[0]}"
        except (TypeError, ValueError) as err:
            return f"Error: {err}"

    def _plug(self, device: str) -> tuple[str, str]:
        """The plug name and MAC for a device name, as the agent knows it."""
        names = {config.device(self.cfg, p).name: p for p in self.cfg.plugs}
        if device not in names:
            raise LookupError(f"no device named {device!r}. Known: {', '.join(names)}")
        plug = names[device]
        if (mac := store.plug_mac(self.app, name=plug)) is None:
            raise LookupError(f"{device} has not been read yet")
        return plug, mac

    def device(self, device: str) -> str:
        """The device card: what it is, access, rules, timers, the plug's own automations."""
        plug, _ = self._plug(device)
        return card(self.app, self.cfg, plug)

    def power(self, device: str, hours: int) -> str:
        """Its power over the last hours, as level stretches."""
        plug, _ = self._plug(device)
        span = timedelta(hours=min(max(int(hours), 1), 24))
        return segments(readings.power(self.energy, plug=plug, start=self.now - span, end=self.now),
                        tz=self.cfg.tz)  # fmt: skip

    def usual(self, device: str, day: str, start: str, end: str) -> str:
        """The usual power for each hour from start to end on a day of the week."""
        _, mac = self._plug(device)
        usual = watcher.usual_week(store.load_profile(self.app, mac=mac))
        if not usual:
            return f"No usual week yet for {device}."
        d = WEEK.index(day.capitalize())
        hours = range(int(start[:2]), int(end[:2]) or 24)
        lines = []
        for h in hours:
            u = usual.get(d * 24 + h)
            text = (
                f"usually about {u.watts:.0f} W, on {u.on_share:.0%} of the time"
                if u
                else "no data"
            )
            lines.append(f"{WEEK[d]} {h:02d}:00: {text}")
        return "\n".join(lines)

    def house_now(self) -> str:
        """What every device draws now (its last reading in the last 5 minutes)."""
        lines = []
        for plug in self.cfg.plugs:
            name = config.device(self.cfg, plug).name
            recent = readings.power(self.energy, plug=plug, start=self.now - NOW_WINDOW,
                                    end=self.now + timedelta(seconds=1))  # fmt: skip
            lines.append(f"{name}: {recent[-1][1]:.0f} W" if recent else
                         f"{name}: no reading in the last 5 min")  # fmt: skip
        return "\n".join(lines)

    def past_signals(self, device: str) -> str:
        """This device's signals in the last 14 days, oldest first."""
        _, mac = self._plug(device)
        zone = ZoneInfo(self.cfg.tz)
        found = [
            e for e in store.load_events(self.app, site_id=self.site_id)
            if e.plug_mac == mac and self.now - PAST <= e.start < self.now
        ]  # fmt: skip
        if not found:
            return "None in the last 14 days."
        return "\n".join(f"{e.start.astimezone(zone):%a %d %b %H:%M}: {KINDS.get(e.kind, e.kind)}"
                         for e in found)  # fmt: skip

    def cost(self, device: str, start: str, end: str) -> str:
        """kWh, LKR and kgCO2e the device used from start to end (local YYYY-MM-DD HH:MM)."""
        plug, _ = self._plug(device)
        zone = ZoneInfo(self.cfg.tz)
        try:
            a = datetime.strptime(start, LOCAL).replace(tzinfo=zone)
            b = datetime.strptime(end, LOCAL).replace(tzinfo=zone)
        except ValueError:
            raise ValueError("times are YYYY-MM-DD HH:MM, local time") from None
        rows = readings.power(self.energy, plug=plug, start=a, end=b)
        kwh = sum(w * min(_next(rows, i, b) - at, watcher.MAX_GAP).total_seconds()
                  for i, (at, w) in enumerate(rows)) / 3600 / 1000  # fmt: skip
        cfg, parts = self.cfg, [f"{kwh:.3f} kWh"]
        if isinstance(cfg.tariff, TimeOfUse):
            parts.append(f"LKR {kwh * tou_rate(cfg.tariff, a, tz=cfg.tz) / (1 - cfg.levy):.1f}")
        elif cfg.tariff:
            parts.append(f"LKR {saving_lkr(cfg.month_kwh, kwh, cfg.tariff, levy=cfg.levy):.1f}")
        if cfg.factor:
            parts.append(f"{kgco2e(kwh, cfg.factor):.3f} kgCO2e")
        missing = [
            x for x, set_ in (("tariff", cfg.tariff), ("carbon factor", cfg.factor)) if not set_
        ]
        note = f" (no {' or '.join(missing)} set)" if missing else ""
        return f"From {a:%a %H:%M} to {b:%a %H:%M}: {', '.join(parts)}{note}"


def _next(readings: list[tuple[datetime, float]], i: int, end: datetime) -> datetime:
    """When reading i stops counting: the next reading, else a minute later (but not past end)."""
    if i + 1 < len(readings):
        return readings[i + 1][0]
    return min(readings[i][0] + timedelta(minutes=1), end.astimezone(UTC))


def card(app: Connection, cfg: Config, plug: str) -> str:
    """The house model's text for one plug's device, as the agent and the owner read it."""
    mac = store.plug_mac(app, name=plug)
    saved = store.load_timers(app, mac=mac) if mac else []
    found = [h for _, h in store.habits(app, mac=mac)] if mac else []
    c = store.load_card(app, mac=mac) if mac else house.Card()
    auto = store.load_automations(app, mac=mac) if mac else house.NO_AUTOMATIONS
    return house.card(config.device(cfg, plug), c, timers=saved, habits=found, automations=auto)
