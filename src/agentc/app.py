"""The web app: the inbox page and the taps from the phone.

It listens on the home network (and the tailnet, if the hub is on Tailscale). The page
shows open questions with their buttons, so anyone who can open it can answer: keep the
hub on a network you trust. Each answer needs a token signed with the secret, so a guessed
address does nothing. The jobs run apart from this app (`agentc run`).
"""

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime
from html import escape
from sqlite3 import Connection
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from agentc import __version__, config, jobs, notify, store
from agentc.config import Config
from agentc.notify import Message, NotSent, Publish

app = FastAPI(title="AgentC Edge", version=__version__)
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>body{{font:16px system-ui;margin:16px;max-width:640px}}section{{border:1px solid #ccc;
border-radius:8px;padding:12px;margin:12px 0}}button{{font:inherit;margin:4px 4px 0 0;
padding:8px 12px}}small{{color:#666}}</style></head><body>{body}</body></html>"""


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


Cfg = Annotated[Config, Depends(settings)]
Secret = Annotated[str, Depends(secret)]
Now = Annotated[datetime, Depends(clock)]
Say = Annotated[Publish | None, Depends(publisher)]
Db = Annotated[tuple[Connection, int], Depends(database)]


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check for the hub's service manager and CI."""
    return {"status": "ok", "version": __version__}


@app.get("/", response_class=HTMLResponse)
def inbox(cfg: Cfg, key: Secret, now: Now, data: Db) -> str:
    """The open questions that are due, each with its buttons, and the last day's notes."""
    db, site = data
    names = {mac: config.device(cfg, plug).name
             for plug, mac in store.plug_macs(db, site_id=site).items()}  # fmt: skip
    parts = ["<h1>Inbox</h1>"]
    due = [q for q in store.questions(db, site_id=site, status="open") if q.send_at <= now]
    for q in due:
        buttons = "".join(
            f'<form method="post" action="{escape(notify.answer_url("", key, q.id, n))}"'
            f' style="display:inline"><button>{escape(c.label)}</button></form>'
            for n, c in enumerate(q.decision.choices, 1)
        )
        parts.append(f"<section><b>{escape(names.get(q.plug_mac, '?'))}</b>"
                     f"<p>{escape(q.decision.text)}</p>{buttons}</section>")  # fmt: skip
    if not due:
        parts.append("<p>No open questions.</p>")
    notes = store.audit_since(db, site_id=site, action="note", since=now - jobs.NOTES_SINCE)
    if notes:
        items = "".join(f"<li>{escape(d['text'])}</li>" for d in notes)
        parts.append(f"<h2>Notes, last day</h2><ul>{items}</ul>")
    return PAGE.format(title="Inbox", body="".join(parts))


@app.post("/answer/{question_id}/{choice}/{tok}", response_class=HTMLResponse)
def answer(question_id: int, choice: int, tok: str, cfg: Cfg, key: Secret, now: Now,
           say: Say, data: Db) -> str:  # fmt: skip
    """A tap on one choice of one question."""
    if not notify.valid(key, tok, question_id, choice):
        raise HTTPException(403, "Not a button from this hub")
    db, site = data
    try:
        done = jobs.answer(db, cfg, site_id=site, question_id=question_id, choice=choice,
                           now=now)  # fmt: skip
    except LookupError as err:
        raise HTTPException(409, err.args[0]) from None
    return _done(done, say)


@app.post("/ping/{tok}", response_class=HTMLResponse)
def ping(tok: str, key: Secret, say: Say) -> str:
    """The phone's tap test (`agentc ping`): answers with a message back."""
    if not notify.valid(key, tok, "ping"):
        raise HTTPException(403, "Not a button from this hub")
    return _done("Tap works: the phone reached the hub.", say)


@app.post("/night/{night}/{does}/{tok}", response_class=HTMLResponse)
def answer_night(night: date, does: str, tok: str, cfg: Cfg, key: Secret, now: Now,
                 say: Say, data: Db) -> str:  # fmt: skip
    """One tap for the night's questions, from the morning message."""
    if not notify.valid(key, tok, night, does):
        raise HTTPException(403, "Not a button from this hub")
    db, site = data
    try:
        done = jobs.answer_night(db, cfg, site_id=site, night=night, does=does, now=now)
    except LookupError as err:
        raise HTTPException(409, err.args[0]) from None
    return _done(done, say)


def _done(text: str, say: Publish | None) -> str:
    """What the tap did: as a page (the inbox) and as a quiet message (the phone)."""
    if say:
        try:
            say(Message("Done", text, quiet=True))
        except NotSent:
            pass  # the answer is saved; only the echo is lost
    lines = "".join(f"<p>{escape(line)}</p>" for line in text.splitlines())
    return PAGE.format(title="Done", body=f"{lines}<p><a href='/'>Back to the inbox</a></p>")
