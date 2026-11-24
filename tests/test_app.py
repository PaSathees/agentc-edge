"""The web app on the hub: health, the inbox, and taps signed with the secret (3d)."""

import base64
import hashlib
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentc import app as web
from agentc import config, notify, store
from agentc.house import Card
from agentc.llm import OverBudget, Unreachable
from agentc.owner import Choice, Decision
from agentc.watcher import Event
from agentc.web import chat as webchat
from tests.test_cli import EXAMPLE

KEY = "s3cret"
NOW = datetime(2026, 11, 2, 1, 30, tzinfo=UTC)  # 07:00 in Colombo
OFF, OK = Choice("Switch off", "switch_off_now"), Choice("Normal", "save_habit")
ASK = Decision("ask", "Desk 2 is still on. Switch it off?", (OFF, OK))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The app on a fresh app.db, with one question due now and one note."""
    cfg = config.load(EXAMPLE)
    energy = tmp_path / "energy.db"  # empty: never the repo's own readings
    sqlite3.connect(energy).execute("CREATE TABLE readings (ts_utc, plug, power_w, error)")
    cfg = config.Config(**{**cfg.__dict__, "app_db": tmp_path / "app.db", "energy_db": energy})
    db = store.connect(cfg.app_db)
    store.add_site(db, name=cfg.site, tz=cfg.tz)
    store.plug_seen(db, site_id=1, mac="AA", name="Plug-1", ip="192.0.2.11", timers_on=True,
                    now=NOW)  # fmt: skip
    e = Event("left_on", "AA", NOW - timedelta(hours=5), {"minutes": 40, "peak_w": 5.0})
    store.add_event(db, site_id=1, event=e, dedupe="e")
    store.add_question(db, site_id=1, event_id=1, mac="AA", decision=ASK, send_at=NOW,
                       now=NOW - timedelta(hours=5))  # fmt: skip
    store.add_audit(db, site_id=1, at=NOW - timedelta(hours=2), actor="agent", action="note",
                    ref="event:1", detail={"text": "Desk 1 ran <late>."})  # fmt: skip
    said = []
    monkeypatch.setenv("AGENTC_SECRET", KEY)
    web.app.dependency_overrides = {web.settings: lambda: cfg, web.clock: lambda: NOW,
                                    web.publisher: lambda: said.append}  # fmt: skip
    yield TestClient(web.app), db, said
    web.app.dependency_overrides = {}


def test_health_reports_ok_and_version():
    r = TestClient(web.app).get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": "0.1.0"}


def test_the_inbox_shows_each_due_question_with_its_buttons(client):
    c, _, _ = client
    page = " ".join(c.get("/").text.split())  # one space between tags and words
    assert "<b>Desk 2</b> <p>Desk 2 is still on. Switch it off?</p>" in page
    assert f'action="/answer/1/2/{notify.token(KEY, 1, 2)}"' in page and ">Normal</button>" in page
    assert "<li>Desk 1 ran &lt;late&gt;.</li>" in page  # notes, escaped


def test_the_inbox_shows_a_night_question_only_once_it_is_due(client):
    c, db, _ = client
    later = Decision("ask", "Due tomorrow?", (OFF, OK))
    store.add_question(db, site_id=1, event_id=1, mac="AA", decision=later,
                       send_at=NOW + timedelta(days=1), now=NOW)  # fmt: skip
    assert "Due tomorrow?" not in c.get("/").text


def test_the_inbox_with_nothing_open(client):
    c, db, _ = client
    store.close_question(db, question_id=1)
    assert "<p>No open questions.</p>" in c.get("/").text


def test_a_tap_answers_and_says_what_it_did(client):
    c, db, said = client
    r = c.post(f"/answer/1/2/{notify.token(KEY, 1, 2)}")
    assert r.status_code == 200
    assert "Saved: Desk 2 is normal on Mondays, 02:00 to 07:00." in r.text
    assert [m.text for m in said] == ["Saved: Desk 2 is normal on Mondays, 02:00 to 07:00."]
    assert store.questions(db, site_id=1)[0].status == "answered"


def test_a_tap_with_a_wrong_token_is_refused(client):
    c, db, _ = client
    assert c.post(f"/answer/1/1/{notify.token(KEY, 1, 2)}").status_code == 403
    assert store.questions(db, site_id=1)[0].status == "open"


def test_a_second_tap_finds_nothing_open(client):
    c, _, _ = client
    url = f"/answer/1/1/{notify.token(KEY, 1, 1)}"
    c.post(url)
    r = c.post(url)
    assert (r.status_code, r.json()["detail"]) == (409, "No open question 1.")


def test_the_night_tap_answers_the_nights_questions(client):
    c, _, said = client
    r = c.post(f"/night/2026-11-02/switch_off_now/{notify.token(KEY, '2026-11-02', OFF.does)}")
    assert r.status_code == 200 and "Recorded: Switch off." in r.text and len(said) == 1


def test_a_night_tap_needs_its_own_token_and_an_open_night(client):
    c, _, _ = client
    assert c.post("/night/2026-11-02/save_habit/bad").status_code == 403
    tok = notify.token(KEY, "2026-11-03", "save_habit")
    assert c.post(f"/night/2026-11-03/save_habit/{tok}").status_code == 409


def test_without_agentc_secret_the_data_folder_makes_one(client, monkeypatch):
    c, db, _ = client
    monkeypatch.delenv("AGENTC_SECRET")
    page = c.get("/").text
    made = Path(db.execute("PRAGMA database_list").fetchone()[2]).parent / "secret"
    key = made.read_text()
    assert len(key) == 64 and oct(made.stat().st_mode)[-3:] == "600"
    assert f"/answer/1/1/{notify.token(key, 1, 1)}" in page  # and the buttons use it
    assert c.get("/").text == page  # the same one next time


def test_an_echo_ntfy_refused_still_saves_the_answer(client):
    c, db, _ = client

    def down(m):
        raise notify.NotSent("no ntfy")

    web.app.dependency_overrides[web.publisher] = lambda: down
    assert c.post(f"/answer/1/1/{notify.token(KEY, 1, 1)}").status_code == 200
    assert store.questions(db, site_id=1)[0].status == "answered"


def test_the_real_dependencies(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTC_CONFIG", str(EXAMPLE))
    monkeypatch.setenv("AGENTC_HUB", "http://192.0.2.50")
    cfg = web.settings()
    assert cfg.owner is not None and web.publisher(cfg, "k") is not None
    assert web.publisher(config.Config(**{**cfg.__dict__, "owner": None}), "k") is None
    assert web.clock().tzinfo is UTC


def test_before_the_jobs_make_app_db_the_inbox_says_wait(tmp_path, monkeypatch):
    cfg = config.load(EXAMPLE)
    cfg = config.Config(**{**cfg.__dict__, "app_db": tmp_path / "app.db"})
    monkeypatch.setenv("AGENTC_SECRET", KEY)
    web.app.dependency_overrides = {web.settings: lambda: cfg}
    try:
        r = TestClient(web.app).get("/")
    finally:
        web.app.dependency_overrides = {}
    assert (r.status_code, r.json()["detail"]) == (
        503,
        "Starting: no data yet. Try again in a minute.",
    )


def test_the_tap_test_answers_back(client):
    c, _, said = client
    assert c.post(f"/ping/{notify.token(KEY, 'ping')}").status_code == 200
    assert [m.text for m in said] == ["Tap works: the phone reached the hub."]
    assert c.post("/ping/forged").status_code == 403


def test_the_inbox_lists_devices_with_no_card_yet(client):
    c, db, _ = client
    page = c.get("/").text
    assert 'href="/setup/Plug-1"' in page and "Devices to set up" in page
    store.save_card(db, site_id=1, mac="AA", card=Card("computer"), now=NOW)
    assert "Devices to set up" not in c.get("/").text


def test_the_pages_load_their_own_css_and_htmx(client):
    c, _, _ = client
    page = c.get("/").text
    assert "/static/app.css" in page and "/static/htmx.min.js" in page and "/static/sse.js" in page
    for f in ("app.css", "htmx.min.js", "sse.js"):
        assert c.get(f"/static/{f}").status_code == 200


# the setup chat (3d3)


def speaks(*steps):
    """A stand-in model for the chat: each step's blocks in turn; text is streamed."""
    left = list(steps)

    def send(*, tier, system, messages, tools, on_text=None):
        blocks = left.pop(0)
        for b in blocks:
            if b["type"] == "text" and on_text:
                on_text(b["text"])
        return blocks, "end_turn", 1000, 100

    return send


