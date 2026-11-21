"""Jobs: the work that runs on a timetable. The Watcher pass runs every 15 minutes.

A job that breaks never stops the jobs after it (FR-37): an AI outage must not stop the
timer check, learning or the heartbeat.
"""

from datetime import datetime, timedelta
from sqlite3 import Connection

from agentc import config, learn, responder, store, timers, watcher
from agentc.config import Config
from agentc.llm import Llm, OverBudget, Unreachable
from agentc.options import Money, Proposal
from agentc.policy import Policy
from agentc.watcher import MAX_GAP, Event, Run, Usual

LOOKBACK = timedelta(hours=24)  # how far back each pass looks for unusual runs
WATCH_EVERY = timedelta(minutes=15)
TIMERS_EVERY = timedelta(days=1)
LEARN_EVERY = timedelta(days=1)
MAX_TRIES = 3  # failed tries before an event is parked


def run_due(
    app: Connection, energy: Connection, cfg: Config, *, site_id: int, now: datetime,
    llm: Llm | None = None,
) -> tuple[list[Event], list[Proposal]]:  # fmt: skip
    """Run each job whose time has come. Passes missed while the app was down run once.

    llm: None when there is no API key. The Responder is then skipped and events wait.
    """
    events: list[Event] = []
    made: list[Proposal] = []
    if _due(app, site_id, "watch", WATCH_EVERY, now):
        events += watch_pass(app, energy, cfg, site_id=site_id, now=now)
        if llm:
            try:
                made = respond_pass(app, energy, cfg, llm, site_id=site_id, now=now)
            except Exception as err:  # a bug in the respond step: log it, run the other jobs
                store.add_audit(app, site_id=site_id, at=now, actor="code",
                                action="respond_pass_failed", ref="job:watch",
                                detail={"error": f"{type(err).__name__}: {err}"})  # fmt: skip
        store.set_last_run(app, site_id=site_id, job="watch", at=now)
    if _due(app, site_id, "timers", TIMERS_EVERY, now):
        events += timer_check(app, cfg, site_id=site_id, now=now)
        store.set_last_run(app, site_id=site_id, job="timers", at=now)
    if _due(app, site_id, "learn", LEARN_EVERY, now):
        learn_pass(app, energy, cfg, site_id=site_id, now=now)
        store.set_last_run(app, site_id=site_id, job="learn", at=now)
    return events, made


def _due(app: Connection, site_id: int, job: str, every: timedelta, now: datetime) -> bool:
    last = store.last_run(app, site_id=site_id, job=job)
    return last is None or now - last >= every


def watch_pass(
    app: Connection, energy: Connection, cfg: Config, *, site_id: int, now: datetime
) -> list[Event]:
    """Check every known plug with its own policy; save new events and return them."""
    macs = store.plug_macs(app, site_id=site_id)
    rounds = store.last_rounds(energy, n=watcher.PASS_ROUNDS, end=now)
    outages = watcher.offline_events(rounds, macs)
    found = [e for e in outages if not _reported(app, energy, site_id, e)]
    if stopped := watcher.logger_stopped(store.last_round(energy, end=now), now=now):
        found.append(stopped)  # its dedupe key is its start, so one event per stop
    for name, mac in macs.items():
        usual = watcher.usual_week(store.load_profile(app, mac=mac))  # no profile: never unusual
        readings = store.readings(energy, plug=name, start=now - LOOKBACK, end=now)
        policy = config.device(cfg, name, store.learned(app, mac=mac)).policy
        runs = watcher.unusual_runs(readings, usual, tz=cfg.tz, policy=policy)
        found += [watcher.unusual_use(mac, run) for run in runs]
    return [
        e for e in found
        if store.add_event(app, site_id=site_id, event=e, dedupe=watcher.dedupe_key(e, tz=cfg.tz))
    ]  # fmt: skip


def respond_pass(
    app: Connection, energy: Connection, cfg: Config, llm: Llm, *, site_id: int, now: datetime
) -> list[Proposal]:
    """Answer each finished unusual run once, oldest first. Returns the new proposals.

    A run still going waits for a later pass, so the Responder sees its full length.
    At a budget cap, or with the model out of reach, the pass stops and the events left
    wait for the next one. Any other error counts a try for that event only, so one bad
    event never blocks the rest; after MAX_TRIES it is parked (FR-37).
    """
    names = {mac: name for name, mac in store.plug_macs(app, site_id=site_id).items()}
    money = Money(cfg.tariff, cfg.month_kwh, cfg.levy, cfg.factor)
    made: list[Proposal] = []
    for eid, e in store.new_events(app, site_id=site_id, kind="unusual_use"):
        try:
            finished, p = _answer(app, energy, cfg, llm, eid, e, names, money, site_id, now)
        except (OverBudget, Unreachable):
            break
        except Exception as err:  # one bad event must not stop the others
            _failed(app, site_id, eid, err, now)
            continue
        if finished:
            made += [p] if p else []
            store.event_done(app, event_id=eid)
    return made


