"""The `agentc` command. It only reads plugs; it never changes them."""

import argparse
import ipaddress
import os
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection
from zoneinfo import ZoneInfo

import anthropic

from agentc import (
    agent,
    agent_tools,
    calc,
    config,
    evals,
    house,
    jobs,
    llm,
    logger,
    notify,
    readings,
    store,
    timers,
)
from agentc.config import Config
from agentc.devices import tasmota
from agentc.devices.base import describe
from agentc.owner import Outcome
from agentc.policy import explain
from agentc.watcher import Event
from agentc.words import decision_words, event_words, outcome_words, week_table

REPLAY_EVERY = timedelta(
    minutes=15
)  # replay steps coarser than the 5-min pass: same events, faster
NO_KEY = "No ANTHROPIC_API_KEY in .env: the housemate did not look. Add it, then restart."


def main(
    argv: list[str] | None = None, *, step: llm.StepSend | None = None,
    publish: notify.Publish | None = None,
) -> int:  # fmt: skip
    """step, publish: stand-ins for the model and ntfy in tests. None means the real ones,
    if a key and an [owner] are set."""
    args = _parser().parse_args(argv)

    if args.command == "find":  # before config.toml exists: it helps write it
        return show_find(args.network, args.config)
    if args.command == "eval":  # needs no config.toml: the evals bring their own
        model = _step(step) if args.live else None
        if args.live and model is None:
            print(NO_KEY)
            return 1
        return show_eval(model, samples=args.samples)

    cfg = config.load(args.config)
    if args.command == "log":  # the logger: no app.db, nothing but readings
        logger.run(cfg.energy_db, cfg.plugs)
        return 0
    db = store.connect(cfg.app_db)
    site_id = store.add_site(db, name=cfg.site, tz=cfg.tz)
    if args.command == "policy":
        return show_policy(db, cfg, args.plug)
    if args.command == "look":
        return show_look(db, cfg, site_id=site_id, event_id=args.event, step=step, at=args.at)
    if args.command == "questions":
        return show_questions(db, cfg, site_id=site_id)
    if args.command == "answer":
        return show_answer(db, cfg, site_id=site_id, question=args.question, choice=args.choice)
    if args.command == "house":
        return show_house(db, cfg, args.plug, args.set, site_id=site_id)
    if args.command == "learn":
        return show_learn(db, cfg, site_id=site_id)
    if args.command == "profile":
        return show_profile(db, cfg, site_id=site_id, plug=args.plug, days=args.days)
    if args.command == "ping":
        return show_ping(cfg, _sender(cfg, publish))
    if args.command in ("watch", "run"):
        model, sender = _llm(db, cfg, site_id, step), _sender(cfg, publish)
        if args.command == "run":
            return show_run(db, cfg, site_id=site_id, model=model, sender=sender)
        return show_watch(db, cfg, site_id=site_id, model=model, sender=sender)
    if args.command == "replay":
        model = _step(step) if args.look else None
        if args.look and model is None:
            print(NO_KEY)
            return 1
        return show_replay(db, cfg, site_id=site_id, days=args.days, step=model)
    show_timers(db, site_id=site_id, plugs=cfg.plugs)
    return 0


def show_house(
    db: Connection, cfg: Config, plug: str | None, changes: list[str], *, site_id: int
) -> int:
    """Each device card, as the agent will read it, or change one plug's card.

    Reads only app.db, never the plugs. A change is the owner's own, so it is saved at
    once, with an audit row.
    """
    if plug is not None and plug not in cfg.plugs:
        print(f"{plug}: not in config.toml.")
        return 1
    if plug and changes:
        if (mac := store.plug_mac(db, name=plug)) is None:
            print(f"{plug}: not seen yet. Run `agentc timers` first.")
            return 1
        try:
            new = house.updated(store.load_card(db, mac=mac), changes)
        except ValueError as err:
            print(err)
            return 1
        now = datetime.now(UTC)
        store.save_card(db, site_id=site_id, mac=mac, card=new, now=now)
        store.add_audit(db, site_id=site_id, at=now, actor="owner", action="card_set",
                        ref=f"plug:{plug}", detail={"set": "; ".join(changes)})  # fmt: skip
    names = [plug] if plug else cfg.plugs
    print("\n\n".join(agent_tools.card(db, cfg, name) for name in names))
    return 0


