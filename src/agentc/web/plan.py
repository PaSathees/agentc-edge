"""The Plan tab (5b5): the week's plan, with Do it, Done and Not now on each item.

Do it sends a timer change through the Actuator, with all its checks; Done marks an action
the owner does by hand; Not now keeps a change away for 30 days. An item that cut real use
is only to talk over: Not now, but no Do it. A timer change can be put back for a day.
Every form carries a token signed with the secret, and answers with a redirect, so a reload
cannot send it again. The message the redirect carries is signed too, so a link from
anywhere else cannot put its own words on the page.
"""

import json
import threading
from datetime import datetime
from sqlite3 import Connection
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from agentc import actuator, config, jobs, notify, planner, store
from agentc.config import Config
from agentc.web.deps import Cfg, Connect, Db, Now, Secret, Tok, pages
from agentc.web.login import Owner

router = APIRouter(dependencies=[Owner])
DOES = ("do", "done", "not_now")
SAID = {"done": "Marked done.", "not_now": "Not now: it will not come back for 30 days."}
TOO_LONG = 300  # characters of a message shown on the page
SENDING = threading.Lock()  # one Do it at a time: a double tap must not send a change twice
SENDS = ("move_timer", "add_pair")  # the changes Do it can send


def month_line(db: Connection, cfg: Config, site: int, now: datetime) -> tuple[str, str]:
    """The month against the goal as the daily job last saw it, and its state; ("", "") if
    there is no goal, or the status is last month's or for an old goal."""
    month = jobs.goal_status(db, site_id=site) if cfg.goal else None
    if (month is None or cfg.goal is None
            or month.month != f"{now.astimezone(ZoneInfo(cfg.tz)):%Y-%m}"
            or month.goal_kg != cfg.goal.month_kg):  # fmt: skip
        return "", ""
    return month.text(tz=cfg.tz), month.state


def open_count(db: Connection, site: int) -> tuple[int, int]:
    """The newest plan's open items, and all its items: for the inbox line."""
    latest = store.latest_plan(db, site_id=site)
    items = store.plan_items(db, plan_id=latest[0]) if latest else []
    return sum(i.status == "open" for i in items), len(items)


@router.get("/plan", response_class=HTMLResponse)
def plan_page(request: Request, cfg: Cfg, key: Secret, now: Now, data: Db, said: str = "",
              sig: str = "") -> HTMLResponse:  # fmt: skip
    """The newest plan, each item with its buttons, and the timer changes that can still be
    put back."""
    db, site = data
    zone = ZoneInfo(cfg.tz)
    names = {mac: config.device(cfg, plug).name
             for plug, mac in store.plug_macs(db, site_id=site).items()}  # fmt: skip
    latest = store.latest_plan(db, site_id=site)
    items = [_row(i, names, key) for i in store.plan_items(db, plan_id=latest[0])] if latest else []
    undo = [{"name": config.device(cfg, plug).name, "what": _what(detail),
             "until": f"{(at + actuator.UNDO_FOR).astimezone(zone):%a %d %b %H:%M}",
             "url": f"/plan/undo/{wid}", "tok": notify.token(key, "plan_undo", wid)}
            for wid, plug, detail, at in _undoable(db, site, now)]  # fmt: skip
    goal, state = month_line(db, cfg, site, now)
    made = f"{datetime.fromisoformat(latest[3]).astimezone(zone):%a %d %b %H:%M}" if latest else ""
    shown = said[:TOO_LONG] if notify.valid(key, sig, "said", said) else ""
    return pages.TemplateResponse(request, "plan.html", {
        "title": "Plan", "page": "plan", "plan": latest, "summary": latest[2] if latest else "",
        "made": made, "items": items, "undo": undo, "goal": goal, "goal_state": state,
        "said": shown})  # fmt: skip


@router.post("/plan/undo/{write_id}")  # before /plan/{item_id}/{does}: that would match it
def undo(write_id: int, tok: Tok, cfg: Cfg, key: Secret, now: Now, data: Db,
         connect: Connect) -> RedirectResponse:  # fmt: skip
    """Put a plug's timers back as they were before a change, for a day after it."""
    if not notify.valid(key, tok, "plan_undo", write_id):
        raise HTTPException(403, "Not a form from this hub")
    db, site = data
    with SENDING:
        found = {wid: (plug, detail) for wid, plug, detail, _ in _undoable(db, site, now)}
        if write_id not in found:
            raise HTTPException(409, "It was changed again since, or it is over a day old.")
        plug, detail = found[write_id]
        text = actuator.put_back(db, cfg, site_id=site, plug=plug, write_id=write_id,
                                 old=detail["old"], new=detail.get("new"), now=now,
                                 connect=connect)  # fmt: skip
    return _back(text, key)


