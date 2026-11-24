"""The setup chat's pages (FR-30): the owner describes a device; only Save writes its card.

A reply runs once per chat, in its own thread, and streams to the page as Server-Sent
Events: "step" (what it is looking at), "text" (its words as they come), "done" (the
finished answer). A reload, a second tab or a dropped connection joins the run in
progress instead of starting another one, so no reply is paid for twice.
"""

import queue
import threading
from collections.abc import Iterator
from datetime import datetime
from html import escape
from sqlite3 import Connection
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from agentc import config, llm, notify, readings, setup, store
from agentc.agent_tools import Tools
from agentc.config import Config
from agentc.llm import OverBudget, StepSend, Unreachable
from agentc.web.deps import Cfg, Db, Model, Now, Secret, Tok, known, pages
from agentc.web.login import Owner, Ways

router = APIRouter(dependencies=[Owner])  # every chat page and form needs the login
AT_ONCE = threading.BoundedSemaphore(2)  # model replies running at once, on the whole hub
WAIT_S = 120  # a stream gives up after this long with no news
TEXT_MAX = 1000  # characters in one owner message
Event = tuple[str, Any]


class Run:
    """One reply in progress, shared by every stream that asks for it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.seen: list[Event] = []
        self.listeners: list[queue.Queue[Event | None]] = []
        self.over = False

    def put(self, event: Event) -> None:
        with self.lock:
            self.seen.append(event)
            for q in self.listeners:
                q.put(event)

    def finish(self) -> None:
        with self.lock:
            self.over = True
            for q in self.listeners:
                q.put(None)

    def listen(self) -> "queue.Queue[Event | None]":
        """A queue with everything said so far, then the rest as it comes."""
        with self.lock:
            q: queue.Queue[Event | None] = queue.Queue()
            for event in self.seen:
                q.put(event)
            if self.over:
                q.put(None)
            self.listeners.append(q)
            return q


RUNS: dict[int, Run] = {}  # chat id -> its reply in progress
RUNS_LOCK = threading.Lock()


@router.get("/setup", response_class=HTMLResponse)
def devices(request: Request, cfg: Cfg, key: Secret, ways: Ways, data: Db) -> HTMLResponse:
    """Every plug, what the housemate knows about it, and a link to set it up or edit it.
    On top, shadow mode, and which devices a tap may switch."""
    db, site = data
    macs = store.plug_macs(db, site_id=site)
    rows = []
    for plug in (p for p in cfg.plugs if p in macs):
        c = store.load_card(db, mac=macs[plug])
        summary = ", ".join(x for x in (c.what, c.room) if x)
        rows.append({"plug": plug, "name": config.device(cfg, plug).name, "known": known(c),
                     "summary": summary})  # fmt: skip
    writable = [config.device(cfg, p).name for p in cfg.plugs
                if config.device(cfg, p).access == "write"]  # fmt: skip
    return pages.TemplateResponse(request, "devices.html", {
        "title": "Devices", "page": "setup", "devices": rows, "writable": writable,
        "shadow": store.shadow(db, site_id=site), "login": ways.set,
        "tok": notify.token(key, "shadow")})  # fmt: skip


@router.post("/shadow")
def shadow(on: Annotated[str, Form()], tok: Tok, key: Secret, now: Now, ways: Ways,
           data: Db) -> RedirectResponse:  # fmt: skip
    """The owner turns shadow mode on or off. Off needs a login first: else anyone who can
    open the page could switch plugs."""
    if not notify.valid(key, tok, "shadow"):
        raise HTTPException(403, "Not a form from this hub")
    if on not in ("on", "off"):
        raise HTTPException(422, "on or off")
    if on == "off" and not ways.set:
        raise HTTPException(409, "Set a login in .env first: AGENTC_PASSWORD or"
                            " AGENTC_TAILSCALE_USER.")  # fmt: skip
    db, site = data
    store.set_shadow(db, site_id=site, on=on == "on", now=now)
    return RedirectResponse("/setup", status_code=303)


@router.get("/setup/{plug}", response_class=HTMLResponse)
def setup_page(request: Request, plug: str, cfg: Cfg, key: Secret, now: Now,
               data: Db) -> HTMLResponse:  # fmt: skip
    """The setup chat for one plug: the talk so far, a draft card if there is one."""
    db, site = data
    mac = _mac(cfg, db, site, plug)
    chat, talk = _open(cfg, db, site, plug, mac, now)
    return pages.TemplateResponse(request, "setup.html", {
        **_view(cfg, db, plug, mac, chat, key), "title": f"Set up {plug}", "page": "setup",
        "talk": talk, "waiting": talk[-1][0] == "owner"})  # fmt: skip


@router.post("/setup/{plug}/say", response_class=HTMLResponse)
def say(request: Request, plug: str, text: Annotated[str, Form()], tok: Tok, cfg: Cfg,
        key: Secret, now: Now, data: Db) -> HTMLResponse:  # fmt: skip
    """The owner's message: saved, then shown with a reply that streams in."""
    _check(key, tok, plug)
    if not (text := text.strip()) or len(text) > TEXT_MAX:
        raise HTTPException(422, f"Say something, in under {TEXT_MAX} characters.")
    db, site = data
    mac = _mac(cfg, db, site, plug)
    chat, talk = _open(cfg, db, site, plug, mac, now)  # a new chat after Save starts greeted
    if talk[-1][0] == "owner":
        raise HTTPException(409, "Wait for the reply to your last message first.")
    store.add_turn(db, chat_id=chat, site_id=site, role="owner", text=text, now=now)
    return pages.TemplateResponse(request, "_said.html", {
        "role": "owner", "text": text, "plug": plug, "tok": tok})  # fmt: skip


