"""The only code that changes a plug (FR-20, FR-32, NFR-12).

A change comes only from the owner's tap: "switch off now" (ADR-008), a timer pair, or one
timer moved (a plan item's Do it, 5b5). A timer change can be put back for a day.
Before anything is sent, the write-time checks, cheapest first:
1. access none: never changed.
2. must run: never switched off, by a tap or a timer.
3. a timer: times as HH:MM, known days, no hard rule broken (checked again here).
4. access advice: the choice is recorded, nothing is sent.
5. shadow mode on, or no login set in .env: recorded, nothing is sent.
6. at most WRITES_A_DAY writes a plug a day, failed ones too.
An undo (switching back on what a tap switched off, or putting timers back) skips 4 and 5:
it only puts things back, so it works even after the owner turns shadow mode on again to
stop everything.
Then the write, a separate read-back, and one audit row. A failure is audited and shown as
a note in the inbox. A timer change is all or nothing: on a failure the old timers go back.
"""

import json
import re
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from sqlite3 import Connection
from zoneinfo import ZoneInfo

from agentc import config, house, store
from agentc.checks import breaks
from agentc.config import Config, Device
from agentc.devices.base import Plug, PlugError, Timer, day_bits, days_words
from agentc.devices.tasmota import TasmotaPlug

WRITES_A_DAY = 4  # a plug switching more than this in a day is a bug, not a plan
UNDO_FOR = timedelta(hours=24)  # a switch off can be undone from the inbox for this long
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

Connect = Callable[[str], Plug]


def switch(
    app: Connection, cfg: Config, *, site_id: int, plug: str, on: bool, why: str, now: datetime,
    connect: Connect = TasmotaPlug,
) -> str:  # fmt: skip
    """Switch one plug on or off for the owner. why: what asked for it, e.g. "question:7".
    Returns what happened, in words for the owner."""
    name = config.device(cfg, plug).name
    act = f"switch {name} {'on' if on else 'off'}"
    detail = {"does": "switch_on" if on else "switch_off", "why": why}
    undo = on and why.startswith("undo:")
    if stop := _checks(app, cfg, site_id, plug, act, detail, now, off=not on, undo=undo):
        return stop
    try:
        p = connect(cfg.plugs[plug])
        p.switch(on)
        state = p.power()  # read back: the plug's answer to the command is not proof
    except PlugError as err:
        maybe = "" if on else " It may be off: Switch back on is in the inbox."
        return _failed(app, site_id, plug, detail, now, f"Could not {act}: the plug did not"
                       f" answer ({err}).{maybe}")  # fmt: skip
    if state != on:
        return _failed(app, site_id, plug, detail, now, f"Could not {act}: the plug still"
                       f" says {'on' if state else 'off'}.")  # fmt: skip
    _wrote(app, site_id, plug, detail, now)
    return f"Done: {name} is {'on' if on else 'off'}."


