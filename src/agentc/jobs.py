"""Jobs: the work that runs on a timetable. The Watcher pass runs every 15 minutes."""

from datetime import datetime, timedelta
from sqlite3 import Connection

from agentc import store, timers, watcher
from agentc.watcher import Event

LOOKBACK = timedelta(hours=24)  # how far back each pass looks for unusual runs
WATCH_EVERY = timedelta(minutes=15)
TIMERS_EVERY = timedelta(days=1)


def run_due(
    app: Connection, energy: Connection, *, site_id: int, tz: str, plugs: dict[str, str],
    now: datetime,
) -> list[Event]:  # fmt: skip
    """Run each job whose time has come. Passes missed while the app was down run once."""
    events: list[Event] = []
    if _due(app, site_id, "watch", WATCH_EVERY, now):
        events += watch_pass(app, energy, site_id=site_id, tz=tz, now=now)
        store.set_last_run(app, site_id=site_id, job="watch", at=now)
    if _due(app, site_id, "timers", TIMERS_EVERY, now):
        events += timer_check(app, site_id=site_id, tz=tz, plugs=plugs, now=now)
        store.set_last_run(app, site_id=site_id, job="timers", at=now)
    return events


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
