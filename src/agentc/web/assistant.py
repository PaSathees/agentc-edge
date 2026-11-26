"""The Ask tab (5d): the Assistant's chat. One running chat for the home; Start again clears
it. The talk streams as in the other chats (web/chat.py). A draft is done only by the
owner's tap: a habit is saved, a timer change and a switch off go through the Actuator.
"""

import threading
from datetime import datetime
from html import escape
from sqlite3 import Connection
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from agentc import actuator, assistant, notify, store
from agentc.config import Config
from agentc.llm import StepSend
from agentc.web import chat, plan
from agentc.web.deps import Cfg, Connect, Db, Model, Now, Secret, Tok, pages
from agentc.web.login import Owner

router = APIRouter(dependencies=[Owner])
BASE = "/ask"


@router.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request, key: Secret, now: Now, data: Db) -> HTMLResponse:
    """The Assistant's chat: the talk so far and a draft if there is one."""
    db, site = data
    chat_id, talk = _open(db, site, now)
    return pages.TemplateResponse(request, "chat.html", {
        **_view(db, chat_id, key), "title": "Ask", "page": "ask", "name": "Ask",
        "subtitle": "About your home's electricity", "talk": talk,
        "waiting": talk[-1][0] == "owner", "back": "/",
        "back_label": "Back to the inbox"})  # fmt: skip


@router.post("/ask/say", response_class=HTMLResponse)
def say(request: Request, text: Annotated[str, Form()], tok: Tok, key: Secret, now: Now,
        data: Db) -> HTMLResponse:  # fmt: skip
    """The owner's message: saved, then shown with a reply that streams in."""
    _check(key, tok)
    if not (text := text.strip()) or len(text) > chat.TEXT_MAX:
        raise HTTPException(422, f"Say something, in under {chat.TEXT_MAX} characters.")
    db, site = data
    chat_id, talk = _open(db, site, now)
    if talk[-1][0] == "owner":
        raise HTTPException(409, "Wait for the reply to your last message first.")
    store.add_turn(db, chat_id=chat_id, site_id=site, role="owner", text=text, now=now)
    return pages.TemplateResponse(request, "_said.html", {
        "role": "owner", "text": text, "tok": tok, "base": BASE})  # fmt: skip


@router.get("/ask/reply")
def reply(tok: str, cfg: Cfg, key: Secret, now: Now, model: Model,
          data: Db) -> StreamingResponse:  # fmt: skip
    """The Assistant's answer to the owner's last message, streamed; one run per chat."""
    _check(key, tok)
    db, site = data
    chat_id, talk = _open(db, site, now)
    args = (cfg, site, chat_id, talk, now, model)
    return chat.start_reply(chat_id, talk, _view(db, chat_id, key), lambda run: threading.Thread(
        target=_answer, args=(*args, run), daemon=True).start())  # fmt: skip


@router.post("/ask/act", response_class=HTMLResponse)
def act(tok: Tok, cfg: Cfg, key: Secret, now: Now, data: Db, connect: Connect) -> HTMLResponse:
    """The owner's tap on the draft: save the habit, do the timer change, or switch off."""
    _check(key, tok)
    db, site = data
    with plan.SENDING:  # one tap at a time, across the app
        chat_id, talk = _open(db, site, now)
        with chat.RUNS_LOCK:
            if chat_id in chat.RUNS or talk[-1][0] == "owner":  # a reply is due: its draft
                raise HTTPException(409, "Wait for the reply first.")
        found = store.chat(db, chat_id=chat_id)
        if found is None or found.draft is None:
            raise HTTPException(409, "Nothing to do: no draft in this chat.")
        d = assistant.Draft.from_json(found.draft)
        text = _do(db, cfg, site, chat_id, d, now, connect)
        store.set_draft(db, chat_id=chat_id, draft=None)
        store.clear_reads(db, chat_id=chat_id)  # the house may have changed: read it again
        store.add_turn(db, chat_id=chat_id, site_id=site, role="agent", text=text, now=now)
    return HTMLResponse(f'<section class="card"><p>{escape(text)}</p></section>')


@router.post("/ask/restart")
def restart(tok: Tok, key: Secret, now: Now, data: Db) -> RedirectResponse:
    """Start again: this chat is closed, and a new one starts greeted."""
    _check(key, tok)
    db, site = data
    chat_id, _ = _open(db, site, now)
    store.close_chat(db, chat_id=chat_id, status="closed")
    return RedirectResponse(BASE, status_code=303)


def _do(db: Connection, cfg: Config, site: int, chat_id: int, d: assistant.Draft,
        now: datetime, connect: Connect) -> str:  # fmt: skip
    if d.plug not in cfg.plugs:
        return "Not done: that device is not in config.toml any more."
    if d.action == "habit":
        try:
            return assistant.save_habit(db, d, site_id=site, now=now)
        except LookupError as err:
            return str(err)
    if d.action == "switch_off":
        text = actuator.switch(db, cfg, site_id=site, plug=d.plug, on=False,
                               why=f"ask:{chat_id}", now=now, connect=connect)  # fmt: skip
        return f"{text} Switch it back on from the inbox for a day." if text.startswith(
            "Done") else text  # fmt: skip
    if d.change is None:
        raise HTTPException(409, "Nothing to do: the draft has no change.")
    text = plan.send(db, cfg, site, d.plug, d.change, f"ask:{chat_id}", now, connect)
    return f"{text} You can put it back on the Plan tab for a day." if text.startswith(
        "Done") else text  # fmt: skip


def _check(key: str, tok: str) -> None:
    if not notify.valid(key, tok, "ask"):
        raise HTTPException(403, "Not a form from this hub")


def _open(db: Connection, site: int, now: datetime) -> tuple[int, list[tuple[str, str]]]:
    """The open chat and its talk; a new chat starts with the greeting, which code writes."""
    chat_id = store.open_assistant(db, site_id=site, now=now)
    if not store.turns(db, chat_id=chat_id):
        store.add_turn(db, chat_id=chat_id, site_id=site, role="agent",
                       text=assistant.GREETING, now=now)  # fmt: skip
    return chat_id, [(role, text) for _, role, text in store.turns(db, chat_id=chat_id)]


def _view(db: Connection, chat_id: int, key: str) -> dict[str, Any]:
    found = store.chat(db, chat_id=chat_id)
    try:
        draft = assistant.Draft.from_json(found.draft) if found and found.draft else None
    except (KeyError, TypeError, ValueError):  # a draft from an older version: drop it
        draft = None
    return {"draft": draft, "draft_tpl": "_ask_draft.html", "tok": notify.token(key, "ask"),
            "base": BASE}  # fmt: skip


def _answer(cfg: Config, site: int, chat_id: int, talk: list[tuple[str, str]], now: datetime,
            model: StepSend | None, run: chat.Run) -> None:  # fmt: skip
    chat.answer_with(cfg, site, chat_id, now, model, run, lambda t, brain, earlier, say:
                     assistant.reply(talk, tools=t, llm=brain,
                                     run_id=f"ask:{chat_id}:{len(talk)}", on_event=say,
                                     earlier=earlier))  # fmt: skip