def set_timer(
    app: Connection, cfg: Config, *, site_id: int, plug: str, off: str, on: str, days: str,
    why: str, now: datetime, connect: Connect = TasmotaPlug,
) -> str:  # fmt: skip
    """Add a timer pair for the owner: off at `off`, on again at `on`, on `days` (a DAY_SETS
    name, or 7 chars Sunday first). Written in two free slots the device allows; the owner's
    own slots are never touched. Returns what happened, in words for the owner."""
    device = config.device(cfg, plug)
    when = f"off at {off}, on at {on}, {days_words(day_bits(days) or '0000000')}"
    act = f"set {device.name}'s timer ({when})"
    detail = {"does": "set_timer", "why": why, "off": off, "on": on, "days": days}
    if stop := _checks(app, cfg, site_id, plug, act, detail, now, off=True, timer=(off, on, days)):
        return stop
    try:
        p = connect(cfg.plugs[plug])
        found = p.timers()
    except PlugError as err:
        return _failed(app, site_id, plug, detail, now, f"Could not {act}: the plug did not"
                       f" answer ({err}).")  # fmt: skip
    free = [s for s in device.slots if not found.timers[s - 1].enable]
    if not found.enabled or len(free) < 2:
        why_not = ("its Timers switch is off, so no timer would fire. Turn it on in the plug's"
                   " page first" if not found.enabled else
                   f"it has no 2 free timer slots of {_slots(device)}")  # fmt: skip
        return _refused(app, site_id, plug, detail, now, f"Not sent: {device.name}: {why_not}.")
    bits = day_bits(days) or ""  # checked in _rules
    pair = (
        Timer(free[0], True, 0, on, 0, bits, True, 1, 1),  # the "on" timer first: a half
        Timer(free[1], True, 0, off, 0, bits, True, 1, 0),
    )  # done change never strands it off
    old = {t.slot: t for t in found.timers}
    done: list[int] = []
    try:
        for t in pair:
            done.append(t.slot)
            p.set_timer(t)
            got = p.timers().timers[t.slot - 1]
            if not _same(got, t):
                raise PlugError(f"timer {t.slot} reads back as {got.time}, enable {got.enable}")
    except PlugError as err:
        back = _put_back(p, [old[s] for s in done])
        return _failed(app, site_id, plug, detail, now, f"Could not {act}: {err}. {back}")
    mac = store.plug_mac(app, name=plug) or ""
    store.save_timers(app, site_id=site_id, mac=mac, timers=[*_merge(found.timers, pair)], now=now)
    c = store.load_card(app, mac=mac)
    reason = f"set by AgentC after your tap: {when}"
    why_slots = {**c.why, **{t.slot: reason for t in pair}}
    store.save_card(app, site_id=site_id, mac=mac, card=replace(c, why=why_slots), now=now)
    _wrote(app, site_id, plug, {**detail, "slots": f"{pair[0].slot},{pair[1].slot}",
                                "old": _json([old[t.slot] for t in pair]),
                                "new": _json(list(pair))}, now)  # fmt: skip
    return f"Done: {device.name} now switches {when} (timers {pair[0].slot} and {pair[1].slot})."


def move_timer(
    app: Connection, cfg: Config, *, site_id: int, plug: str, slot: int, time: str, why: str,
    now: datetime, was: str | None = None, connect: Connect = TasmotaPlug,
) -> str:  # fmt: skip
    """Move one clock timer to a new time, same days and action: any slot, the owner's own
    too. It must still be as the plan saw it: at `was` on the plug, and as stored; else
    nothing is sent, so a change by hand is never written over. Returns what happened, in
    words for the owner."""
    device = config.device(cfg, plug)
    mac = store.plug_mac(app, name=plug) or ""
    seen = {t.slot: t for t in store.load_timers(app, mac=mac)}.get(slot)
    act = f"move {device.name}'s timer {slot} to {time}"
    detail = {"does": "move_timer", "why": why, "slot": str(slot), "time": time}
    if seen is None or not seen.enable or seen.mode != 0 or seen.action not in (0, 1):
        return _refused(app, site_id, plug, detail, now, f"Not sent: {device.name} has no clock"
                        f" timer {slot} to move.")  # fmt: skip
    if not HHMM.match(time):
        return _refused(app, site_id, plug, detail, now, f"Not sent: {device.name}'s timer"
                        " needs an HH:MM time.")  # fmt: skip
    off_at = (time, time, "every_day") if seen.action == 0 else None  # hard rules: off times
    if stop := _checks(app, cfg, site_id, plug, act, detail, now, off=True, timer=off_at):
        return stop
    try:
        p = connect(cfg.plugs[plug])
        found = p.timers()
    except PlugError as err:
        return _failed(app, site_id, plug, detail, now, f"Could not {act}: the plug did not"
                       f" answer ({err}).")  # fmt: skip
    old = found.timers[slot - 1]
    if not _same(old, seen) or (was is not None and old.time != was):
        text = (f"Not sent: {device.name}'s timer {slot} changed since the plan was made."
                " Check its timers first.")  # fmt: skip
        return _refused(app, site_id, plug, detail, now, text)
    new = replace(old, time=time)
    try:
        p.set_timer(new)
        got = p.timers().timers[slot - 1]
        if not _same(got, new):
            raise PlugError(f"timer {slot} reads back as {got.time}, enable {got.enable}")
    except PlugError as err:
        return _failed(app, site_id, plug, detail, now, f"Could not {act}: {err}. "
                       f"{_put_back(p, [old])}")  # fmt: skip
    store.save_timers(app, site_id=site_id, mac=mac, timers=_merge(found.timers, (new,)), now=now)
    c = store.load_card(app, mac=mac)
    reason = f"moved to {time} by AgentC after your tap (was {old.time})"
    store.save_card(app, site_id=site_id, mac=mac, card=replace(c, why={**c.why, slot: reason}),
                    now=now)  # fmt: skip
    _wrote(app, site_id, plug, {**detail, "old": _json([old]), "new": _json([new])}, now)
    return f"Done: {device.name}'s timer {slot} now runs at {time} (was {old.time})."


