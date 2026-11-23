"""Jobs: the work that runs on a timetable. The watch pass runs every 5 minutes.

A job that breaks never stops the jobs after it (FR-37): an AI outage must not stop the
timer check, learning or the heartbeat.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from sqlite3 import Connection
from zoneinfo import ZoneInfo

from agentc import agent, agent_tools, config, house, learn, owner, store, timers, watcher, words
from agentc.config import Config
from agentc.llm import Llm, OverBudget, Unreachable
from agentc.owner import Decision
from agentc.policy import Policy
from agentc.watcher import Event

LOOKBACK = timedelta(hours=24)  # how far back each pass looks for unusual runs
WATCH_EVERY = timedelta(minutes=5)  # signals should come while it happens
TIMERS_EVERY = timedelta(days=1)
LEARN_EVERY = timedelta(days=1)  # learning and the change check
RECENT = timedelta(days=2)  # a change point compares these last days with the ones before
MAX_TRIES = 3  # failed tries before an event is parked
LOOKED = ("left_on", "unusual_use", "automation_drift", "override", "change_point")  # the rest
# (offline, lost contact, logger stopped, timers changed) are notices: no AI needed
PATTERN = timedelta(days=7)  # a timer's drift is asked about once a week, if it repeats
QUESTION_LIFE = timedelta(hours=12)  # an unanswered question expires this long after it is due
REMIND = timedelta(hours=2)  # "remind me later"


@dataclass(frozen=True)
class Outcome:
    """What came of one signal."""

    event_id: int
    device: str
    decision: Decision | None  # None: skipped by code, or no decision passed the checks
    why: str  # skipped or dropped: why; asked: when it reaches the owner; else ""
    looked: bool = True  # False: code skipped it, no model call
    question_id: int | None = None


def run_due(
    app: Connection, energy: Connection, cfg: Config, *, site_id: int, now: datetime,
    llm: Llm | None = None,
) -> tuple[list[Event], list[Outcome]]:  # fmt: skip
    """Run each job whose time has come. Passes missed while the app was down run once.

    llm: None when there is no API key. The housemate is then skipped and signals wait.
    """
    events: list[Event] = []
    looked: list[Outcome] = []
    if _due(app, site_id, "watch", WATCH_EVERY, now):
        events += watch_pass(app, energy, cfg, site_id=site_id, now=now)
        if llm:
            try:
                looked = look_pass(app, energy, cfg, llm, site_id=site_id, now=now)
            except Exception as err:  # a bug in the look step: log it, run the other jobs
                store.add_audit(app, site_id=site_id, at=now, actor="code",
                                action="look_pass_failed", ref="job:watch",
                                detail={"error": f"{type(err).__name__}: {err}"})  # fmt: skip
        store.set_last_run(app, site_id=site_id, job="watch", at=now)
    if _due(app, site_id, "timers", TIMERS_EVERY, now):
        events += timer_check(app, cfg, site_id=site_id, now=now)
        store.set_last_run(app, site_id=site_id, job="timers", at=now)
    if _due(app, site_id, "learn", LEARN_EVERY, now):
        learn_pass(app, energy, cfg, site_id=site_id, now=now)
        events += change_check(app, energy, cfg, site_id=site_id, now=now)
        store.set_last_run(app, site_id=site_id, job="learn", at=now)
    return events, looked


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
        found += watcher.automation_events(mac, store.load_timers(app, mac=mac), readings, usual,
                                           now=now, tz=cfg.tz, policy=policy)  # fmt: skip
        if house.runs_always(store.load_card(app, mac=mac)):
            continue  # a fridge's or router's cycles are normal (FM-04)
        runs = watcher.unusual_runs(readings, usual, tz=cfg.tz, policy=policy)
        found += [watcher.run_event(mac, run) for run in runs]
    return [
        e for e in found
        if store.add_event(app, site_id=site_id, event=e, dedupe=watcher.dedupe_key(e, tz=cfg.tz))
    ]  # fmt: skip


def look_pass(
    app: Connection, energy: Connection, cfg: Config, llm: Llm, *, site_id: int, now: datetime
) -> list[Outcome]:
    """The housemate looks at each new signal once, oldest first (FR-15 v2).

    Code skips what needs no look first: a habit covers it, or a timer's drift is not a
    pattern yet. At a budget cap, or with the model out of reach, the pass stops and the
    rest wait. Any other error counts a try for that event only; after MAX_TRIES it is
    parked (FR-37).
    """
    store.expire_questions(app, site_id=site_id, before=now - QUESTION_LIFE)
    plugs = {mac: name for name, mac in store.plug_macs(app, site_id=site_id).items()}
    waiting = sorted((pair for kind in LOOKED for pair in store.new_events(app, site_id=site_id,
                                                                          kind=kind)),
                     key=lambda pair: pair[0])  # fmt: skip
    found: list[Outcome] = []
    for eid, e in waiting:
        try:
            out = _look(app, energy, cfg, llm, eid, e, plugs[e.plug_mac or ""], site_id, now)
        except (OverBudget, Unreachable):
            break
        except Exception as err:  # one bad event must not stop the others
            _failed(app, site_id, eid, err, now)
            continue
        store.event_done(app, event_id=eid, status="done" if out.looked else "skipped")
        found.append(out)
    return found


def _look(
    app: Connection, energy: Connection, cfg: Config, llm: Llm, eid: int, e: Event, plug: str,
    site_id: int, now: datetime,
) -> Outcome:  # fmt: skip
    mac, zone, ref = e.plug_mac or "", ZoneInfo(cfg.tz), f"event:{eid}"
    device = config.device(cfg, plug, store.learned(app, mac=mac))
    skip, signal = _skip(app, site_id, mac, e, device.policy, zone)
    if skip:
        store.add_audit(app, site_id=site_id, at=now, actor="code", action="skipped", ref=ref,
                        detail={"why": skip})  # fmt: skip
        return Outcome(eid, device.name, None, skip, looked=False)
    signal = words.event_words(e, {mac: device.name}, tz=cfg.tz) + signal
    tools = agent_tools.Tools(app, energy, cfg, site_id, now)
    look = agent.look(signal, plug, tools=tools, llm=llm, run_id=ref)
    if (d := look.decision) is None:
        why = f"no decision: {look.blocked}"
        store.add_audit(app, site_id=site_id, at=now, actor="agent", action="dropped", ref=ref,
                        detail={"why": why, "tools": ", ".join(look.tools)})  # fmt: skip
        return Outcome(eid, device.name, None, why)
    why, qid = "", None
    if d.action in ("ask", "propose"):
        local = now.astimezone(zone)
        due = owner.send_at(
            local, _watts(energy, plug, e, now), night_wake_w=device.policy.night_wake_w
        )
        if held := _held(app, site_id, mac, device.name, due, cfg.policy):
            d, why = Decision("note", d.text), held  # nothing is lost: it is in the summary
        else:
            qid = store.add_question(app, site_id=site_id, event_id=eid, mac=mac, decision=d,
                                     send_at=due, now=now)  # fmt: skip
            why = "reaches you now" if due == local else f"reaches you at {due:%a %H:%M}"
    detail = {"text": d.text, "why": why, "tools": ", ".join(look.tools)}
    store.add_audit(app, site_id=site_id, at=now, actor="agent", action=d.action, ref=ref,
                    detail=detail)  # fmt: skip
    return Outcome(eid, device.name, d, why, question_id=qid)


def _skip(
    app: Connection, site_id: int, mac: str, e: Event, policy: Policy, zone: ZoneInfo
) -> tuple[str, str]:  # fmt: skip
    """(why code skips this signal, or "", and what to add to its words for the agent)."""
    if e.kind in ("left_on", "unusual_use"):
        local = e.start.astimezone(zone)
        if h := next((h for _, h in store.habits(app, mac=mac) if house.covers(h, local)), None):
            return f"habit: {h.text}", ""
    if e.kind != "automation_drift":
        return "", ""
    recent = store.recent_events(app, site_id=site_id, mac=mac, kind=e.kind,
                                 since=e.start - PATTERN)  # fmt: skip
    same = [
        (s, x) for _, s, x in recent if x.facts["slot"] == e.facts["slot"] and x.start <= e.start
    ]
    days = len({x.start.astimezone(zone).date() for _, x in same})
    if any(s == "done" for s, _ in same):
        return f"grouped: timer {e.facts['slot']} was looked at in the last 7 days", ""
    if days < policy.drift_days:
        return f"waiting: timer {e.facts['slot']} drifted on {days} of the last 7 days", ""
    return "", f". The same timer did this on {days} of the last 7 days"


def _watts(energy: Connection, plug: str, e: Event, now: datetime) -> float:
    """What the device draws now: its last reading in the last 5 minutes, else the signal's."""
    recent = store.readings(energy, plug=plug, start=now - agent_tools.NOW_WINDOW, end=now)
    return recent[-1][1] if recent else float(e.facts.get("peak_w", 0.0))