def _answer(
    app: Connection, energy: Connection, cfg: Config, llm: Llm, eid: int, e: Event,
    names: dict[str, str], money: Money, site_id: int, now: datetime,
) -> tuple[bool, Proposal | None]:  # fmt: skip
    """One event: (finished, proposal). Not finished while its run is still going.

    With under min_days of readings there is no answer: the usual hours of use are not
    known yet.
    """
    mac = e.plug_mac or ""  # an unusual run always has a plug
    device = config.device(cfg, names[mac], store.learned(app, mac=mac))
    policy = device.policy
    since = now - timedelta(days=policy.history_days)
    readings = store.readings(energy, plug=names[mac], start=since, end=now)
    usual = watcher.usual_week(store.load_profile(app, mac=mac))
    run = _run(readings, usual, e.start, tz=cfg.tz, policy=policy)
    if run and readings[-1][0] == run.end and now - run.end <= MAX_GAP:
        return False, None  # still going
    days = (now - readings[0][0]).days if readings else 0
    if run is None:
        why = "no run in the readings"
    elif days < policy.min_days:
        why = f"under {policy.min_days} days of readings"
    else:
        why = ""  # code may still find nothing to offer
    case = None if run is None or why else responder.gather(
        run, mac=mac, device=device, readings=readings, usual=usual,
        timers=store.load_timers(app, mac=mac), money=money,
        rejected=store.rejected(app, site_id=site_id, mac=mac, since=since), tz=cfg.tz,
        days=days,
    )  # fmt: skip
    if case is None:
        store.add_audit(app, site_id=site_id, at=now, actor="code", action="no_case",
                        ref=f"event:{eid}", detail={"why": why or "nothing to offer"})  # fmt: skip
        return True, None
    return True, responder.respond(case, llm=llm, db=app, site_id=site_id, event_id=eid,
                                   run_id=f"event:{eid}", tz=cfg.tz, now=now)  # fmt: skip


def _failed(app: Connection, site_id: int, eid: int, err: Exception, now: datetime) -> None:
    """Record a failed try; park the event at MAX_TRIES so it stops coming back."""
    ref, error = f"event:{eid}", {"error": f"{type(err).__name__}: {err}"}
    store.add_audit(app, site_id=site_id, at=now, actor="code", action="respond_failed",
                    ref=ref, detail=error)  # fmt: skip
    if store.event_failed(app, event_id=eid, limit=MAX_TRIES):
        store.add_audit(app, site_id=site_id, at=now, actor="code", action="event_parked",
                        ref=ref, detail={"tries": str(MAX_TRIES)})  # fmt: skip


def learn_pass(
    app: Connection, energy: Connection, cfg: Config, *, site_id: int, now: datetime
) -> dict[str, learn.Levels | None]:
    """Learn each plug's levels and save the values they give. plug name -> levels.

    A plug with under min_days of readings keeps what it had. One with a single level,
    or none, learns nothing, so it keeps the defaults.
    """
    found: dict[str, learn.Levels | None] = {}
    for name, mac in store.plug_macs(app, site_id=site_id).items():
        policy = config.device(cfg, name).policy
        readings = store.readings(energy, plug=name, end=now,
                                  start=now - timedelta(days=policy.history_days))  # fmt: skip
        if not readings or (now - readings[0][0]).days < policy.min_days:
            continue
        found[name] = lv = learn.levels([w for _, w in readings])
        store.save_learned(app, site_id=site_id, mac=mac, at=now,
                           values=learn.values(lv) if lv else {})  # fmt: skip
    return found


def _run(
    readings: list[tuple[datetime, float]], usual: dict[int, Usual], start: datetime, *, tz: str,
    policy: Policy,
) -> Run | None:  # fmt: skip
    """The run that holds this start, as long as it is now. None if the readings have none."""
    return next((r for r in watcher.unusual_runs(readings, usual, tz=tz, policy=policy)
                 if r.start <= start <= r.end), None)  # fmt: skip


def _reported(app: Connection, energy: Connection, site_id: int, e: Event) -> bool:
    """Is this outage already reported? Yes if an event exists and the plug has not answered
    since it started. For lost contact with every plug, any plug answering ends it."""
    last = store.last_event_start(app, site_id=site_id, kind=e.kind, plug_mac=e.plug_mac)
    if last is None:
        return False
    plug = e.facts.get("plug")  # None for lost contact with every plug
    return not store.answered(energy, plug=str(plug) if plug else None, start=last, end=e.start)


def timer_check(app: Connection, cfg: Config, *, site_id: int, now: datetime) -> list[Event]:
    """Read every plug's timers again. A timer that changed since the last read is an event."""
    saved = {
        mac: store.load_timers(app, mac=mac)
        for mac in store.plug_macs(app, site_id=site_id).values()
    }
    timers.import_all(app, site_id=site_id, plugs=cfg.plugs, now=now)
    found = [
        watcher.timer_mismatch(mac, saved.get(mac, []), store.load_timers(app, mac=mac), now=now)
        for mac in store.plug_macs(app, site_id=site_id).values()
    ]
    return [
        e for e in found
        if e and store.add_event(app, site_id=site_id, event=e,
                                 dedupe=watcher.dedupe_key(e, tz=cfg.tz))
    ]  # fmt: skip
