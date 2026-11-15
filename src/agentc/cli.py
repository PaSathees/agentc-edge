"""The `agentc` command. It only reads plugs; it never changes them."""

import argparse
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection
from zoneinfo import ZoneInfo

from agentc import calc, config, jobs, store, timers
from agentc.config import Config
from agentc.watcher import Event

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WHEN = "%a %d %b %H:%M"  # Tue 03 Nov 01:00


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentc")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("timers", help="read each plug's timers and show them in plain words")
    profile = commands.add_parser("profile", help="build one plug's week of use and save it")
    profile.add_argument("plug", help="a name from config.toml, e.g. Plug-2")
    profile.add_argument("--days", type=int, default=28, help="how far back to look")
    commands.add_parser("watch", help="run the checks that are due now and show new events")
    replay = commands.add_parser("replay", help="play past days through the Watcher")
    replay.add_argument("--days", type=int, default=14, help="how many days to play")
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    db = store.connect(cfg.app_db)
    site_id = store.add_site(db, name=cfg.site, tz=cfg.tz)
    if args.command == "profile":
        return show_profile(db, cfg, site_id=site_id, plug=args.plug, days=args.days)
    if args.command == "watch":
        return show_watch(db, cfg, site_id=site_id)
    if args.command == "replay":
        return show_replay(db, cfg, site_id=site_id, days=args.days)
    show_timers(db, site_id=site_id, plugs=cfg.plugs)
    return 0


def show_timers(db: Connection, *, site_id: int, plugs: dict[str, str]) -> None:
    """Read every plug, then print its timers. Problems go last."""
    problems = timers.import_all(db, site_id=site_id, plugs=plugs, now=datetime.now(UTC))
    no_answer = {p.plug for p in problems if p.kind == "unreachable"}
    for name in plugs:
        print(name)
        if name in no_answer:
            print("  Did not answer")
            continue
        mac = store.plug_mac(db, name=name)
        set_timers = [t for t in store.load_timers(db, mac=mac) if t.enable] if mac else []
        for t in set_timers:
            print(f"  Timer{t.slot}: {timers.describe(t)}")
        if not set_timers:
            print("  No timers set")
    if problems:
        print("\nNeeds you:")
        for p in problems:
            print(f"  - {p.text}")


def show_profile(db: Connection, cfg: Config, *, site_id: int, plug: str, days: int) -> int:
    """Build one plug's profile from energy.db, save it, and print it as a week table."""
    mac = store.plug_mac(db, name=plug)
    if mac is None:
        print(f"{plug} is not known yet. Run `agentc timers` first.")
        return 1
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    energy = store.open_readings(cfg.energy_db)
    profile = calc.build_profile(store.readings(energy, plug=plug, start=start, end=end), tz=cfg.tz)
    if profile is None:
        print(f"{plug}: not enough data yet. A profile needs {calc.MIN_DAYS} days.")
        return 1
    store.save_profile(db, site_id=site_id, mac=mac, profile=profile)
    print(week_table(profile))
    used = calc.kwh(store.counter(energy, plug=plug, start=start, end=end))
    print(f"\n{plug} used {used:.2f} kWh in the last {days} days.")
    return 0


def week_table(profile: list[calc.ProfileHour]) -> str:
    """Usual watts (the median): one row per hour, one column per day."""
    watts = {p.hour: p.median_w for p in profile}
    lines = ["Hour " + "".join(f"{day:>7}" for day in DAYS)]
    for hour in range(24):
        cells = [watts.get(day * 24 + hour) for day in range(7)]
        lines.append(f"{hour:02d}:00" + "".join(_cell(w) for w in cells))
    return "\n".join(lines)


def _cell(w: float | None) -> str:
    return f"{'-':>7}" if w is None else f"{w:>7.1f}"  # "-" = no readings in that hour


def show_watch(db: Connection, cfg: Config, *, site_id: int) -> int:
    """Run the jobs that are due now and print any new events."""
    energy = store.open_readings(cfg.energy_db)
    now = datetime.now(UTC)
    events = jobs.run_due(db, energy, site_id=site_id, tz=cfg.tz, plugs=cfg.plugs, now=now)
    names = {mac: name for name, mac in store.plug_macs(db, site_id=site_id).items()}
    for e in events:
        print(event_words(e, names, tz=cfg.tz))
    if not events:
        print("Nothing new.")
    return 0


def show_replay(db: Connection, cfg: Config, *, site_id: int, days: int) -> int:
    """Play past days through the Watcher, one pass every 15 min, and list the events.

    It works on a copy of app.db in memory, so the real app.db is not changed.
    """
    copy = sqlite3.connect(":memory:")
    db.backup(copy)
    energy = store.open_readings(cfg.energy_db)
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    names = {}
    for name, mac in store.plug_macs(copy, site_id=site_id).items():
        names[mac] = name
        readings = store.readings(energy, plug=name, start=start, end=end)
        profile = calc.build_profile(readings, tz=cfg.tz)
        if profile:
            store.save_profile(copy, site_id=site_id, mac=mac, profile=profile)
    events: list[Event] = []
    at = start + jobs.WATCH_EVERY
    while at <= end:
        events += jobs.watch_pass(copy, energy, site_id=site_id, tz=cfg.tz, now=at)
        at += jobs.WATCH_EVERY
    for n, e in enumerate(events, 1):
        print(f"{n}. {event_words(e, names, tz=cfg.tz)}")
    print(f"Events in {days} days: {len(events)}")
    return 0


def event_words(e: Event, names: dict[str, str], *, tz: str) -> str:
    """An event as the owner reads it."""
    when = e.start.astimezone(ZoneInfo(tz)).strftime(WHEN)
    name = names.get(e.plug_mac or "", "")
    if e.kind == "unusual_use":
        size = f"{e.facts['minutes']} min, peak {e.facts['peak_w']:.0f} W"
        return f"{name}: on at an unusual time from {when}, {size}"
    if e.kind == "offline":
        return f"{name}: not answering since {when}"
    if e.kind == "power_cut":
        return f"Power cut at {when}: all {e.facts['plugs']} plugs stopped answering"
    return f"{name}: timers changed on the plug (slots {e.facts['slots']})"
