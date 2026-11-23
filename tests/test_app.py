"""The web app on the hub: health, the inbox, and taps signed with the secret (3d)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from agentc import app as web
from agentc import config, notify, store
from agentc.owner import Choice, Decision
from agentc.watcher import Event
from tests.test_cli import EXAMPLE

KEY = "s3cret"
NOW = datetime(2026, 11, 2, 1, 30, tzinfo=UTC)  # 07:00 in Colombo
OFF, OK = Choice("Switch off", "switch_off_now"), Choice("Normal", "save_habit")
ASK = Decision("ask", "Desk 2 is still on. Switch it off?", (OFF, OK))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The app on a fresh app.db, with one question due now and one note."""
    cfg = config.load(EXAMPLE)
    cfg = config.Config(**{**cfg.__dict__, "app_db": tmp_path / "app.db"})
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
    page = c.get("/").text
    assert "<b>Desk 2</b><p>Desk 2 is still on. Switch it off?</p>" in page
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


def test_without_a_secret_answers_are_off(client, monkeypatch):
    c, _, _ = client
    monkeypatch.delenv("AGENTC_SECRET")
    assert c.get("/").status_code == 503


def test_an_echo_ntfy_refused_still_saves_the_answer(client):
    c, db, _ = client

    def down(m):
        raise notify.NotSent("no ntfy")

    web.app.dependency_overrides[web.publisher] = lambda: down
    assert c.post(f"/answer/1/1/{notify.token(KEY, 1, 1)}").status_code == 200
    assert store.questions(db, site_id=1)[0].status == "answered"


def test_the_real_dependencies(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTC_CONFIG", str(EXAMPLE))
    cfg = web.settings()
    assert cfg.owner is not None and web.publisher(cfg) is not None
    assert web.publisher(config.Config(**{**cfg.__dict__, "owner": None})) is None
    assert web.clock().tzinfo is UTC
