"""Jobs: the work that runs on a timetable. The Watcher pass runs every 15 minutes."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from sqlite3 import Connection

from agentc import calc, config, responder, store, timers, watcher
from agentc.config import Config
from agentc.llm import Llm, OverBudget
from agentc.options import Money, Proposal
from agentc.watcher import MAX_GAP, Event, Run, Usual

LOOKBACK = timedelta(hours=24)  # how far back each pass looks for unusual runs
WATCH_EVERY = timedelta(minutes=15)
TIMERS_EVERY = timedelta(days=1)
DAYS = 14  # days of readings behind each answer, at most; a rejection blocks a repeat as long


@dataclass(frozen=True)
class Ask:
    """What the respond step needs: the settings and the one door to the model."""

    cfg: Config
    llm: Llm


def run_due(
    app: Connection, energy: Connection, *, site_id: int, tz: str, plugs: dict[str, str],
    now: datetime, ask: Ask | None = None,
) -> tuple[list[Event], list[Proposal]]:  # fmt: skip
    """Run each job whose time has come. Passes missed while the app was down run once.

    ask: None when there is no API key. The Responder is then skipped and events wait.
    """
    events: list[Event] = []
    made: list[Proposal] = []
    if _due(app, site_id, "watch", WATCH_EVERY, now):
        events += watch_pass(app, energy, site_id=site_id, tz=tz, now=now)
        if ask:
            made = respond_pass(app, energy, ask, site_id=site_id, now=now)
        store.set_last_run(app, site_id=site_id, job="watch", at=now)
    if _due(app, site_id, "timers", TIMERS_EVERY, now):
        events += timer_check(app, site_id=site_id, tz=tz, plugs=plugs, now=now)
        store.set_last_run(app, site_id=site_id, job="timers", at=now)
    return events, made


def _due(app: Connection, site_id: int, job: str, every: timedelta, now: datetime) -> bool:
    last = store.last_run(app, site_id=site_id, job=job)
    return last is None or now - last >= every


def watch_pass(app: Connection, energy: Connection, *, site_id: int, tz: str, now: datetime
               ) -> list[Event]:  # fmt: skip
    """Check every known plug and save new events. Returns only the new ones."""
    macs = store.plug_macs(app, site_id=site_id)
    rounds = store.last_rounds(energy, n=watcher.PASS_ROUNDS, end=now)
    outages = watcher.offline_events(rounds, macs)
    found = [e for e in outages if not _reported(app, energy, site_id, e)]
    for name, mac in macs.items():
        usual = watcher.usual_week(store.load_profile(app, mac=mac))  # no profile: never unusual
        readings = store.readings(energy, plug=name, start=now - LOOKBACK, end=now)
        runs = watcher.unusual_runs(readings, usual, tz=tz)
        found += [watcher.unusual_use(mac, run) for run in runs]
    return [
        e for e in found
        if store.add_event(app, site_id=site_id, event=e, dedupe=watcher.dedupe_key(e, tz=tz))
    ]  # fmt: skip


def respond_pass(
    app: Connection, energy: Connection, ask: Ask, *, site_id: int, now: datetime
) -> list[Proposal]:
    """Answer each finished unusual run once, oldest first. Returns the new proposals.

    A run still going waits for a later pass, so the Responder sees its full length.
    Under a week of readings, there is no answer: the usual hours of use are not known yet.
    At a budget cap the pass stops, and the events left wait for the next one.
    """
    cfg, since = ask.cfg, now - timedelta(days=DAYS)
    names = {mac: name for name, mac in store.plug_macs(app, site_id=site_id).items()}
    money = Money(cfg.tariff, cfg.month_kwh, cfg.levy, cfg.factor)
    made: list[Proposal] = []
    for eid, e in store.new_events(app, site_id=site_id, kind="unusual_use"):
        mac = e.plug_mac or ""  # an unusual run always has a plug
        readings = store.readings(energy, plug=names[mac], start=since, end=now)
        usual = watcher.usual_week(store.load_profile(app, mac=mac))
        run = _run(readings, usual, e.start, tz=cfg.tz)
        if run and readings[-1][0] == run.end and now - run.end <= MAX_GAP:
            continue  # still going
        days = (now - readings[0][0]).days if readings else 0
        case = None if run is None or days < calc.MIN_DAYS else responder.gather(
            run, mac=mac, device=config.device(cfg, names[mac]), readings=readings, usual=usual,
            timers=store.load_timers(app, mac=mac), money=money,
            rejected=store.rejected(app, site_id=site_id, mac=mac, since=since), tz=cfg.tz,
            days=days,
        )  # fmt: skip
        if case is None:
            store.add_audit(app, site_id=site_id, at=now, actor="code", action="no_case",
                            ref=f"event:{eid}", detail={})  # fmt: skip
        else:
            try:
                p = responder.respond(case, llm=ask.llm, db=app, site_id=site_id, event_id=eid,
                                      run_id=f"event:{eid}", tz=cfg.tz, now=now)  # fmt: skip
            except OverBudget:
                break
            made += [p] if p else []
        store.event_done(app, event_id=eid)
    return made


def _run(
    readings: list[tuple[datetime, float]], usual: dict[int, Usual], start: datetime, *, tz: str
) -> Run | None:
    """The run that holds this start, as long as it is now. None if the readings have none."""
    return next((r for r in watcher.unusual_runs(readings, usual, tz=tz)
                 if r.start <= start <= r.end), None)  # fmt: skip


def _reported(app: Connection, energy: Connection, site_id: int, e: Event) -> bool:
    """Is this outage already reported? Yes if an event exists and the plug has not answered
    since it started. For a power cut, any plug answering ends it."""
    last = store.last_event_start(app, site_id=site_id, kind=e.kind, plug_mac=e.plug_mac)
    if last is None:
        return False
    plug = e.facts.get("plug")  # None for a power cut
    return not store.answered(energy, plug=str(plug) if plug else None, start=last, end=e.start)


def timer_check(
    app: Connection, *, site_id: int, tz: str, plugs: dict[str, str], now: datetime,
) -> list[Event]:  # fmt: skip
    """Read every plug's timers again. A timer that changed since the last read is an event."""
    saved = {
        mac: store.load_timers(app, mac=mac)
        for mac in store.plug_macs(app, site_id=site_id).values()
    }
    timers.import_all(app, site_id=site_id, plugs=plugs, now=now)
    found = [
        watcher.timer_mismatch(mac, saved.get(mac, []), store.load_timers(app, mac=mac), now=now)
        for mac in store.plug_macs(app, site_id=site_id).values()
    ]
    return [
        e for e in found
        if e and store.add_event(app, site_id=site_id, event=e, dedupe=watcher.dedupe_key(e, tz=tz))
    ]  # fmt: skip