def _parser() -> argparse.ArgumentParser:
    """The commands and their arguments."""
    parser = argparse.ArgumentParser(prog="agentc")
    parser.add_argument("--config", type=Path,
                        default=Path(os.environ.get("AGENTC_CONFIG", "config.toml")))  # fmt: skip
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("timers", help="read each plug's timers and show them in plain words")
    profile = commands.add_parser("profile", help="build one plug's week of use and save it")
    profile.add_argument("plug", help="a name from config.toml, e.g. Plug-2")
    profile.add_argument("--days", type=int, default=28, help="how far back to look")
    commands.add_parser(
        "watch", help="run the due jobs once and show new signals (debugging; the service is `run`)"
    )
    replay = commands.add_parser(
        "replay", help="play past days through the signals (and the housemate)"
    )
    replay.add_argument("--days", type=int, default=14, help="how many days to play")
    replay.add_argument("--look", action="store_true", help="the housemate looks too (costs)")
    commands.add_parser("learn", help="learn each plug's levels from its readings, and show them")
    card = commands.add_parser("house", help="show or set what the system knows about devices")
    card.add_argument("plug", nargs="?", help="a name from config.toml; none: every plug")
    card.add_argument("set", nargs="*", metavar="key=value",
                      help="change the card: kind, room, what, notes, must_run (yes or no), "
                           "why.N (why timer N is set)")  # fmt: skip
    peek = commands.add_parser("look", help="run the housemate agent on one saved event (costs)")
    peek.add_argument("event", type=int, help="the event's id in app.db")
    peek.add_argument("--at", help="look as of this local time, YYYY-MM-DD HH:MM (default: now)")
    where = commands.add_parser("find", help="find the Tasmota plugs on the home network")
    where.add_argument("network", nargs="?", help="e.g. 192.168.1.0/24 (default: this one)")
    commands.add_parser("log", help="the logger: read every plug each minute, for good")
    commands.add_parser("run", help="run the due jobs every minute, for good (the jobs service)")
    commands.add_parser("ping", help="send a test message to the phone, with a tap test")
    commands.add_parser("questions", help="the housemate's open questions, numbered")
    tap = commands.add_parser("answer", help="answer a question, as a tap on the phone would")
    tap.add_argument("question", type=int, help="the question's number")
    tap.add_argument("choice", type=int, help="the choice's number, from 1")
    policy = commands.add_parser("policy", help="show each threshold, where it came from, and why")
    policy.add_argument("plug", nargs="?", help="a name from config.toml; none: the whole site")
    score = commands.add_parser("eval", help="score the housemate on the eval cases")
    score.add_argument("--live", action="store_true", help="ask the real model and record (costs)")
    score.add_argument("--samples", type=int, default=5, help="tries per case when live")
    return parser


def show_look(
    db: Connection, cfg: Config, *, site_id: int, event_id: int, step: llm.StepSend | None,
    at: str | None = None,
) -> int:  # fmt: skip
    """Run the housemate agent on one saved event, as of now (or `at`), and show what it decided.

    It reads only; the model calls are traced in app.db like any other.
    """
    if (e := store.get_event(db, event_id=event_id)) is None:
        print(f"No event {event_id}.")
        return 1
    names = {mac: name for name, mac in store.plug_macs(db, site_id=site_id).items()}
    if e.plug_mac not in names:
        print(f"Event {event_id} is about the whole site: there is no device to look at.")
        return 1
    if (step := _step(step)) is None:
        print(NO_KEY)
        return 1
    now = (
        datetime.strptime(at, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(cfg.tz))
        if at
        else datetime.now(UTC)
    )
    run_id = f"look:{event_id}:{now:%Y%m%dT%H%M}"
    model = llm.Llm(db, site_id, cfg.tz, cfg.tiers, cfg.budget, step)
    tools = agent_tools.Tools(db, readings.connect(cfg.energy_db), cfg, site_id, now)
    device = config.device(cfg, names[e.plug_mac]).name
    signal = event_words(e, {e.plug_mac: device}, tz=cfg.tz)
    print(f"Signal: {signal}")
    result = agent.look(signal, names[e.plug_mac], tools=tools, llm=model, run_id=run_id)
    print(f"Tools used: {', '.join(result.tools) or 'none'}")
    if (d := result.decision) is None:
        print(f"No decision: {result.blocked}")
    else:
        print(decision_words(d))
    print(f"Model calls cost USD {store.llm_usd(db, site_id=site_id, run_id=run_id):.4f}.")
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
    energy = readings.connect(cfg.energy_db)
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
            print(f"  Timer{t.slot}: {describe(t)}")
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
    energy = readings.connect(cfg.energy_db)
    policy = config.device(cfg, plug).policy
    rows = readings.power(energy, plug=plug, start=start, end=end)
    profile = calc.build_profile(rows, tz=cfg.tz, policy=policy)
    if profile is None:
        print(f"{plug}: not enough data yet. A profile needs {policy.min_days} days.")
        return 1
    store.save_profile(db, site_id=site_id, mac=mac, profile=profile)
    print(week_table(profile))
    used = calc.kwh(readings.counter(energy, plug=plug, start=start, end=end))
    print(f"\n{plug} used {used:.2f} kWh in the last {days} days.")
    return 0