@router.post("/plan/{item_id}/{does}")
def decide(item_id: int, does: str, tok: Tok, cfg: Cfg, key: Secret, now: Now, data: Db,
           connect: Connect) -> RedirectResponse:  # fmt: skip
    """Do it, Done or Not now on one item. An item already closed is left as it is. Only a
    change that went through closes it: one recorded in shadow mode, refused or failed
    stays open, to do for real later."""
    if does not in DOES or not notify.valid(key, tok, "plan", item_id, does):
        raise HTTPException(403, "Not a form from this hub")
    db, site = data
    with SENDING:  # read the status and act on it as one step
        item = store.plan_item(db, site_id=site, item_id=item_id)
        if item is None:
            raise HTTPException(404, "No such plan item")
        if item.status != "open":
            status = item.status.replace("_", " ")
            return _back(f"Nothing changed: this item is {status} already.", key)
        change = json.loads(item.change)
        action = change.get("kind") == "action"
        if does == "do" and (action or not item.ready):
            raise HTTPException(409, "Only a ready timer change can be done from here.")
        if does == "done" and not action:
            raise HTTPException(409, "Done is for an action you do by hand; use Do it.")
        if does != "do":
            store.decide_item(db, item_id=item_id, status=does, now=now)
            return _back(SAID[does], key)
        plug = next((p for p, m in store.plug_macs(db, site_id=site).items()
                     if m == item.plug_mac), None)  # fmt: skip
        if plug is None or plug not in cfg.plugs:
            return _back("Not sent: that plug is not in config.toml any more.", key)
        text = _send(db, cfg, site, plug, planner.Change(**change), f"plan:{item_id}", now,
                     connect)  # fmt: skip
        if text.startswith("Done"):
            store.decide_item(db, item_id=item_id, status="done", now=now)
    return _back(text, key)


def _send(db: Connection, cfg: Config, site: int, plug: str, c: planner.Change, why: str,
          now: datetime, connect: Connect) -> str:  # fmt: skip
    if c.kind == "move_timer" and c.slot is not None and c.time:
        return actuator.move_timer(db, cfg, site_id=site, plug=plug, slot=c.slot, time=c.time,
                                   why=why, now=now, was=c.from_time, connect=connect)  # fmt: skip
    if c.kind == "add_pair" and c.off and c.on and c.days:
        return actuator.set_timer(db, cfg, site_id=site, plug=plug, off=c.off, on=c.on,
                                  days=c.days, why=why, now=now, connect=connect)  # fmt: skip
    raise HTTPException(409, "This change cannot be sent from here yet.")


def _row(i: store.PlanItem, names: dict[str, str], key: str) -> dict[str, Any]:
    change = json.loads(i.change)
    action = change.get("kind") == "action"
    buttons = [("do", "Do it")] if i.ready and change.get("kind") in SENDS else []
    buttons += [("done", "Done")] if action else []
    buttons += [("not_now", "Not now")]
    return {"device": names.get(i.plug_mac, "?"), "action": change.get("do", "") if action else "",
            "was": i.was, "becomes": i.becomes, "why": i.why, "saving": i.saving,
            "ready": i.ready, "status": i.status, "tag": _tag(i, action),
            "buttons": [{"label": label, "url": f"/plan/{i.id}/{does}",
                         "tok": notify.token(key, "plan", i.id, does)}
                        for does, label in buttons] if i.status == "open" else []}  # fmt: skip


def _tag(i: store.PlanItem, action: bool) -> str:
    if i.status != "open":
        return {"not_now": "Not now"}.get(i.status, i.status.capitalize())
    return "You do it" if action else "Ready" if i.ready else "To talk over"


def _undoable(db: Connection, site: int, now: datetime
              ) -> list[tuple[int, str, dict[str, str], datetime]]:  # fmt: skip
    """Timer changes from the last day that can be put back (those that kept the old
    timers), with when each was made."""
    rows = store.timer_undoable(db, site_id=site, since=now - actuator.UNDO_FOR)
    return [row for row in rows if row[2].get("old")]


def _what(detail: dict[str, str]) -> str:
    if detail.get("does") == "move_timer":
        return f"timer {detail.get('slot')} moved to {detail.get('time')}"
    return f"off at {detail.get('off')}, on at {detail.get('on')}"


def _back(said: str, key: str) -> RedirectResponse:
    said = said[:TOO_LONG]
    sig = notify.token(key, "said", said)
    return RedirectResponse(f"/plan?said={quote(said)}&sig={sig}", status_code=303)