def put_back(
    app: Connection, cfg: Config, *, site_id: int, plug: str, write_id: int, old: str,
    now: datetime, new: str | None = None, connect: Connect = TasmotaPlug,
) -> str:  # fmt: skip
    """Undo a timer change: write the timers it replaced (old, as JSON) back, and read them
    back. Only if the plug still has what AgentC wrote (new): a change by hand since is
    never written over. Returns what happened, in words for the owner."""
    device = config.device(cfg, plug)
    timers = [Timer(**t) for t in json.loads(old)]
    wrote = [Timer(**t) for t in json.loads(new)] if new else []
    act = f"put {device.name}'s timers back"
    detail = {"does": "put_back", "why": f"undo:{write_id}"}
    if stop := _checks(app, cfg, site_id, plug, act, detail, now, off=False, undo=True):
        return stop
    try:
        p = connect(cfg.plugs[plug])
        now_on = p.timers().timers
        if not all(_same(now_on[t.slot - 1], t) for t in wrote):
            return _refused(
                app,
                site_id,
                plug,
                detail,
                now,
                f"Not sent: {device.name}'s timers"
                " were changed on the plug since, so nothing was put back.",
            )
        for t in timers:
            p.set_timer(t)
        found = p.timers()
    except PlugError as err:
        return _failed(app, site_id, plug, detail, now, f"Could not {act}: the plug did not"
                       f" answer ({err}).")  # fmt: skip
    if not all(_same(found.timers[t.slot - 1], t) for t in timers):
        return _failed(app, site_id, plug, detail, now, f"Could not {act}: they read back"
                       " different. Check the plug's Timers page.")  # fmt: skip
    mac = store.plug_mac(app, name=plug) or ""
    store.save_timers(app, site_id=site_id, mac=mac, timers=found.timers, now=now)
    c = store.load_card(app, mac=mac)
    why = {s: w for s, w in c.why.items() if s not in {t.slot for t in timers}}
    store.save_card(app, site_id=site_id, mac=mac, card=replace(c, why=why), now=now)
    _wrote(app, site_id, plug, detail, now)
    return f"Done: {device.name}'s timers are back as they were."


def _json(timers: list[Timer]) -> str:
    return json.dumps([asdict(t) for t in timers])


