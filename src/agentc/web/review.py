"""The weekly review chat's pages (5c): talk one plan item over, or the whole plan.

/plan/talk/{scope}: scope is an item's number, or "all" for the whole plan. The talk and
its streaming work as in the setup chat (web/chat.py). The agent's draft is done only by
the owner's tap on it, through the same Actuator and checks as Do it on the Plan tab.
"""

import threading
from datetime import datetime
from html import escape
from sqlite3 import Connection
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from agentc import config, llm, notify, readings, review, store
from agentc.agent_tools import Tools
from agentc.config import Config
from agentc.llm import OverBudget, StepSend, Unreachable
from agentc.web import chat, plan
from agentc.web.deps import Cfg, Connect, Db, Model, Now, Secret, Tok, pages
from agentc.web.login import Owner

router = APIRouter(dependencies=[Owner])


@router.get("/plan/talk/{scope}", response_class=HTMLResponse)
def talk_page(request: Request, scope: str, cfg: Cfg, key: Secret, now: Now,
              data: Db) -> HTMLResponse:  # fmt: skip
    """The chat about one item, or the whole plan: the talk so far and a draft if any."""
    db, site = data
    s = _scope(db, site, scope)
    chat_id, talk = _open(cfg, db, site, s, now)
    return pages.TemplateResponse(request, "chat.html", {
        **_view(cfg, db, site, scope, s, chat_id, key), "title": "Talk it over",
        "page": "plan", "talk": talk, "waiting": talk[-1][0] == "owner", "back": "/plan",
        "back_label": "Back to the plan"})  # fmt: skip


@router.post("/plan/talk/{scope}/say", response_class=HTMLResponse)
def say(request: Request, scope: str, text: Annotated[str, Form()], tok: Tok, cfg: Cfg,
        key: Secret, now: Now, data: Db) -> HTMLResponse:  # fmt: skip
    """The owner's message: saved, then shown with a reply that streams in."""
    _check(key, tok, scope)
    if not (text := text.strip()) or len(text) > chat.TEXT_MAX:
        raise HTTPException(422, f"Say something, in under {chat.TEXT_MAX} characters.")
    db, site = data
    chat_id, talk = _open(cfg, db, site, _scope(db, site, scope), now)
    if talk[-1][0] == "owner":
        raise HTTPException(409, "Wait for the reply to your last message first.")
    store.add_turn(db, chat_id=chat_id, site_id=site, role="owner", text=text, now=now)
    return pages.TemplateResponse(request, "_said.html", {
        "role": "owner", "text": text, "tok": tok, "base": f"/plan/talk/{scope}"})  # fmt: skip


@router.get("/plan/talk/{scope}/reply")
def reply(scope: str, tok: str, cfg: Cfg, key: Secret, now: Now, model: Model,
          data: Db) -> StreamingResponse:  # fmt: skip
    """The agent's answer to the owner's last message, streamed; one run per chat."""
    _check(key, tok, scope)
    db, site = data
    s = _scope(db, site, scope)
    chat_id, talk = _open(cfg, db, site, s, now)
    view = _view(cfg, db, site, scope, s, chat_id, key)
    if talk[-1][0] == "agent":  # answered already (a reload): just show it
        return chat.stream(iter([("done", (talk[-1][1], view["draft"]))]), view)
    with chat.RUNS_LOCK:
        run, started = chat.RUNS.get(chat_id), False
        if run is None:
            run, started = chat.Run(), True
            chat.RUNS[chat_id] = run
    listen = run.listen()
    if started:
        args = (cfg, site, s, chat_id, talk, now, model, run)
        threading.Thread(target=_answer, args=args, daemon=True).start()
    return chat.stream(chat.drain(listen), view)


@router.post("/plan/talk/{scope}/act", response_class=HTMLResponse)
def act(scope: str, tok: Tok, cfg: Cfg, key: Secret, now: Now, data: Db,
        connect: Connect) -> HTMLResponse:  # fmt: skip
    """The owner's tap on the draft: do the change, do it anyway, or keep it as it is."""
    _check(key, tok, scope)
    db, site = data
    chat_id, _ = _open(cfg, db, site, _scope(db, site, scope), now)
    with chat.RUNS_LOCK:
        if chat_id in chat.RUNS:
            raise HTTPException(409, "Wait for the reply first.")
    with plan.SENDING:  # one tap at a time, as on the Plan tab
        found = store.chat(db, chat_id=chat_id)
        if found is None or found.draft is None:
            raise HTTPException(409, "Nothing to do: no draft in this chat.")
        d = review.Draft.from_json(found.draft)
        item = store.plan_item(db, site_id=site, item_id=d.item)
        if item is None or item.status != "open":
            text = "Nothing changed: that item is closed already."
        elif d.action == "keep":
            store.decide_item(db, item_id=d.item, status="not_now", now=now, reason=d.reason)
            text = "Kept as it is. Your reason goes into next week's plan."
        elif d.plug not in cfg.plugs or d.change is None:
            text = "Not sent: that plug is not in config.toml any more."
        else:
            text = plan.send(db, cfg, site, d.plug, d.change, f"review:{chat_id}", now, connect)
            if text.startswith("Done"):
                store.decide_item(db, item_id=d.item, status="done", now=now)
        store.set_draft(db, chat_id=chat_id, draft=None)
        store.add_turn(db, chat_id=chat_id, site_id=site, role="agent", text=text, now=now)
    return HTMLResponse(f'<section class="card"><p>{escape(text)}</p>'
                        '<a href="/plan">Back to the plan</a></section>')  # fmt: skip


