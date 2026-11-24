"""Jobs: the work that runs on a timetable. The watch pass runs every 5 minutes.

A job that breaks never stops the jobs after it (FR-37): an AI outage must not stop the
timer check, learning or the heartbeat.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from sqlite3 import Connection
from zoneinfo import ZoneInfo

from agentc import (
    agent,
    agent_tools,
    calc,
    config,
    house,
    learn,
    notify,
    owner,
    readings,
    store,
    timers,
    watcher,
    words,
)
from agentc.config import Config
from agentc.llm import Llm, OverBudget, Unreachable
from agentc.notify import Message, NotSent, Publish
from agentc.owner import Decision, Outcome
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
SUMMARY_AT = time(21, 0)  # the day's notes, in one quiet message
NOTES_SINCE = timedelta(days=1)  # the summary and the inbox show notes this recent
PROFILE_DAYS = timedelta(days=28)  # the usual week is built from this many days


@dataclass(frozen=True)
class Sender:
    """How messages reach the owner: ntfy, the hub's address and the button secret."""

    publish: Publish
    base_url: str  # the inbox, as the phone reaches it: the buttons' address
    secret: str


def run_due(
    app: Connection, energy: Connection, cfg: Config, *, site_id: int, now: datetime,
    llm: Llm | None = None, sender: Sender | None = None,
) -> tuple[list[Event], list[Outcome], list[Message]]:  # fmt: skip
    """Run each job whose time has come. Passes missed while the app was down run once.

    llm: None when there is no API key. The housemate is then skipped and signals wait.
    sender: None when no [owner] is set. Questions then wait in the inbox.
    """
    events: list[Event] = []
    looked: list[Outcome] = []
    sent: list[Message] = []
    if _due(app, site_id, "watch", WATCH_EVERY, now):
        store.expire_questions(app, site_id=site_id, before=now - QUESTION_LIFE)
        events += _safely(app, site_id, now, "watch", lambda: watch_pass(
            app, energy, cfg, site_id=site_id, now=now)) or []  # fmt: skip
        if llm:
            looked = _safely(app, site_id, now, "look_pass", lambda: look_pass(
                app, energy, cfg, llm, site_id=site_id, now=now)) or []  # fmt: skip
        if sender:  # a broken send leaves every question in the inbox
            sent = _safely(app, site_id, now, "send_pass", lambda: send_pass(
                app, energy, cfg, sender, site_id=site_id, now=now)) or []  # fmt: skip
        store.set_last_run(app, site_id=site_id, job="watch", at=now)
    if sender and _summary_due(app, site_id, now, cfg.tz):
        try:
            if m := summary_pass(app, sender, site_id=site_id, now=now):
                sent.append(m)
            store.set_last_run(app, site_id=site_id, job="summary", at=now)
        except NotSent as err:  # try again on the next pass
            _broken(app, site_id, now, "not_sent", err)
    if _due(app, site_id, "timers", TIMERS_EVERY, now):
        events += _safely(app, site_id, now, "timers", lambda: timer_check(
            app, cfg, site_id=site_id, now=now)) or []  # fmt: skip
        store.set_last_run(app, site_id=site_id, job="timers", at=now)
    if _due(app, site_id, "learn", LEARN_EVERY, now):
        _safely(app, site_id, now, "learn", lambda: learn_pass(
            app, energy, cfg, site_id=site_id, now=now))  # fmt: skip
        events += _safely(app, site_id, now, "change", lambda: change_check(
            app, energy, cfg, site_id=site_id, now=now)) or []  # fmt: skip
        store.set_last_run(app, site_id=site_id, job="learn", at=now)
    return events, looked, sent


def _safely[T](app: Connection, site_id: int, now: datetime, job: str, run: Callable[[], T]
               ) -> T | None:  # fmt: skip
    """Run one job. A bug in it is logged, and the other jobs still run (FR-37: a broken job
    never stops the others). It runs again at its next time."""
    try:
        return run()
    except Exception as err:
        _broken(app, site_id, now, f"{job}_failed", err)
        return None


def _broken(app: Connection, site_id: int, now: datetime, action: str, err: Exception) -> None:
    store.add_audit(app, site_id=site_id, at=now, actor="code", action=action, ref="job:watch",
                    detail={"error": f"{type(err).__name__}: {err}"})  # fmt: skip


def _summary_due(app: Connection, site_id: int, now: datetime, tz: str) -> bool:
    """After 21:00, once a day."""
    local, last = now.astimezone(ZoneInfo(tz)), store.last_run(app, site_id=site_id, job="summary")
    sent_today = last is not None and last.astimezone(ZoneInfo(tz)).date() == local.date()
    return local.time() >= SUMMARY_AT and not sent_today


def _due(app: Connection, site_id: int, job: str, every: timedelta, now: datetime) -> bool:
    last = store.last_run(app, site_id=site_id, job=job)
    return last is None or now - last >= every


def watch_pass(
    app: Connection, energy: Connection, cfg: Config, *, site_id: int, now: datetime
) -> list[Event]:
    """Check every known plug with its own policy; save new events and return them."""
    macs = store.plug_macs(app, site_id=site_id)
    rounds = readings.last_rounds(energy, n=watcher.PASS_ROUNDS, end=now)
    outages = watcher.offline_events(rounds, macs)
    found = [e for e in outages if not _reported(app, energy, site_id, e)]
    if stopped := watcher.logger_stopped(readings.last_round(energy, end=now), now=now):
        found.append(stopped)  # its dedupe key is its start, so one event per stop
    for name, mac in macs.items():
        usual = watcher.usual_week(store.load_profile(app, mac=mac))  # no profile: never unusual
        rows = readings.power(energy, plug=name, start=now - LOOKBACK, end=now)
        policy = config.device(cfg, name, store.learned(app, mac=mac)).policy
        found += watcher.automation_events(mac, store.load_timers(app, mac=mac), rows, usual,
                                           now=now, tz=cfg.tz, policy=policy)  # fmt: skip
        if house.runs_always(store.load_card(app, mac=mac)):
            continue  # a fridge's or router's cycles are normal (FM-04)
        runs = watcher.unusual_runs(rows, usual, tz=cfg.tz, policy=policy)
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
            if qid is None:  # another run asked about it first
                return Outcome(eid, device.name, None, "handled by another run")
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
    w = _now_w(energy, plug, now)
    return float(e.facts.get("peak_w", 0.0)) if w is None else w