ROOF = {"kind": "light", "room": "living room", "what": "roof light", "notes": "",
        "must_run": "no", "habits": [], "message": "Here is the card. Save it?"}  # fmt: skip


def events(body):
    """A Server-Sent Events body as (event, data) pairs."""
    out = []
    for block in body.strip().split("\n\n"):
        lines = block.split("\n")
        name = lines[0].removeprefix("event: ")
        out.append((name, "\n".join(x.removeprefix("data: ") for x in lines[1:])))
    return out


@pytest.fixture
def chat(client):
    """The setup chat page for Plug-1, opened once. Returns (client, db, the form token)."""
    c, db, _ = client
    c.get("/setup/Plug-1")
    return c, db, notify.token(KEY, "setup", "Plug-1")


def model(steps):
    web.app.dependency_overrides[web.stepper] = lambda: speaks(*steps)


def test_the_devices_page_lists_every_plug_seen(client):
    c, _, _ = client
    page = c.get("/setup").text
    assert "Desk 2" in page and 'href="/setup/Plug-1">Set up</a>' in page


def test_opening_a_chat_greets_by_code(chat):
    c, db, tok = chat
    page = c.get("/setup/Plug-1").text
    assert "Let&#39;s set up Desk 2. What is plugged into it?" in page
    assert f'value="{tok}"' in page and "/static/chat.js" in page
    assert len(store.turns(db, chat_id=1)) == 1  # opened twice, greeted once


