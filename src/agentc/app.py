"""The web app: the inbox page and the taps from the phone.

It listens on the home network (and the tailnet, if the hub is on Tailscale). The page
shows open questions with their buttons, so anyone who can open it can answer: keep the
hub on a network you trust. Each answer needs a token signed with the secret, so a guessed
address does nothing. The jobs run apart from this app (`agentc run`).
"""

import os
import queue
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime
from html import escape
from pathlib import Path
from sqlite3 import Connection
from typing import Annotated, Any

import anthropic
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agentc import __version__, config, house, jobs, llm, notify, setup, store
from agentc.agent_tools import Tools
from agentc.config import Config
from agentc.devices.base import days_words
from agentc.llm import OverBudget, StepSend, Unreachable
from agentc.notify import Message, NotSent, Publish

WEB = Path(__file__).parent / "web"
app = FastAPI(title="AgentC Edge", version=__version__)
app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
pages = Jinja2Templates(directory=WEB / "templates")  # escapes every value it prints
pages.env.filters["days"] = days_words  # "0111110" -> "Monday to Friday"


def settings() -> Config:
    """config.toml, from AGENTC_CONFIG (default: the working folder's)."""
    return config.load(os.environ.get("AGENTC_CONFIG", "config.toml"))


def secret(cfg: Annotated[Config, Depends(settings)]) -> str:
    """The key that signs the buttons: AGENTC_SECRET, else the data folder's own."""
    return config.secret(cfg)


def publisher(cfg: Annotated[Config, Depends(settings)],
              key: Annotated[str, Depends(secret)]) -> Publish | None:  # fmt: skip
    """ntfy, to say back what a tap did. None without AGENTC_HUB."""
    return notify.ntfy(cfg.owner, key) if cfg.owner else None


def database(cfg: Annotated[Config, Depends(settings)]) -> Iterator[tuple[Connection, int]]:
    """app.db for one request, and the site's id; closed after. The jobs make the tables."""
    db = store.open_app(cfg.app_db)
    try:
        if (site := store.site_id(db, name=cfg.site)) is None:
            raise HTTPException(503, "Starting: no data yet. Try again in a minute.")
        yield db, site
    finally:
        db.close()


def clock() -> datetime:
    return datetime.now(UTC)


def stepper() -> StepSend | None:
    """The model, for the setup chat. None without ANTHROPIC_API_KEY."""
    return llm.claude_step(anthropic.Anthropic()) if os.environ.get("ANTHROPIC_API_KEY") else None


Cfg = Annotated[Config, Depends(settings)]
Secret = Annotated[str, Depends(secret)]
Now = Annotated[datetime, Depends(clock)]
Say = Annotated[Publish | None, Depends(publisher)]
Db = Annotated[tuple[Connection, int], Depends(database)]
Model = Annotated[StepSend | None, Depends(stepper)]
Tok = Annotated[str, Form()]


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check for the hub's service manager and CI."""
    return {"status": "ok", "version": __version__}


@app.get("/", response_class=HTMLResponse)
def inbox(request: Request, cfg: Cfg, key: Secret, now: Now, data: Db) -> HTMLResponse:
    """The open questions that are due, each with its buttons; devices still to set up;
    and the last day's notes."""
    db, site = data
    macs = store.plug_macs(db, site_id=site)
    names = {mac: config.device(cfg, plug).name for plug, mac in macs.items()}
    questions = [
        {"device": names.get(q.plug_mac, "?"), "text": q.decision.text,
         "choices": [{"label": c.label, "url": notify.answer_url("", key, q.id, n)}
                     for n, c in enumerate(q.decision.choices, 1)]}
        for q in store.questions(db, site_id=site, status="open") if q.send_at <= now
    ]  # fmt: skip
    unknown = [(plug, config.device(cfg, plug).name) for plug, mac in macs.items()
               if not known(store.load_card(db, mac=mac))]  # fmt: skip
    notes = [d["text"] for d in store.audit_since(db, site_id=site, action="note",
                                                   since=now - jobs.NOTES_SINCE)]  # fmt: skip
    return pages.TemplateResponse(request, "inbox.html", {
        "title": "Inbox", "page": "inbox", "questions": questions, "unknown": unknown,
        "notes": notes})  # fmt: skip


def known(card: house.Card) -> bool:
    """Has the owner said anything about this device yet?"""
    return card != house.Card()


@app.post("/answer/{question_id}/{choice}/{tok}", response_class=HTMLResponse)
def answer(request: Request, question_id: int, choice: int, tok: str, cfg: Cfg, key: Secret,
           now: Now, say: Say, data: Db) -> HTMLResponse:  # fmt: skip
    """A tap on one choice of one question."""
    if not notify.valid(key, tok, question_id, choice):
        raise HTTPException(403, "Not a button from this hub")
    db, site = data
    try:
        done = jobs.answer(db, cfg, site_id=site, question_id=question_id, choice=choice,
                           now=now)  # fmt: skip
    except LookupError as err:
        raise HTTPException(409, err.args[0]) from None
    return _done(request, done, say)