def _step(step: llm.StepSend | None) -> llm.StepSend | None:
    """The model to use: the stand-in, else the real one if a key is set, else None."""
    if step is None and os.environ.get("ANTHROPIC_API_KEY"):
        return llm.claude_step(anthropic.Anthropic())
    return step


def _llm(db: Connection, cfg: Config, site_id: int, step: llm.StepSend | None) -> llm.Llm | None:
    """The door to the model, with its calls traced in `db`. None without a model."""
    if (s := _step(step)) is None:
        return None
    return llm.Llm(db, site_id, cfg.tz, cfg.tiers, cfg.budget, s)


NO_HUB = "No AGENTC_HUB in .env: nothing goes to the phone. Questions wait in the inbox."
RUN_EVERY = 60  # seconds between passes of `agentc run`; each job keeps its own timetable


def _sender(cfg: Config, publish: notify.Publish | None) -> jobs.Sender | None:
    """How messages reach the phone, or None: no AGENTC_HUB in .env."""
    if cfg.owner is None:
        return None
    secret = config.secret(cfg)
    return jobs.Sender(publish or notify.ntfy(cfg.owner, secret), cfg.owner.inbox_url, secret)


def show_find(network: str | None, cfg_path: Path) -> int:
    """Ask every address on the home network if it is a Tasmota plug; print config lines for
    the new ones with an energy sensor. Reads only."""
    net = ipaddress.IPv4Network(network, strict=False) if network else tasmota.home_network()
    print(f"Asking {net.num_addresses - 2} addresses in {net} ...")
    found = sorted(tasmota.find(str(h) for h in net.hosts()),
                   key=lambda f: ipaddress.IPv4Address(f[0].split(":")[0]))  # fmt: skip
    if not found:
        print("No Tasmota plugs found. Are they on this network, with Wi-Fi set up?")
        return 1
    known = (
        {ip: name for name, ip in config.load(cfg_path).plugs.items()} if cfg_path.is_file() else {}
    )
    metered = [(ip, i) for ip, i in found if logger.read_plug("", ip, "", timeout_s=2)[-1] is None]
    new = [(ip, i) for ip, i in metered if ip not in known]
    print(f"Found {len(found)} Tasmota devices, {len(metered)} with an energy sensor.")
    if new:
        print("Paste the new ones under [plugs] in config.toml, and rename as you like:")
        for n, (ip, info) in enumerate(new, len(known) + 1):
            print(f'"Plug-{n}" = "{ip}"  # {info.hostname}')
    if had := [known[ip] for ip, _ in metered if ip in known]:
        print(f"Already in config.toml: {', '.join(had)}")
    if bare := [f"{ip} ({i.hostname})" for ip, i in found if (ip, i) not in metered]:
        print(f"No energy sensor, so of no use here: {', '.join(bare)}")
    print(f"\nFor the phone, put this box's address in .env: AGENTC_HUB=http://{tasmota.this_ip()}")
    return 0


def show_ping(cfg: Config, sender: jobs.Sender | None) -> int:
    """One test message to the phone: is ntfy set up, and does a tap reach the hub?"""
    if sender is None or cfg.owner is None:
        print(NO_HUB)
        return 1
    name = notify.topic(cfg.owner, sender.secret)
    print(f"In the ntfy app: add server {cfg.owner.phone_ntfy}, subscribe to {name}")
    print(f"Inbox: {cfg.owner.inbox_url}/")
    at = datetime.now(ZoneInfo(cfg.tz)).strftime("%H:%M:%S")
    try:
        sender.publish(notify.ping(hub=sender.base_url, secret=sender.secret, at=at))
    except notify.NotSent as err:
        print(err)
        return 1
    print(f'Sent at {at}. On the phone, tap "Tap test": a "Tap works" message should follow.')
    return 0


def show_watch(
    db: Connection, cfg: Config, *, site_id: int, model: llm.Llm | None,
    sender: jobs.Sender | None = None, quiet: bool = False,
) -> int:  # fmt: skip
    """Run the jobs that are due now; print new signals, what the housemate made of them,
    and what went to the phone. quiet: say nothing when nothing is new (for `run`)."""
    energy = readings.connect(cfg.energy_db)
    now = datetime.now(UTC)
    events, looked, sent = jobs.run_due(db, energy, cfg, site_id=site_id, now=now, llm=model,
                                        sender=sender)  # fmt: skip
    names = _names(db, cfg, site_id)
    for e in events:
        print(event_words(e, names, tz=cfg.tz))
    for o in looked:
        print(outcome_words(o))
    for m in sent:
        print(f"Sent: {m.title}")
    if not events and not looked and not sent and not quiet:
        print("Nothing new.")
    if parked := store.count_events(db, site_id=site_id, status="parked"):
        print(f"{parked} event{'s' if parked > 1 else ''} parked after {jobs.MAX_TRIES} failed"
              " tries: see the audit log")  # fmt: skip
    if quiet:
        return 0
    if model is None:
        print(NO_KEY)
    if sender is None:
        print(NO_HUB)
    return 0