def test_a_message_is_saved_and_its_reply_streams(chat):
    c, db, tok = chat
    r = c.post("/setup/Plug-1/say", data={"text": "the roof light", "tok": tok})
    assert 'class="bubble owner"' in r.text and "the roof light" in r.text
    assert f'sse-connect="/setup/Plug-1/reply?tok={tok}"' in r.text
    model([[{"type": "tool_use", "id": "t1", "name": "power", "input": {"device": "Desk 2",
                                                                         "hours": 24}}],
           [{"type": "text", "text": "Which room?"}]])  # fmt: skip
    got = events(c.get(f"/setup/Plug-1/reply?tok={tok}").text)
    assert got[0] == ("step", "looking at its power")
    assert got[1] == ("text", "Which room?")
    assert got[-1][0] == "done" and "data-say>Which room?</div>" in got[-1][1]
    assert [role for _, role, _ in store.turns(db, chat_id=1)] == ["agent", "owner", "agent"]


def test_a_draft_card_shows_with_save_and_only_save_writes_it(chat):
    c, db, tok = chat
    c.post("/setup/Plug-1/say", data={"text": "roof light, living room", "tok": tok})
    model([[{"type": "tool_use", "id": "t1", "name": "propose_card", "input": ROOF}]])
    done = events(c.get(f"/setup/Plug-1/reply?tok={tok}").text)[-1][1]
    assert "<dt>Kind</dt><dd>light</dd>" in done and ">Save card</button>" in done
    assert store.load_card(db, mac="AA") == Card()  # nothing saved yet
    assert "Card for Desk 2" in c.get("/setup/Plug-1").text  # the draft survives a reload
    r = c.post("/setup/Plug-1/save", data={"tok": tok})
    assert "Saved the card for Desk 2." in r.text
    assert store.load_card(db, mac="AA").what == "roof light"
    assert c.post("/setup/Plug-1/save", data={"tok": tok}).status_code == 409