@app.post("/ping/{tok}", response_class=HTMLResponse)
def ping(request: Request, tok: str, key: Secret, say: Say) -> HTMLResponse:
    """The phone's tap test (`agentc ping`): answers with a message back."""
    if not notify.valid(key, tok, "ping"):
        raise HTTPException(403, "Not a button from this hub")
    return _done(request, "Tap works: the phone reached the hub.", say)


@app.post("/night/{night}/{does}/{tok}", response_class=HTMLResponse)
def answer_night(request: Request, night: date, does: str, tok: str, cfg: Cfg, key: Secret,
                 now: Now, say: Say, data: Db) -> HTMLResponse:  # fmt: skip
    """One tap for the night's questions, from the morning message."""
    if not notify.valid(key, tok, night, does):
        raise HTTPException(403, "Not a button from this hub")
    db, site = data
    try:
        done = jobs.answer_night(db, cfg, site_id=site, night=night, does=does, now=now)
    except LookupError as err:
        raise HTTPException(409, err.args[0]) from None
    return _done(request, done, say)


def _done(request: Request, text: str, say: Publish | None) -> HTMLResponse:
    """What the tap did: as a page (the inbox) and as a quiet message (the phone)."""
    if say:
        try:
            say(Message("Done", text, quiet=True))
        except NotSent:
            pass  # the answer is saved; only the echo is lost
    return pages.TemplateResponse(request, "done.html", {
        "title": "Done", "page": "inbox", "lines": text.splitlines(), "back": "/",
        "back_label": "Back to the inbox"})  # fmt: skip


# the setup chat (3d3): the owner describes a device; only Save writes its card


@app.get("/setup", response_class=HTMLResponse)
def devices(request: Request, cfg: Cfg, data: Db) -> HTMLResponse:
    """Every plug, what the housemate knows about it, and a link to set it up or edit it."""
    db, site = data
    macs = store.plug_macs(db, site_id=site)
    rows = []
    for plug in (p for p in cfg.plugs if p in macs):
        c = store.load_card(db, mac=macs[plug])
        summary = ", ".join(x for x in (c.what, c.room) if x)
        rows.append({"plug": plug, "name": config.device(cfg, plug).name, "known": known(c),
                     "summary": summary})  # fmt: skip
    return pages.TemplateResponse(request, "devices.html", {
        "title": "Devices", "page": "setup", "devices": rows})  # fmt: skip


@app.get("/setup/{plug}", response_class=HTMLResponse)
def setup_page(request: Request, plug: str, cfg: Cfg, key: Secret, now: Now,
               data: Db) -> HTMLResponse:  # fmt: skip
    """The setup chat for one plug: the talk so far, a draft card if there is one."""
    db, site = data
    mac = _mac(cfg, db, site, plug)
    chat = store.open_chat(db, site_id=site, mac=mac, kind="setup", now=now)
    if not (talk := store.turns(db, chat_id=chat)):
        greet = setup.greeting(config.device(cfg, plug).name, store.load_card(db, mac=mac))
        store.add_turn(db, chat_id=chat, site_id=site, role="agent", text=greet, now=now)
        talk = store.turns(db, chat_id=chat)
    return pages.TemplateResponse(request, "setup.html", {
        **_chat_view(cfg, db, plug, mac, chat, key), "title": f"Set up {plug}", "page": "setup",
        "talk": [(role, text) for _, role, text in talk],
        "waiting": talk[-1][1] == "owner"})  # fmt: skip


@app.post("/setup/{plug}/say", response_class=HTMLResponse)
def say(request: Request, plug: str, text: Annotated[str, Form()], tok: Tok, cfg: Cfg,
        key: Secret, now: Now, data: Db) -> HTMLResponse:  # fmt: skip
    """The owner's message: saved, then shown with a reply that streams in."""
    _check(key, tok, plug)
    db, site = data
    chat = store.open_chat(db, site_id=site, mac=_mac(cfg, db, site, plug), kind="setup", now=now)
    store.add_turn(db, chat_id=chat, site_id=site, role="owner", text=text.strip(), now=now)
    return pages.TemplateResponse(request, "_said.html", {
        "role": "owner", "text": text.strip(), "plug": plug, "tok": tok})  # fmt: skip


