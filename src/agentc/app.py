"""The web app on the hub: the inbox page and the taps from the phone (3d).

Served inside Tailscale only (a private network of the owner's own devices). Every answer
needs a token signed with AGENTC_SECRET, so a button from a message or the inbox works,
and a guessed URL does not. The jobs run apart from this app (`agentc watch`).
"""

import os
from datetime import UTC, date, datetime
from html import escape
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


def secret() -> str:
    if not (s := os.environ.get("AGENTC_SECRET", "")):
        raise HTTPException(503, "No AGENTC_SECRET: answers are switched off")
    return s


def publisher(cfg: Annotated[Config, Depends(settings)]) -> Publish | None:
    """ntfy, to say back what a tap did. None without [owner]."""
    return notify.ntfy(cfg.owner) if cfg.owner else None


def clock() -> datetime:
    return datetime.now(UTC)


Cfg = Annotated[Config, Depends(settings)]
Secret = Annotated[str, Depends(secret)]
Now = Annotated[datetime, Depends(clock)]
Say = Annotated[Publish | None, Depends(publisher)]


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check for the hub's service manager and CI."""
    return {"status": "ok", "version": __version__}


@app.get("/", response_class=HTMLResponse)
def inbox(cfg: Cfg, key: Secret, now: Now) -> str:
    """The open questions that are due, each with its buttons, and the last day's notes."""
    db = store.connect(cfg.app_db)
    site = store.add_site(db, name=cfg.site, tz=cfg.tz)
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
           say: Say) -> str:  # fmt: skip
    """A tap on one choice of one question."""
    if not notify.valid(key, tok, question_id, choice):
        raise HTTPException(403, "Not a button from this hub")
    db = store.connect(cfg.app_db)
    site = store.add_site(db, name=cfg.site, tz=cfg.tz)
    try:
        done = jobs.answer(db, cfg, site_id=site, question_id=question_id, choice=choice,
                           now=now)  # fmt: skip
    except LookupError as err:
        raise HTTPException(409, err.args[0]) from None
    return _done(done, say)


@app.post("/night/{night}/{does}/{tok}", response_class=HTMLResponse)
def answer_night(night: date, does: str, tok: str, cfg: Cfg, key: Secret, now: Now,
                 say: Say) -> str:  # fmt: skip
    """One tap for the night's questions, from the morning message."""
    if not notify.valid(key, tok, night, does):
        raise HTTPException(403, "Not a button from this hub")
    db = store.connect(cfg.app_db)
    site = store.add_site(db, name=cfg.site, tz=cfg.tz)
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