@router.get("/setup/{plug}/reply")
def reply(plug: str, tok: str, cfg: Cfg, key: Secret, now: Now, model: Model,
          data: Db) -> StreamingResponse:  # fmt: skip
    """The agent's answer to the owner's last message, streamed (see the module's notes)."""
    _check(key, tok, plug)
    db, site = data
    mac = _mac(cfg, db, site, plug)
    chat, talk = _open(cfg, db, site, plug, mac, now)
    view = _view(cfg, db, plug, mac, chat, key)
    if talk[-1][0] == "agent":  # answered already (a reload): just show it
        return _stream(iter([("done", (talk[-1][1], view["draft"]))]), view)
    with RUNS_LOCK:
        run, started = RUNS.get(chat), False
        if run is None:
            run, started = Run(), True
            RUNS[chat] = run
    listen = run.listen()
    if started:
        draft = view["draft"]
        args = (cfg, site, plug, chat, talk, draft, now, model, run)
        threading.Thread(target=_answer, args=args, daemon=True).start()
    return _stream(_drain(listen), view)


@router.post("/setup/{plug}/save", response_class=HTMLResponse)
def save(plug: str, tok: Tok, cfg: Cfg, key: Secret, now: Now, data: Db) -> HTMLResponse:
    """The owner's Save: code checks the draft and writes the card and its habits."""
    _check(key, tok, plug)
    db, site = data
    chat = store.open_chat(db, site_id=site, mac=_mac(cfg, db, site, plug), kind="setup", now=now)
    try:
        said = setup.save(db, cfg, site_id=site, chat_id=chat, now=now)
    except LookupError as err:
        raise HTTPException(409, err.args[0]) from None
    return HTMLResponse(f'<section class="card"><p>{escape(said)}</p>'
                        '<a href="/setup">Back to devices</a></section>')  # fmt: skip


@router.post("/setup/{plug}/restart")
def restart(plug: str, tok: Tok, cfg: Cfg, key: Secret, now: Now, data: Db) -> RedirectResponse:
    """Start the chat again: this one is closed, unsaved."""
    _check(key, tok, plug)
    db, site = data
    chat = store.open_chat(db, site_id=site, mac=_mac(cfg, db, site, plug), kind="setup", now=now)
    store.close_chat(db, chat_id=chat, status="closed")
    return RedirectResponse(f"/setup/{plug}", status_code=303)


def _check(key: str, tok: str, plug: str) -> None:
    if not notify.valid(key, tok, "setup", plug):
        raise HTTPException(403, "Not a form from this hub")


def _mac(cfg: Config, db: Connection, site: int, plug: str) -> str:
    if plug not in cfg.plugs or (mac := store.plug_macs(db, site_id=site).get(plug)) is None:
        raise HTTPException(404, f"No plug {plug} seen yet")
    return mac


def _open(cfg: Config, db: Connection, site: int, plug: str, mac: str, now: datetime
          ) -> tuple[int, list[tuple[str, str]]]:  # fmt: skip
    """The plug's open chat and its talk; a new chat starts with the greeting."""
    chat = store.open_chat(db, site_id=site, mac=mac, kind="setup", now=now)
    if not store.turns(db, chat_id=chat):
        greet = setup.greeting(config.device(cfg, plug).name, store.load_card(db, mac=mac))
        store.add_turn(db, chat_id=chat, site_id=site, role="agent", text=greet, now=now)
    return chat, [(role, text) for _, role, text in store.turns(db, chat_id=chat)]