@app.get("/setup/{plug}/reply")
def reply(plug: str, tok: str, cfg: Cfg, key: Secret, now: Now, model: Model,
          data: Db) -> StreamingResponse:  # fmt: skip
    """The agent's answer to the owner's last message, as Server-Sent Events: "step" (what it
    is looking at), "text" (its words as they come), then "done" (the finished answer)."""
    _check(key, tok, plug)
    db, site = data
    mac = _mac(cfg, db, site, plug)
    chat = store.open_chat(db, site_id=site, mac=mac, kind="setup", now=now)
    talk = [(role, text) for _, role, text in store.turns(db, chat_id=chat)]
    view = _chat_view(cfg, db, plug, mac, chat, key)
    if talk[-1][0] == "agent":  # answered already (a reload): just show it
        return _stream(iter([("done", (talk[-1][1], view["draft"]))]), view)
    news: queue.Queue[tuple[str, Any]] = queue.Queue()
    threading.Thread(target=_answer, daemon=True,
                     args=(cfg, site, plug, chat, talk, now, model, news)).start()  # fmt: skip
    return _stream(iter(news.get, None), view)


@app.post("/setup/{plug}/save", response_class=HTMLResponse)
def save(request: Request, plug: str, tok: Tok, cfg: Cfg, key: Secret, now: Now,
         data: Db) -> HTMLResponse:  # fmt: skip
    """The owner's Save: code checks the draft and writes the card and its habits."""
    _check(key, tok, plug)
    db, site = data
    chat = store.open_chat(db, site_id=site, mac=_mac(cfg, db, site, plug), kind="setup", now=now)
    try:
        said = setup.save(db, cfg, site_id=site, chat_id=chat, now=now)
    except LookupError as err:
        raise HTTPException(409, err.args[0]) from None
    return HTMLResponse(f'<section class="card" id="draft"><p>{escape(said)}</p>'
                        '<a href="/setup">Back to devices</a></section>')  # fmt: skip


@app.post("/setup/{plug}/restart")
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


def _chat_view(cfg: Config, db: Connection, plug: str, mac: str, chat: int, key: str
               ) -> dict[str, Any]:  # fmt: skip
    found = store.chat(db, chat_id=chat)
    raw = found[2] if found else None
    draft = setup.Draft.from_json(raw, base=store.load_card(db, mac=mac)) if raw else None
    return {"plug": plug, "name": config.device(cfg, plug).name, "draft": draft,
            "tok": notify.token(key, "setup", plug)}  # fmt: skip


def _answer(cfg: Config, site: int, plug: str, chat: int, talk: list[tuple[str, str]],
            now: datetime, model: StepSend | None, news: "queue.Queue[Any]") -> None:  # fmt: skip
    """Run one agent reply in its own thread (own database connections), sending events to
    the stream. The reply and its draft are saved; a failure is said, not saved."""
    db = store.open_app(cfg.app_db)
    try:
        if model is None:
            raise LookupError("No ANTHROPIC_API_KEY in .env: the setup chat needs the model.")
        if not cfg.energy_db.exists():
            raise LookupError("No readings yet: the logger starts them within a minute.")
        tools = Tools(db, store.open_readings(cfg.energy_db), cfg, site, now)
        brain = llm.Llm(db, site, cfg.tz, cfg.tiers, cfg.budget, model)
        r = setup.reply(talk, plug, tools=tools, llm=brain, run_id=f"setup:{chat}",
                        on_event=lambda kind, text: news.put((kind, text)))  # fmt: skip
        store.add_turn(db, chat_id=chat, site_id=site, role="agent", text=r.text, now=now)
        if r.draft:
            store.set_draft(db, chat_id=chat, draft=r.draft.to_json())
        news.put(("done", (r.text, r.draft)))
    except OverBudget:
        news.put(("done", ("The model budget for this month is used up.", None)))
    except Unreachable:
        news.put(("done", ("I can't reach the model now. Send it again later.", None)))
    except Exception as err:  # e.g. no key, or no readings yet: say it, keep the chat
        news.put(("done", (f"Something went wrong: {err}", None)))
    finally:
        db.close()
        news.put(None)


def _stream(news: Iterator[tuple[str, Any]], view: dict[str, Any]) -> StreamingResponse:
    """Events as Server-Sent Events. "done" carries the finished answer, rendered."""

    def events() -> Iterator[str]:
        for kind, payload in news:
            if kind == "done":
                text, draft = payload
                html = pages.get_template("_answer.html").render(
                    {**view, "role": "agent", "text": text, "draft": draft}
                )
                yield _event("done", html)
            else:
                yield _event(kind, str(escape(payload)))

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})  # fmt: skip


def _event(kind: str, data: str) -> str:
    lines = "".join(f"data: {line}\n" for line in data.strip("\n").split("\n"))
    return f"event: {kind}\n{lines}\n"
