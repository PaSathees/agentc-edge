"""The `agentc` command. It only reads plugs; it never changes them."""

import argparse
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection
from zoneinfo import ZoneInfo

import anthropic

from agentc import calc, config, evals, jobs, llm, store, timers
from agentc.config import Config
from agentc.options import Proposal
from agentc.policy import explain
from agentc.responder import fix_words
from agentc.watcher import Event

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WHEN = "%a %d %b %H:%M"  # Tue 03 Nov 01:00
NO_KEY = "No ANTHROPIC_API_KEY: the Responder did not run. Run with `uv run --env-file .env`."


def main(argv: list[str] | None = None, *, send: llm.Send | None = None) -> int:
    """send: a stand-in for the model in tests. None means the real model, if a key is set."""
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
    replay.add_argument("--respond", action="store_true", help="ask the model too (costs money)")
    commands.add_parser("learn", help="learn each plug's levels from its readings, and show them")
    policy = commands.add_parser("policy", help="show each threshold, where it came from, and why")
    policy.add_argument("plug", nargs="?", help="a name from config.toml; none: the whole site")
    score = commands.add_parser("eval", help="score the Responder on the eval cases")
    score.add_argument("--live", action="store_true", help="ask the real model and record (costs)")
    score.add_argument("--samples", type=int, default=10, help="tries per case when live")
    args = parser.parse_args(argv)

    if args.command == "eval":  # needs no config.toml: the evals bring their own
        model = _send(send) if args.live else None
        if args.live and model is None:
            print(NO_KEY)
            return 1
        return show_eval(model, samples=args.samples)

    cfg = config.load(args.config)
    db = store.connect(cfg.app_db)
    site_id = store.add_site(db, name=cfg.site, tz=cfg.tz)
    if args.command == "policy":
        return show_policy(db, cfg, args.plug)
    if args.command == "learn":
        return show_learn(db, cfg, site_id=site_id)
    if args.command == "profile":
        return show_profile(db, cfg, site_id=site_id, plug=args.plug, days=args.days)
    if args.command == "watch":
        return show_watch(db, cfg, site_id=site_id, model=_llm(db, cfg, site_id, send))
    if args.command == "replay":
        model = _send(send) if args.respond else None
        if args.respond and model is None:
            print(NO_KEY)
            return 1
        return show_replay(db, cfg, site_id=site_id, days=args.days, send=model)
    show_timers(db, site_id=site_id, plugs=cfg.plugs)
    return 0


def show_policy(db: Connection, cfg: Config, plug: str | None) -> int:
    """Each threshold: its value, where it came from (default, learned, site, device), and why."""
    if plug and plug not in cfg.plugs:
        print(f"{plug} is not in {', '.join(cfg.plugs)}.")
        return 1
    if plug:
        mac = store.plug_mac(db, name=plug)
        device = config.device(cfg, plug, store.learned(db, mac=mac) if mac else None)
        print(f"{plug}: {device.name}")
        rows = explain(device.policy, site=cfg.own, device=device.own, learned=device.learned)
    else:
        print(f"Site: {cfg.site}")
        rows = explain(cfg.policy, site=cfg.own)
    for name, value, source, why in rows:
        print(f"  {name:<16}{value:>6g}  {source:<8}{why}")
    return 0


def show_learn(db: Connection, cfg: Config, *, site_id: int) -> int:
    """Learn each plug's levels now, and say what each one learned."""
    energy = store.open_readings(cfg.energy_db)
    found = jobs.learn_pass(db, energy, cfg, site_id=site_id, now=datetime.now(UTC))
    for name in store.plug_macs(db, site_id=site_id):
        if name not in found:
            print(f"{name}: not enough days of readings yet")
        elif (lv := found[name]) is None:
            print(f"{name}: one level or none, so it keeps the defaults")
        else:
            print(f"{name}: standby {lv.standby:g} W, active {lv.active:g} W")
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
    policy = config.device(cfg, plug).policy
    readings = store.readings(energy, plug=plug, start=start, end=end)
    profile = calc.build_profile(readings, tz=cfg.tz, policy=policy)
    if profile is None:
        print(f"{plug}: not enough data yet. A profile needs {policy.min_days} days.")
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


