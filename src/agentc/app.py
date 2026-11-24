"""The web app: the inbox page, the taps from the phone, and the setup chat (web/chat.py).

Pages need the owner's login (web/login.py): Tailscale or a password. The phone's buttons
need none: each carries a token signed with the secret, so a guessed address does nothing,
and once a question is answered its token does nothing either. The jobs run apart from this
app (`agentc run`).
"""

from datetime import date

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from agentc import __version__, config, jobs, notify, store
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
    return pages.TemplateResponse(request, "inbox.html", {
        "title": "Inbox", "page": "inbox", "questions": questions, "unknown": unknown,
        "notes": notes})  # fmt: skip


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