def show_run(
    db: Connection, cfg: Config, *, site_id: int, model: llm.Llm | None,
    sender: jobs.Sender | None, rounds: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:  # fmt: skip
    """Run the due jobs every minute, for good (the jobs service). Prints only what is new."""
    print(f"Running every {RUN_EVERY} s. {'' if model else NO_KEY} {'' if sender else NO_HUB}")
    done = 0
    while rounds is None or done < rounds:
        try:
            if cfg.energy_db.exists():  # on a first start the logger makes it within a minute
                show_watch(db, cfg, site_id=site_id, model=model, sender=sender, quiet=True)
        except Exception as err:  # e.g. the database busy for too long: try again next minute
            print(f"Pass failed, trying again in {RUN_EVERY} s: {type(err).__name__}: {err}")
        done += 1
        sleep(RUN_EVERY)
    return 0


def _names(db: Connection, cfg: Config, site_id: int) -> dict[str, str]:
    """Plug MAC -> the device's name (not the plug's)."""
    return {mac: config.device(cfg, name).name
            for name, mac in store.plug_macs(db, site_id=site_id).items()}  # fmt: skip


def show_questions(db: Connection, cfg: Config, *, site_id: int) -> int:
    """The open questions, as the phone shows them, with when each is due."""
    names, zone = _names(db, cfg, site_id), ZoneInfo(cfg.tz)
    open_ = store.questions(db, site_id=site_id, status="open")
    for q in open_:
        due = q.send_at.astimezone(zone)
        print(f"Question {q.id}, {names[q.plug_mac]}, from {due:%a %d %b %H:%M}:")
        print(f"  {decision_words(q.decision)}")
    if not open_:
        print("No open questions.")
    return 0


def show_answer(db: Connection, cfg: Config, *, site_id: int, question: int, choice: int) -> int:
    """Answer a question, as a tap would; say what was done."""
    try:
        print(jobs.answer(db, cfg, site_id=site_id, question_id=question, choice=choice,
                          now=datetime.now(UTC)))  # fmt: skip
    except LookupError as err:
        print(err.args[0])
        return 1
    return 0


def show_eval(step: llm.StepSend | None, *, samples: int) -> int:
    """Score each live eval case; 1 if any fails. step: ask the model; None: replay."""
    try:
        scores = evals.run(step, samples=samples)
    except evals.Stale as err:
        print(err)
        return 1
    for s in scores:
        print(s.line())
    return 0 if all(s.passed for s in scores) else 1


def show_replay(
    db: Connection, cfg: Config, *, site_id: int, days: int, step: llm.StepSend | None
) -> int:
    """Play past days through the watch pass, one every 15 min (REPLAY_EVERY), and list them.

    step: the housemate looks too, and its outcomes are listed. It works on a copy of
    app.db in memory, so the real app.db is not changed. Profiles and learned values come
    from the same days it plays, so it is a little kinder than real life.
    """
    copy = sqlite3.connect(":memory:")
    db.backup(copy)
    energy = readings.connect(cfg.energy_db)
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    for name, mac in store.plug_macs(copy, site_id=site_id).items():
        rows = readings.power(energy, plug=name, start=start, end=end)
        profile = calc.build_profile(rows, tz=cfg.tz, policy=config.device(cfg, name).policy)
        if profile:
            store.save_profile(copy, site_id=site_id, mac=mac, profile=profile)
    jobs.learn_pass(copy, energy, cfg, site_id=site_id, now=end)  # in the copy only
    model = _llm(copy, cfg, site_id, step) if step else None  # its calls go in the copy too
    events: list[Event] = []
    looked: list[Outcome] = []
    at = start + REPLAY_EVERY
    while at <= end:
        events += jobs.watch_pass(copy, energy, cfg, site_id=site_id, now=at)
        if model:
            looked += jobs.look_pass(copy, energy, cfg, model, site_id=site_id, now=at)
        at += REPLAY_EVERY
    names = _names(copy, cfg, site_id)
    for n, e in enumerate(events, 1):
        print(f"{n}. {event_words(e, names, tz=cfg.tz)}")
    print(f"Signals in {days} days: {len(events)}")
    if model:
        for o in looked:
            print(outcome_words(o))
        asked = sum(o.question_id is not None for o in looked)
        usd = store.llm_usd(copy, site_id=site_id)
        print(f"Looked at {sum(o.looked for o in looked)}, questions {asked}."
              f" Model calls cost USD {usd:.4f}.")  # fmt: skip
    return 0
