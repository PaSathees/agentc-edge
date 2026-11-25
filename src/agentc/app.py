"""The web app: the inbox page, the taps from the phone, and the setup chat (web/chat.py).

Pages need the owner's login (web/login.py): Tailscale or a password. The phone's buttons
need none: each carries a token signed with the secret, so a guessed address does nothing,
and once a question is answered its token does nothing either. The jobs run apart from this
app (`agentc run`).
"""

from datetime import date, datetime
from sqlite3 import Connection

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from agentc import __version__, actuator, config, jobs, notify, store
from agentc.config import Config
from agentc.notify import Message, NotSent, Publish
from agentc.web import chat, login
from agentc.web.deps import (  # noqa: F401 (re-exported: tests override these)
    WEB,
    Cfg,
    Db,
    Now,
    Say,
    Secret,
    clock,
    connect,
    database,
    known,
    pages,
    publisher,
    secret,
    settings,
    stepper,
)

app = FastAPI(title="AgentC Edge", version=__version__)
app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
app.add_middleware(login.HostCheck)
app.include_router(login.router)
app.include_router(chat.router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check for the hub's service manager and CI."""
    return {"status": "ok", "version": __version__}


@app.get("/", response_class=HTMLResponse, dependencies=[login.Owner])
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
    undo = _undo(db, cfg, key, site, since=now - actuator.UNDO_FOR, hub="")
    return pages.TemplateResponse(request, "inbox.html", {
        "title": "Inbox", "page": "inbox", "questions": questions, "unknown": unknown,
        "notes": notes, "undo": undo})  # fmt: skip


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
    return _done(request, done, say, _switched(db, cfg, key, site, now), _hub(cfg))


@app.post("/undo/{write_id}/{tok}", response_class=HTMLResponse)
def undo(request: Request, write_id: int, tok: str, cfg: Cfg, key: Secret, now: Now, say: Say,
         data: Db) -> HTMLResponse:  # fmt: skip
    """Switch a plug back on after a switch off, from the inbox or the phone."""
    if not notify.valid(key, tok, "undo", write_id):
        raise HTTPException(403, "Not a button from this hub")
    db, site = data
    found = dict(store.undoable(db, site_id=site, since=now - actuator.UNDO_FOR))
    if (plug := found.get(write_id)) is None:
        raise HTTPException(409, "It was changed again since, or it is over a day old.")
    done = actuator.switch(db, cfg, site_id=site, plug=plug, on=True, why=f"undo:{write_id}",
                           now=now)  # fmt: skip
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
    return _done(request, done, say, _switched(db, cfg, key, site, now), _hub(cfg))


def _undo(db: Connection, cfg: Config, key: str, site: int, *, since: datetime, hub: str
          ) -> list[tuple[str, str]]:  # fmt: skip
    """Each switch off since a time that can be undone, as (device name, undo url)."""
    return [(config.device(cfg, plug).name, notify.undo_url(hub, key, wid))
            for wid, plug in store.undoable(db, site_id=site, since=since)]  # fmt: skip


def _hub(cfg: Config) -> str:
    return cfg.owner.inbox_url if cfg.owner else ""


def _switched(db: Connection, cfg: Config, key: str, site: int, now: datetime
              ) -> list[tuple[str, str]]:  # fmt: skip
    """Undo buttons for what this tap switched off: its rows are stamped with its time."""
    return _undo(db, cfg, key, site, since=now, hub="")


def _done(request: Request, text: str, say: Publish | None,
          undo: list[tuple[str, str]] | None = None, hub: str = "") -> HTMLResponse:  # fmt: skip
    """What the tap did: as a page (the inbox) and as a quiet message (the phone), with a
    button to switch back on anything it switched off. hub: the phone's address for the hub."""
    undo = undo or []
    if say:
        buttons = tuple(
            notify.Button(f"Switch {name} back on", f"{hub}{url}") for name, url in undo
        )
        try:
            say(Message("Done", text, buttons[: notify.MAX_BUTTONS], quiet=True))
        except NotSent:
            pass  # the answer is saved; only the echo is lost
    return pages.TemplateResponse(request, "done.html", {
        "title": "Done", "page": "inbox", "lines": text.splitlines(), "back": "/", "undo": undo,
        "back_label": "Back to the inbox"})  # fmt: skip