def _send(send: llm.Send | None) -> llm.Send | None:
    """The model to use: the stand-in, else the real one if a key is set, else None."""
    if send is None and os.environ.get("ANTHROPIC_API_KEY"):
        return llm.claude(anthropic.Anthropic())
    return send


def _llm(db: Connection, cfg: Config, site_id: int, send: llm.Send | None) -> llm.Llm | None:
    """The door to the model, with its calls traced in `db`. None without a model."""
    if (s := _send(send)) is None:
        return None
    return llm.Llm(db, site_id, cfg.tz, cfg.tiers, cfg.budget, s)


def show_watch(db: Connection, cfg: Config, *, site_id: int, model: llm.Llm | None) -> int:
    """Run the jobs that are due now; print new events, then new proposals."""
    energy = store.open_readings(cfg.energy_db)
    now = datetime.now(UTC)
    events, made = jobs.run_due(db, energy, cfg, site_id=site_id, now=now, llm=model)
    names = {mac: name for name, mac in store.plug_macs(db, site_id=site_id).items()}
    for e in events:
        print(event_words(e, names, tz=cfg.tz))
    for p in made:
        print(proposal_words(p))
    if not events and not made:
        print("Nothing new.")
    if parked := store.count_events(db, site_id=site_id, status="parked"):
        print(f"{parked} event{'s' if parked > 1 else ''} parked after {jobs.MAX_TRIES} failed"
              " tries: see the audit log")  # fmt: skip
    if model is None:
        print(NO_KEY)
    return 0


def show_eval(send: llm.Send | None, *, samples: int) -> int:
    """Score each live eval case; 1 if any fails. send: ask the model; None: replay."""
    try:
        scores = evals.run(send, samples=samples)
    except evals.Stale as err:
        print(err)
        return 1
    for s in scores:
        print(s.line())
    return 0 if all(s.passed for s in scores) else 1


def show_replay(
    db: Connection, cfg: Config, *, site_id: int, days: int, send: llm.Send | None
) -> int:
    """Play past days through the Watcher, one pass every 15 min, and list the events.

    send: also ask the model after each pass, and list the proposals. It works on a
    copy of app.db in memory, so the real app.db is not changed. Profiles and learned
    values come from the same days it plays, so it is a little kinder than real life.
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
        profile = calc.build_profile(readings, tz=cfg.tz, policy=config.device(cfg, name).policy)
        if profile:
            store.save_profile(copy, site_id=site_id, mac=mac, profile=profile)
    jobs.learn_pass(copy, energy, cfg, site_id=site_id, now=end)  # in the copy only
    model = _llm(copy, cfg, site_id, send) if send else None  # its calls go in the copy too
    events: list[Event] = []
    made: list[Proposal] = []
    at = start + jobs.WATCH_EVERY
    while at <= end:
        events += jobs.watch_pass(copy, energy, cfg, site_id=site_id, now=at)
        if model:
            made += jobs.respond_pass(copy, energy, cfg, model, site_id=site_id, now=at)
        at += jobs.WATCH_EVERY
    for n, e in enumerate(events, 1):
        print(f"{n}. {event_words(e, names, tz=cfg.tz)}")
    print(f"Events in {days} days: {len(events)}")
    if model:
        for p in made:
            print(proposal_words(p))
        usd = store.llm_usd(copy, site_id=site_id)
        print(f"Proposals: {len(made)}. Model calls cost USD {usd:.4f}.")
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
    if e.kind == "no_contact":
        n = e.facts["plugs"]
        return f"Lost contact with all {n} plugs at {when}: a power cut, or the hub's Wi-Fi is down"
    if e.kind == "logger_stopped":
        return f"No readings since {when}: the logger has stopped"
    return f"{name}: timers changed on the plug (slots {e.facts['slots']})"


def proposal_words(p: Proposal) -> str:
    """A proposal as the owner reads it."""
    fix = f"{fix_words(p.changes)}. Saves LKR {p.lkr:.1f} a week."
    return f"Proposal for {p.device}: {fix}\n  {p.reason}"