def test_a_reload_while_answered_shows_the_answer_without_a_model_call(chat):
    c, db, tok = chat
    store.add_turn(db, chat_id=1, site_id=1, role="owner", text="x", now=NOW)
    store.add_turn(db, chat_id=1, site_id=1, role="agent", text="Which room?", now=NOW)
    web.app.dependency_overrides[web.stepper] = lambda: None
    assert events(c.get(f"/setup/Plug-1/reply?tok={tok}").text) == [
        ("done", '<div class="bubble agent" data-say>Which room?</div>')
    ]


@pytest.mark.parametrize(
    ("brain", "said"),
    [(lambda: None, "No ANTHROPIC_API_KEY in .env"),
     (lambda: _raises(Unreachable("no internet")), "I can&#39;t reach the model now"),
     (lambda: _raises(OverBudget("month cap USD 3.0 reached")), "model budget is used up"),
     (lambda: _raises(OverBudget("run cap USD 0.05 reached")), "hit its cost cap")],
)  # fmt: skip
def test_a_reply_that_cannot_run_says_why_and_keeps_the_chat(chat, brain, said):
    c, db, tok = chat
    c.post("/setup/Plug-1/say", data={"text": "hello", "tok": tok})
    web.app.dependency_overrides[web.stepper] = brain
    assert said in events(c.get(f"/setup/Plug-1/reply?tok={tok}").text)[-1][1]
    assert store.turns(db, chat_id=1)[-1][1] == "owner"  # not saved: send again later


def _raises(err):
    def send(**_):
        raise err

    return send


def test_setup_forms_need_this_hubs_token(chat):
    c, _, _ = chat
    assert c.post("/setup/Plug-1/say", data={"text": "x", "tok": "bad"}).status_code == 403
    assert c.get("/setup/Plug-1/reply?tok=bad").status_code == 403
    assert c.post("/setup/Plug-1/save", data={"tok": "bad"}).status_code == 403
    assert c.get("/setup/Plug-9").status_code == 404