def _held(
    app: Connection, site_id: int, mac: str, device: str, due: datetime, site: Policy
) -> str:  # fmt: skip
    """Why a question must wait for the summary instead: the day's limits. "" if none."""
    day = due.replace(hour=0, minute=0, second=0, microsecond=0)
    bounds = {"site_id": site_id, "start": day, "end": day + timedelta(days=1)}
    if store.count_questions(app, mac=mac, **bounds):
        return f"held: {device} has a question that day already"
    if (n := store.count_questions(app, **bounds)) >= site.questions_a_day:
        return f"held: {n} questions that day already"
    return ""


def answer(app: Connection, cfg: Config, *, site_id: int, question_id: int, choice: int,
           now: datetime) -> str:  # fmt: skip
    """The owner's tap on a question: do what the choice does, and say what was done.

    Shadow mode (Phase 3): a switch off or a timer is recorded, never written.
    """
    q = store.get_question(app, question_id=question_id)
    if q is None or q.status != "open":
        raise LookupError(f"No open question {question_id}.")
    if not 1 <= choice <= len(q.decision.choices):
        raise LookupError(f"Question {question_id} has choices 1 to {len(q.decision.choices)}.")
    c, ref = q.decision.choices[choice - 1], f"question:{question_id}"
    store.answer_question(app, question_id=question_id, answer=choice, now=now)
    store.add_audit(app, site_id=site_id, at=now, actor="owner", action="answer", ref=ref,
                    detail={"choice": c.label, "does": c.does})  # fmt: skip
    plug = next(p for p, m in store.plug_macs(app, site_id=site_id).items() if m == q.plug_mac)
    name, zone = config.device(cfg, plug).name, ZoneInfo(cfg.tz)
    if c.does == "save_habit":
        e = store.get_event(app, event_id=q.event_id)
        h = owner.habit(name, (e.start if e else now).astimezone(zone))
        store.add_habit(app, site_id=site_id, mac=q.plug_mac, habit=h, now=now)
        return f"Saved: {h.text}."
    if c.does == "remind_later":
        store.ask_again(app, question_id=question_id, at=now + REMIND)
        return f"I'll ask again at {(now + REMIND).astimezone(zone):%H:%M}."
    if c.does in ("switch_off_now", "set_timer"):
        store.add_audit(app, site_id=site_id, at=now, actor="code", action="shadow_action",
                        ref=ref, detail={"does": c.does, "off": c.off or "", "on": c.on or "",
                                         "days": c.days or ""})  # fmt: skip
        return f"Recorded: {c.label}. Shadow mode: nothing was sent to the plug."
    return "Noted."