def _view(cfg: Config, db: Connection, plug: str, mac: str, chat: int, key: str
          ) -> dict[str, Any]:  # fmt: skip
    found = store.chat(db, chat_id=chat)
    raw = found.draft if found else None
    draft = setup.Draft.from_json(raw, base=store.load_card(db, mac=mac)) if raw else None
    return {"plug": plug, "name": config.device(cfg, plug).name, "draft": draft,
            "tok": notify.token(key, "setup", plug)}  # fmt: skip


def _answer(cfg: Config, site: int, plug: str, chat: int, talk: list[tuple[str, str]],
            draft: setup.Draft | None, now: datetime, model: StepSend | None, run: Run
            ) -> None:  # fmt: skip
    """Run one reply in its own thread, with its own database connections. The reply and
    its draft are saved; a failure is said, not saved, so sending again retries it."""
    db = energy = None
    if not AT_ONCE.acquire(blocking=False):
        run.put(("done", ("Busy with another reply. Send it again in a moment.", None)))
        _end(chat, run)
        return
    try:
        if model is None:
            raise LookupError("No ANTHROPIC_API_KEY in .env: the setup chat needs the model.")
        if not cfg.energy_db.exists():
            raise LookupError("No readings yet: the logger starts them within a minute.")
        db, energy = store.open_app(cfg.app_db), readings.connect(cfg.energy_db)
        brain = llm.Llm(db, site, cfg.tz, cfg.tiers, cfg.budget, model)
        found = store.chat(db, chat_id=chat)
        r = setup.reply(talk, plug, tools=Tools(db, energy, cfg, site, now), llm=brain,
                        run_id=f"setup:{chat}:{len(talk)}", draft=draft,
                        on_event=lambda kind, text: run.put((kind, text)),
                        earlier=found.reads if found else ())  # fmt: skip
        store.add_reads(db, chat_id=chat, reads=r.reads)
        store.add_turn(db, chat_id=chat, site_id=site, role="agent", text=r.text, now=now)
        if r.draft:
            store.set_draft(db, chat_id=chat, draft=r.draft.to_json())
        run.put(("done", (r.text, r.draft)))
    except OverBudget as err:
        why = ("This month's model budget is used up." if "month" in str(err)
               else "That reply hit its cost cap. Try a shorter message.")  # fmt: skip
        run.put(("done", (why, None)))
    except Unreachable:
        run.put(("done", ("I can't reach the model now. Send it again later.", None)))
    except Exception as err:  # e.g. no key, no readings yet: say it, keep the chat
        run.put(("done", (f"Something went wrong: {err}", None)))
    finally:
        AT_ONCE.release()
        for conn in (db, energy):
            if conn is not None:
                conn.close()
        _end(chat, run)


def _end(chat: int, run: Run) -> None:
    with RUNS_LOCK:
        if RUNS.get(chat) is run:
            del RUNS[chat]
    run.finish()


def _drain(listen: "queue.Queue[Event | None]") -> Iterator[Event]:
    """The run's events, until it ends. If it goes quiet too long, say so and stop."""
    while True:
        try:
            event = listen.get(timeout=WAIT_S)
        except queue.Empty:
            yield ("done", ("This is taking too long. Reload the page to see the reply.", None))
            return
        if event is None:
            return
        yield event


def _stream(news: Iterator[Event], view: dict[str, Any]) -> StreamingResponse:
    """Events as Server-Sent Events. "done" carries the finished answer, rendered."""

    def events() -> Iterator[str]:
        for kind, payload in news:
            if kind != "done":
                yield _event(kind, str(escape(payload)))
                continue
            text, draft = payload
            try:
                html = pages.get_template("_answer.html").render(
                    {**view, "role": "agent", "text": text, "draft": draft}
                )
            except Exception as err:  # never end without "done": the page would reconnect
                html = (
                    f'<div class="bubble agent">Could not show the reply: {escape(str(err))}</div>'
                )
            yield _event("done", html.strip("\n"))

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})  # fmt: skip


def _event(kind: str, data: str) -> str:
    """One SSE event. A carriage return would end a line early, so it is dropped."""
    lines = "".join(f"data: {line}\n" for line in data.replace("\r", "").split("\n"))
    return f"event: {kind}\n{lines}\n"
