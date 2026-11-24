"""The only code that changes a plug (FR-20, FR-32, NFR-12).

A change comes only from the owner's tap (ADR-008: one "switch off now"; timers in 4c).
Before anything is sent, the write-time checks, cheapest first:
1. access none: never changed.
2. must run: never switched off.
3. access advice: the choice is recorded, nothing is sent.
4. shadow mode on: recorded, nothing is sent.
5. at most WRITES_A_DAY writes a plug a day, failed ones too.
Then one command, a separate read-back, and one audit row. A failure is audited and shown
as a note in the inbox; nothing else is tried, so the plug stays as it was.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from sqlite3 import Connection
from zoneinfo import ZoneInfo

from agentc import config, house, store
from agentc.config import Config
from agentc.devices.base import Plug, PlugError
from agentc.devices.tasmota import TasmotaPlug

WRITES_A_DAY = 4  # a plug switching more than this in a day is a bug, not a plan
UNDO_FOR = timedelta(hours=24)  # a switch off can be undone from the inbox for this long


def switch(
    app: Connection, cfg: Config, *, site_id: int, plug: str, on: bool, why: str, now: datetime,
    connect: Callable[[str], Plug] = TasmotaPlug,
) -> str:  # fmt: skip
    """Switch one plug on or off for the owner. why: what asked for it, e.g. "question:7".
    Returns what happened, in words for the owner."""
    device = config.device(cfg, plug)
    name, does, ref = device.name, "switch_on" if on else "switch_off", f"plug:{plug}"
    act = f"switch {name} {'on' if on else 'off'}"
    detail = {"does": does, "why": why}
    mac = store.plug_mac(app, name=plug)
    card = store.load_card(app, mac=mac) if mac else house.Card()
    if device.access == "none":
        refused = f"Not sent: {name} is never changed (its access is none)."
    elif not on and house.runs_always(card):
        refused = f"Not sent: {name} must always run, so it is never switched off."
    else:
        refused = ""
    if refused:
        store.add_audit(app, site_id=site_id, at=now, actor="code", action="write_refused",
                        ref=ref, detail={**detail, "reason": refused})  # fmt: skip
        return refused
    if device.access != "write" or store.shadow(app, site_id=site_id):
        store.add_audit(app, site_id=site_id, at=now, actor="code", action="shadow_action",
                        ref=why, detail={**detail, "plug": plug})  # fmt: skip
        if device.access != "write":
            return (f"Recorded: {act}. {name} is set to advice, so nothing was sent. To let me"
                    " switch it, set its access to write in config.toml.")  # fmt: skip
        return f"Recorded: {act}. Shadow mode: nothing was sent to the plug."
    midnight = now.astimezone(ZoneInfo(cfg.tz)).replace(hour=0, minute=0, second=0, microsecond=0)
    if store.count_writes(app, site_id=site_id, plug=plug, since=midnight) >= WRITES_A_DAY:
        refused = f"Not sent: {name} was changed {WRITES_A_DAY} times today already."
        store.add_audit(app, site_id=site_id, at=now, actor="code", action="write_refused",
                        ref=ref, detail={**detail, "reason": refused})  # fmt: skip
        return refused
    try:
        p = connect(cfg.plugs[plug])
        p.switch(on)
        state = p.power()  # read back: the plug's answer to the command is not proof
    except PlugError as err:
        return _failed(app, site_id, ref, detail, now, f"Could not {act}: the plug did not"
                       f" answer ({err}).")  # fmt: skip
    if state != on:
        return _failed(app, site_id, ref, detail, now, f"Could not {act}: the plug still"
                       f" says {'on' if state else 'off'}.")  # fmt: skip
    store.add_audit(app, site_id=site_id, at=now, actor="code", action="write", ref=ref,
                    detail=detail)  # fmt: skip
    return f"Done: {name} is {'on' if on else 'off'}."


def _failed(app: Connection, site_id: int, ref: str, detail: dict[str, str], now: datetime,
            text: str) -> str:  # fmt: skip
    store.add_audit(app, site_id=site_id, at=now, actor="code", action="write_failed", ref=ref,
                    detail={**detail, "error": text})  # fmt: skip
    store.add_audit(app, site_id=site_id, at=now, actor="code", action="note", ref=ref,
                    detail={"text": text})  # fmt: skip
    return text