@router.post("/plan/talk/{scope}/restart")
def restart(scope: str, tok: Tok, cfg: Cfg, key: Secret, now: Now,
            data: Db) -> RedirectResponse:  # fmt: skip
    """Start the chat again: this one is closed."""
    _check(key, tok, scope)
    db, site = data
    chat_id, _ = _open(cfg, db, site, _scope(db, site, scope), now)
    store.close_chat(db, chat_id=chat_id, status="closed")
    return RedirectResponse(f"/plan/talk/{scope}", status_code=303)


def _check(key: str, tok: str, scope: str) -> None:
    if not notify.valid(key, tok, "review", scope):
        raise HTTPException(403, "Not a form from this hub")


def _scope(db: Connection, site: int, scope: str) -> review.Scope:
    """The newest plan, and the item asked for ("all": the whole plan)."""
    latest = store.latest_plan(db, site_id=site)
    if latest is None:
        raise HTTPException(404, "No plan yet")
    if scope == "all":
        return review.Scope(latest[0], None)
    ids = {i.id for i in store.plan_items(db, plan_id=latest[0])}
    if not scope.isdigit() or int(scope) not in ids:
        raise HTTPException(404, "No such item in this week's plan")
    return review.Scope(latest[0], int(scope))


def _tools(cfg: Config, db: Connection, site: int, now: datetime) -> Any:  # noqa: ANN401
    energy = readings.connect(cfg.energy_db)
    try:
        return review.tools_for(Tools(db, energy, cfg, site, now)), energy
    except BaseException:  # pragma: no cover (tools_for only builds a dataclass)
        energy.close()
        raise


def _open(cfg: Config, db: Connection, site: int, s: review.Scope, now: datetime
          ) -> tuple[int, list[tuple[str, str]]]:  # fmt: skip
    """The open chat and its talk; a new chat starts with the greeting, which code writes."""
    chat_id = store.open_review(db, site_id=site, plan_id=s.plan_id, item_id=s.item_id, now=now)
    if not store.turns(db, chat_id=chat_id):
        t, energy = _tools(cfg, db, site, now)
        try:
            greet = review.greeting(t, s)
        finally:
            energy.close()
        store.add_turn(db, chat_id=chat_id, site_id=site, role="agent", text=greet, now=now)
    return chat_id, [(role, text) for _, role, text in store.turns(db, chat_id=chat_id)]


def _view(cfg: Config, db: Connection, site: int, scope: str, s: review.Scope, chat_id: int,
          key: str) -> dict[str, Any]:  # fmt: skip
    found = store.chat(db, chat_id=chat_id)
    draft = review.Draft.from_json(found.draft) if found and found.draft else None
    if s.item_id is None:
        name, what = "This week's plan", "the whole plan"
    else:
        item = next(i for i in store.plan_items(db, plan_id=s.plan_id) if i.id == s.item_id)
        plug = next((p for p, m in store.plug_macs(db, site_id=site).items()
                     if m == item.plug_mac), "")  # fmt: skip
        name = config.device(cfg, plug).name if plug in cfg.plugs else "?"
        what = "a plan item"
    return {"name": name, "subtitle": f"Talking over {what}", "draft": draft,
            "draft_tpl": "_plan_draft.html",
            "tok": notify.token(key, "review", scope), "base": f"/plan/talk/{scope}"}  # fmt: skip


def _answer(cfg: Config, site: int, s: review.Scope, chat_id: int, talk: list[tuple[str, str]],
            now: datetime, model: StepSend | None, run: chat.Run) -> None:  # fmt: skip
    """Run one reply in its own thread, with its own database connections. The reply and
    its draft are saved; a failure is said, not saved, so sending again retries it."""
    db = energy = None
    if not chat.AT_ONCE.acquire(blocking=False):
        run.put(("done", ("Busy with another reply. Send it again in a moment.", None)))
        _end(chat_id, run)
        return
    try:
        if model is None:
            raise LookupError("No ANTHROPIC_API_KEY in .env: the chat needs the model.")
        db = store.open_app(cfg.app_db)
        t, energy = _tools(cfg, db, site, now)
        brain = llm.Llm(db, site, cfg.tz, cfg.tiers, cfg.budget, model)
        found = store.chat(db, chat_id=chat_id)
        r = review.reply(talk, s, tools=t, llm=brain, run_id=f"review:{chat_id}:{len(talk)}",
                         on_event=lambda kind, text: run.put((kind, text)),
                         earlier=found.reads if found else ())  # fmt: skip
        store.add_reads(db, chat_id=chat_id, reads=r.reads)
        store.add_turn(db, chat_id=chat_id, site_id=site, role="agent", text=r.text, now=now)
        store.set_draft(db, chat_id=chat_id, draft=r.draft.to_json() if r.draft else None)
        run.put(("done", (r.text, r.draft)))
    except OverBudget as err:
        why = ("This month's model budget is used up." if "month" in str(err)
               else "That reply hit its cost cap. Try a shorter message.")  # fmt: skip
        run.put(("done", (why, None)))
    except Unreachable:
        run.put(("done", ("I can't reach the model now. Send it again later.", None)))
    except Exception as err:  # e.g. no key: say it, keep the chat
        run.put(("done", (f"Something went wrong: {err}", None)))
    finally:
        chat.AT_ONCE.release()
        for conn in (db, energy):
            if conn is not None:
                conn.close()
        _end(chat_id, run)


def _end(chat_id: int, run: chat.Run) -> None:
    with chat.RUNS_LOCK:
        if chat.RUNS.get(chat_id) is run:
            del chat.RUNS[chat_id]
    run.finish()