def _now_w(energy: Connection, plug: str, now: datetime) -> float | None:
    """Its last reading in the last 5 minutes; None if there is none."""
    recent = readings.power(energy, plug=plug, start=now - agent_tools.NOW_WINDOW, end=now)
    return recent[-1][1] if recent else None


def send_pass(
    app: Connection, energy: Connection, cfg: Config, sender: Sender, *, site_id: int,
    now: datetime,
) -> list[Message]:  # fmt: skip
    """Send the questions that are due: each day question on its own, the night's together.

    A night question whose device is off by morning is dropped: nothing to ask. If
    ntfy fails, the rest stay unsent and go on the next pass; the inbox has them all.
    """
    plugs = {mac: name for name, mac in store.plug_macs(app, site_id=site_id).items()}
    out: list[tuple[Message, list[int]]] = []
    nights: list[tuple[owner.Question, str, float | None]] = []
    for q in store.due_questions(app, site_id=site_id, now=now):
        plug = plugs[q.plug_mac]
        device = config.device(cfg, plug, store.learned(app, mac=q.plug_mac))
        if not owner.held_for_morning(q, tz=cfg.tz):
            out.append((notify.single(q, device.name, hub=sender.base_url, secret=sender.secret),
                        [q.id]))  # fmt: skip
            continue
        w = _now_w(energy, plug, now)
        if w is not None and w < device.policy.on_w:
            store.close_question(app, question_id=q.id)
            store.add_audit(app, site_id=site_id, at=now, actor="code", action="off_by_morning",
                            ref=f"question:{q.id}", detail={"watts": f"{w:g}"})  # fmt: skip
            continue
        nights.append((q, device.name, w))
    if len(nights) == 1:
        q, name, _ = nights[0]
        out.append((notify.single(q, name, hub=sender.base_url, secret=sender.secret), [q.id]))
    elif nights:
        day = now.astimezone(ZoneInfo(cfg.tz)).date()
        m = notify.night(nights, day=day, hub=sender.base_url, secret=sender.secret)
        out.append((m, [q.id for q, _, _ in nights]))
    sent = []
    for m, ids in out:
        try:
            sender.publish(m)
        except NotSent as err:
            _broken(app, site_id, now, "not_sent", err)
            break
        store.mark_sent(app, question_ids=ids, now=now)
        sent.append(m)
    return sent


def summary_pass(app: Connection, sender: Sender, *, site_id: int, now: datetime
                 ) -> Message | None:  # fmt: skip
    """The last day's notes in one quiet message. None if there were none."""
    notes = [d["text"] for d in store.audit_since(app, site_id=site_id, action="note",
                                                   since=now - NOTES_SINCE)]  # fmt: skip
    if not notes:
        return None
    m = notify.summary(notes)
    sender.publish(m)
    return m


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
    if not store.answer_question(app, question_id=question_id, answer=choice, now=now):
        raise LookupError(f"No open question {question_id}.")  # a second tap, at the same time
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


def answer_night(app: Connection, cfg: Config, *, site_id: int, night: date, does: str,
                 now: datetime) -> str:  # fmt: skip
    """One tap for the night's questions: each open one that offers `does` gets it."""
    zone, done = ZoneInfo(cfg.tz), []
    for q in store.questions(app, site_id=site_id, status="open"):
        if not (owner.held_for_morning(q, tz=cfg.tz)
                and q.send_at.astimezone(zone).date() == night):  # fmt: skip
            continue
        n = next((n for n, c in enumerate(q.decision.choices, 1) if c.does == does), None)
        if n is not None:
            done.append(answer(app, cfg, site_id=site_id, question_id=q.id, choice=n, now=now))
    if not done:
        raise LookupError(f"No open questions from the night before {night}.")
    return "\n".join(done)


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
        rows = readings.power(energy, plug=name, start=since, end=now)
        recent = [r for r in rows if r[0] >= now - RECENT]
        before = [
            [r for r in rows if now - RECENT * (k + 1) <= r[0] < now - RECENT * k]
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
    """Each plug's usual week (FR-02) and its levels, saved. plug name -> levels.

    The usual week comes from the last PROFILE_DAYS, rebuilt daily so it follows new
    habits. A plug with under min_days of readings keeps what it had. One with a single
    level, or none, learns nothing, so it keeps the defaults.
    """
    found: dict[str, learn.Levels | None] = {}
    for name, mac in store.plug_macs(app, site_id=site_id).items():
        policy = config.device(cfg, name).policy
        week = readings.power(energy, plug=name, start=now - PROFILE_DAYS, end=now)
        if profile := calc.build_profile(week, tz=cfg.tz, policy=policy):
            store.save_profile(app, site_id=site_id, mac=mac, profile=profile)
        rows = readings.power(energy, plug=name, end=now,
                                  start=now - timedelta(days=policy.history_days))  # fmt: skip
        if not rows or (now - rows[0][0]).days < policy.min_days:
            continue
        found[name] = lv = learn.levels([w for _, w in rows])
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
    return not readings.answered(energy, plug=str(plug) if plug else None, start=last, end=e.start)


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