def test_start_again_closes_the_chat_unsaved(chat):
    c, db, tok = chat
    r = c.post("/setup/Plug-1/restart", data={"tok": tok}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/setup/Plug-1")
    assert store.chat(db, chat_id=1).status == "closed"  # type: ignore[union-attr]
    c.get("/setup/Plug-1")
    assert store.open_chat(db, site_id=1, mac="AA", kind="setup", now=NOW) == 2


def test_the_real_model_door_is_used_with_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert web.stepper() is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert callable(web.stepper())


def test_with_no_readings_yet_the_chat_says_so(chat, tmp_path):
    c, _, tok = chat
    (tmp_path / "energy.db").unlink()
    c.post("/setup/Plug-1/say", data={"text": "hello", "tok": tok})
    model([[{"type": "text", "text": "never"}]])
    assert "No readings yet" in events(c.get(f"/setup/Plug-1/reply?tok={tok}").text)[-1][1]


# from the review before merge: one run per chat, and a stream that always ends


def test_a_second_stream_joins_the_run_in_progress_and_pays_once(chat):
    c, _, tok = chat
    c.post("/setup/Plug-1/say", data={"text": "the roof light", "tok": tok})
    run = webchat.Run()  # a reply already running for chat 1 (another tab, a reconnect)
    webchat.RUNS[1] = run
    run.put(("text", "Which "))
    run.put(("done", ("Which room?", None)))
    run.finish()
    web.app.dependency_overrides[web.stepper] = lambda: _raises(AssertionError("paid twice"))
    try:
        got = events(c.get(f"/setup/Plug-1/reply?tok={tok}").text)
    finally:
        webchat.RUNS.clear()
    assert got[0] == ("text", "Which ") and "Which room?" in got[-1][1]


def test_a_run_tells_a_late_listener_everything_so_far():
    run = webchat.Run()
    run.put(("step", "looking"))
    early = run.listen()
    run.put(("text", "Hi"))
    run.finish()
    late = run.listen()
    for q in (early, late):
        assert [q.get() for _ in range(3)] == [("step", "looking"), ("text", "Hi"), None]


def test_a_quiet_run_ends_the_stream_with_a_note(monkeypatch):
    monkeypatch.setattr(webchat, "WAIT_S", 0.01)
    silent = webchat.Run().listen()
    (only,) = list(webchat._drain(silent))  # pyright: ignore[reportPrivateUsage]
    assert only[0] == "done" and "taking too long" in only[1][0]


def test_two_replies_at_once_is_the_most(chat, monkeypatch):
    c, _, tok = chat
    c.post("/setup/Plug-1/say", data={"text": "hello", "tok": tok})
    monkeypatch.setattr(webchat, "AT_ONCE", threading.BoundedSemaphore(0))  # all busy
    model([[{"type": "text", "text": "never"}]])
    assert "Busy with another reply" in events(c.get(f"/setup/Plug-1/reply?tok={tok}").text)[-1][1]


def test_a_broken_worker_still_ends_the_stream(chat, monkeypatch):
    c, _, tok = chat
    c.post("/setup/Plug-1/say", data={"text": "hello", "tok": tok})
    model([[{"type": "text", "text": "never"}]])
    monkeypatch.setattr(webchat.store, "open_readings", lambda path: _raises(OSError("disk"))())
    assert (
        "Something went wrong: disk" in events(c.get(f"/setup/Plug-1/reply?tok={tok}").text)[-1][1]
    )


def test_a_reply_that_cannot_render_still_ends_with_done(chat, monkeypatch):
    c, _, tok = chat
    c.post("/setup/Plug-1/say", data={"text": "hello", "tok": tok})
    model([[{"type": "text", "text": "Which room?"}]])

    def broken(name):
        raise ValueError("bad template")

    monkeypatch.setattr(webchat.pages, "get_template", broken)
    last = events(c.get(f"/setup/Plug-1/reply?tok={tok}").text)[-1]
    assert last[0] == "done" and "Could not show the reply: bad template" in last[1]


def test_text_keeps_its_new_lines_and_loses_carriage_returns():
    assert webchat._event("text", "a\r\nb\n") == "event: text\ndata: a\ndata: b\ndata: \n\n"  # pyright: ignore[reportPrivateUsage]


def test_typing_after_save_starts_a_new_greeted_chat(chat):
    c, db, tok = chat
    c.post("/setup/Plug-1/say", data={"text": "roof light, living room", "tok": tok})
    model([[{"type": "tool_use", "id": "t1", "name": "propose_card", "input": ROOF}]])
    c.get(f"/setup/Plug-1/reply?tok={tok}")
    c.post("/setup/Plug-1/save", data={"tok": tok})
    r = c.post("/setup/Plug-1/say", data={"text": "one more thing", "tok": tok})
    assert r.status_code == 200
    assert [role for _, role, _ in store.turns(db, chat_id=2)] == ["agent", "owner"]


@pytest.mark.parametrize(("text", "code"), [("   ", 422), ("x" * 1001, 422)])
def test_empty_or_huge_messages_are_refused(chat, text, code):
    c, _, tok = chat
    assert c.post("/setup/Plug-1/say", data={"text": text, "tok": tok}).status_code == code


def test_a_second_message_waits_for_the_reply(chat):
    c, db, tok = chat
    c.post("/setup/Plug-1/say", data={"text": "one", "tok": tok})
    assert c.post("/setup/Plug-1/say", data={"text": "two", "tok": tok}).status_code == 409
    assert [role for _, role, _ in store.turns(db, chat_id=1)] == ["agent", "owner"]


def test_the_vendored_scripts_match_their_recorded_hashes():
    static = web.WEB / "static"
    recorded = (static / "README.md").read_text()
    for name in ("htmx.min.js", "sse.js"):
        got = base64.b64encode(hashlib.sha256((static / name).read_bytes()).digest()).decode()
        assert f"`{got}`" in recorded, name