def _failed(app: Connection, site_id: int, eid: int, err: Exception, now: datetime) -> None:
    """Record a failed try; park the event at MAX_TRIES so it stops coming back."""
    ref, error = f"event:{eid}", {"error": f"{type(err).__name__}: {err}"}
    store.add_audit(app, site_id=site_id, at=now, actor="code", action="look_failed",
                    ref=ref, detail=error)  # fmt: skip
    if store.event_failed(app, event_id=eid, limit=MAX_TRIES):
        store.add_audit(app, site_id=site_id, at=now, actor="code", action="event_parked",
                        ref=ref, detail={"tries": str(MAX_TRIES)})  # fmt: skip


def change_check(
    app: Connection, energy: Connection, cfg: Config, *, site_id: int, now: datetime
) -> list[Event]:
    """Daily: has a new device appeared on any plug? At most once per history_days a plug."""
    found = []
    for name, mac in store.plug_macs(app, site_id=site_id).items():
        since = now - timedelta(days=config.device(cfg, name).policy.history_days)
        last = store.last_event_start(app, site_id=site_id, kind="change_point", plug_mac=mac)
        if last is not None and last >= since:
            continue  # asked already; the owner's answer or the new levels settle it
        readings = store.readings(energy, plug=name, start=since, end=now)
        recent = [r for r in readings if r[0] >= now - RECENT]
        before = [
            [r for r in readings if now - RECENT * (k + 1) <= r[0] < now - RECENT * k]
            for k in range(1, int((now - since) / RECENT))
        ]  # the earlier windows, each as long as `recent`
        if (e := watcher.change_point(mac, before, recent, now=now)) and store.add_event(
            app, site_id=site_id, event=e, dedupe=watcher.dedupe_key(e, tz=cfg.tz)
        ):
            found.append(e)
    return found


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