def _checks(
    app: Connection, cfg: Config, site_id: int, plug: str, act: str, detail: dict[str, str],
    now: datetime, *, off: bool, timer: tuple[str, str, str] | None = None, undo: bool = False,
) -> str:  # fmt: skip
    """The write-time checks. Returns why nothing is sent, or "" to go ahead."""
    device = config.device(cfg, plug)
    mac = store.plug_mac(app, name=plug)
    card = store.load_card(app, mac=mac) if mac else house.Card()
    refused = _rules(device, card, off=off, timer=timer)
    if refused:
        return _refused(app, site_id, plug, detail, now, refused)
    logged_in = any(config.logins())  # with no login, anyone on the network could tap
    if not undo and (device.access != "write" or not logged_in
                     or store.shadow(app, site_id=site_id)):  # fmt: skip
        store.add_audit(app, site_id=site_id, at=now, actor="code", action="shadow_action",
                        ref=detail["why"], detail={**detail, "plug": plug})  # fmt: skip
        if device.access != "write":
            return (f"Recorded: {act}. {device.name} is set to advice, so nothing was sent. To"
                    " let me change it, set its access to write in config.toml.")  # fmt: skip
        if not logged_in:
            return (f"Recorded: {act}. No login is set in .env, so nothing was sent to the"
                    " plug.")  # fmt: skip
        return f"Recorded: {act}. Shadow mode: nothing was sent to the plug."
    midnight = now.astimezone(ZoneInfo(cfg.tz)).replace(hour=0, minute=0, second=0, microsecond=0)
    if store.count_writes(app, site_id=site_id, plug=plug, since=midnight) >= WRITES_A_DAY:
        text = f"Not sent: {device.name} was changed {WRITES_A_DAY} times today already."
        return _refused(app, site_id, plug, detail, now, text)
    return ""


def _rules(device: Device, card: house.Card, *, off: bool,
           timer: tuple[str, str, str] | None) -> str:  # fmt: skip
    name = device.name
    if device.access == "none":
        return f"Not sent: {name} is never changed (its access is none)."
    if off and house.runs_always(card):
        return f"Not sent: {name} must always run, so it is never switched off."
    if timer is None:
        return ""
    t_off, t_on, days = timer
    if not (HHMM.match(t_off) and HHMM.match(t_on)) or not day_bits(days):
        return f"Not sent: {name}'s timer needs HH:MM times and known days."
    if hard := [r for r in device.rules if r.hard and breaks(t_off, r)]:
        return f"Not sent: {hard[0].text}."
    return ""


def _same(got: Timer, want: Timer) -> bool:
    """A timer as the plug reads it back. Tasmota may show days as letters ("-MTWTF-")."""
    return replace(got, days=_bits(got.days)) == replace(want, days=_bits(want.days))


def _bits(days: str) -> str:
    return "".join("0" if d in "0-" else "1" for d in days)


def _put_back(p: Plug, old: list[Timer]) -> str:
    """Write the old timers back, the last written first, and read them back. Says how it
    went."""
    try:
        for t in reversed(old):
            p.set_timer(t)
        now = p.timers().timers
        if all(_same(now[t.slot - 1], t) for t in old):
            return "The old timers were put back."
    except PlugError:
        pass
    return "Putting the old timers back failed too: check the plug's Timers page."


def _merge(timers: list[Timer], new: tuple[Timer, ...]) -> list[Timer]:
    by_slot = {t.slot: t for t in new}
    return [by_slot.get(t.slot, t) for t in timers]


def _slots(device: Device) -> str:
    return f"{device.slots[0]} to {device.slots[-1]}" if device.slots else "none"


def _wrote(app: Connection, site_id: int, plug: str, detail: dict[str, str],
           now: datetime) -> None:  # fmt: skip
    store.add_audit(app, site_id=site_id, at=now, actor="code", action="write",
                    ref=f"plug:{plug}", detail=detail)  # fmt: skip


def _refused(app: Connection, site_id: int, plug: str, detail: dict[str, str], now: datetime,
             text: str) -> str:  # fmt: skip
    store.add_audit(app, site_id=site_id, at=now, actor="code", action="write_refused",
                    ref=f"plug:{plug}", detail={**detail, "reason": text})  # fmt: skip
    return text


def _failed(app: Connection, site_id: int, plug: str, detail: dict[str, str], now: datetime,
            text: str) -> str:  # fmt: skip
    ref = f"plug:{plug}"
    store.add_audit(app, site_id=site_id, at=now, actor="code", action="write_failed", ref=ref,
                    detail={**detail, "error": text})  # fmt: skip
    store.add_audit(app, site_id=site_id, at=now, actor="code", action="note", ref=ref,
                    detail={"text": text})  # fmt: skip
    return text
