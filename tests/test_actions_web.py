"""A tap that switches a plug, its undo, and the shadow mode switch, in the web app (4b)."""

import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from agentc import app as web
from agentc import config, notify, store
from agentc.config import Device
from agentc.devices.base import Timer
from agentc.devices.tasmota import TasmotaPlug
from agentc.house import Card
from agentc.web import login
from tests.test_app import KEY, NOW, client  # noqa: F401 (the fixture)

TAP = f"/answer/1/1/{notify.token(KEY, 1, 1)}"  # "Switch off" on Desk 2's question


@pytest.fixture
def live(client, plug, monkeypatch):  # noqa: F811 (the fixture)
    """Desk 2 (Plug-1) on the fake plug, set to write; shadow mode off; a login set; a phone
    to echo to."""
    monkeypatch.setenv("AGENTC_PASSWORD", "pw")
    monkeypatch.setattr(login, "LAST_WRONG", [0.0])
    c, db, said = client
    cfg = web.app.dependency_overrides[web.settings]()
    hub = config.Owner("http://192.0.2.50")
    cfg = replace(cfg, plugs={"Plug-1": plug.ip}, owner=hub,
                  devices={"Plug-1": Device("Desk 2", access="write")})  # fmt: skip
    web.app.dependency_overrides[web.settings] = lambda: cfg
    store.set_shadow(db, site_id=1, on=False, now=NOW)
    plug.relay_on = True
    c.post("/login", data={"password": "pw", "next": "/"})  # a login is set, so pages ask
    return c, db, said, plug


def test_a_tap_switches_off_and_offers_the_undo_on_page_and_phone(live):
    c, db, said, plug = live
    r = c.post(TAP)
    assert "Done: Desk 2 is off." in r.text and not plug.relay_on
    ((wid, _),) = store.undoable(db, site_id=1, since=NOW)
    url = notify.undo_url("", KEY, wid)
    assert f'action="{url}"' in r.text and "Switch Desk 2 back on" in r.text
    (m,) = said
    assert [(b.label, b.url) for b in m.buttons] == [
        ("Switch Desk 2 back on", f"http://192.0.2.50:8765{url}")
    ]
    assert f'action="{url}"' in c.get("/").text  # and in the inbox, for a day


def test_undo_switches_it_back_on_once(live):
    c, db, said, plug = live
    c.post(TAP)
    ((wid, _),) = store.undoable(db, site_id=1, since=NOW)
    url = notify.undo_url("", KEY, wid)
    assert "Done: Desk 2 is on." in c.post(url).text and plug.relay_on
    r = c.post(url)
    assert (r.status_code, r.json()["detail"]) == (
        409, "It was changed again since, or it is over a day old.")  # fmt: skip
    assert "Switched off, last day" not in c.get("/").text


def test_an_undo_needs_its_own_token(live):
    c, _, _, _ = live
    c.post(TAP)
    assert c.post("/undo/1/forged").status_code == 403


def test_in_shadow_mode_a_tap_offers_no_undo(client):  # noqa: F811
    c, _, said = client
    r = c.post(TAP)
    assert "Recorded" in r.text and "back on" not in r.text and said[0].buttons == ()


# the shadow mode switch, on the Devices page


def test_the_devices_page_says_shadow_mode_is_on_and_how_to_turn_it_off(client):  # noqa: F811
    c, _, _ = client
    page = c.get("/setup").text
    assert "Shadow mode is on" in page and "set its access to write in config.toml" in page
    assert "set a login in .env first" in page and 'action="/shadow"' not in page


def test_with_a_login_the_owner_turns_shadow_mode_off_and_on(live):
    c, db, _, _ = live
    web.app.dependency_overrides[login.login] = lambda: login.Login("pw")
    c.post("/login", data={"password": "pw", "next": "/setup"})
    store.set_shadow(db, site_id=1, on=True, now=NOW)
    tok = notify.token(KEY, "shadow")
    page = c.get("/setup").text
    assert "Turn shadow mode off" in page and f'value="{tok}"' in page
    r = c.post("/shadow", data={"on": "off", "tok": tok}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/setup")
    assert not store.shadow(db, site_id=1)
    page = c.get("/setup").text
    assert 'A tap on "Switch off" switches Desk 2.' in page and "Turn shadow mode back on" in page
    c.post("/shadow", data={"on": "on", "tok": tok})
    assert store.shadow(db, site_id=1)


def test_shadow_mode_stays_on_without_a_login(client):  # noqa: F811
    c, db, _ = client
    r = c.post("/shadow", data={"on": "off", "tok": notify.token(KEY, "shadow")})
    assert r.status_code == 409 and store.shadow(db, site_id=1)


@pytest.mark.parametrize(("on", "tok", "code"), [("off", "forged", 403), ("maybe", None, 422)])
def test_the_shadow_form_needs_its_token_and_on_or_off(client, on, tok, code):  # noqa: F811
    c, _, _ = client
    data = {"on": on, "tok": tok or notify.token(KEY, "shadow")}
    assert c.post("/shadow", data=data).status_code == code


# the Devices page: power, timers, and the switch (ADR-008, amended 2026-11-25)


def test_the_devices_page_shows_state_power_and_timers(live):
    c, db, _, plug = live
    store.save_timers(db, site_id=1, mac="AA", now=NOW, timers=[
        Timer(1, True, 0, "06:00", 0, "1111111", True, 1, 1),
        Timer(9, True, 0, "01:30", 0, "0111110", True, 1, 0)])  # fmt: skip
    store.save_card(db, site_id=1, mac="AA", now=NOW, card=Card(why={9: "set by AgentC"}))
    energy = web.app.dependency_overrides[web.settings]().energy_db
    with sqlite3.connect(energy) as e:
        e.execute("INSERT INTO readings VALUES (?, 'Plug-1', 2.4, NULL)",
                  ((NOW - timedelta(minutes=1)).isoformat(),))  # fmt: skip
    page = c.get("/setup").text
    assert "On · " in page and "about 2 W" in page
    assert "Timer 1: On at 06:00, every day" in page and "· yours" in page
    assert 'Timer 9: Off at 01:30, Monday to Friday <span class="muted">(set by AgentC)' in page
    assert (
        f'value="{notify.token(KEY, "switch", "Plug-1")}"' in page
        and ">Switch off</button>" in page
    )


def test_the_switch_goes_through_the_actuator(live):
    c, db, _, plug = live
    tok = notify.token(KEY, "switch", "Plug-1")
    r = c.post("/switch/Plug-1", data={"on": "off", "tok": tok})
    assert "Done: Desk 2 is off." in r.text and "Back to devices" in r.text and not plug.relay_on
    assert "Done: Desk 2 is on." in c.post("/switch/Plug-1", data={"on": "on", "tok": tok}).text
    why = [d for (d,) in db.execute("SELECT detail FROM audit WHERE action = 'write'")]
    assert all('"why": "devices"' in d for d in why) and len(why) == 2


@pytest.mark.parametrize(
    ("name", "on", "tok", "code"),
    [
        ("Plug-1", "off", "forged", 403),
        ("Plug-1", "maybe", None, 422),
        ("Plug-99", "off", None, 422),
    ],
)
def test_the_switch_needs_its_token_a_known_plug_and_on_or_off(live, name, on, tok, code):
    c, _, _, _ = live
    data = {"on": on, "tok": tok or notify.token(KEY, "switch", name)}
    assert c.post(f"/switch/{name}", data=data).status_code == code


def test_a_write_device_that_does_not_answer_shows_no_switch(live):
    c, _, _, plug = live
    plug.offline = True
    web.app.dependency_overrides[web.connect] = lambda: lambda ip: TasmotaPlug(ip, timeout_s=0.3)
    page = c.get("/setup").text
    assert "Not answering" in page and ">Switch off</button>" not in page
    assert "no reading now" in page


def test_the_saved_card_lists_the_plugs_timers(live):
    c, db, _, _ = live
    store.save_card(db, site_id=1, mac="AA", now=NOW, card=Card("media", why={2: "night off"}))
    store.save_timers(db, site_id=1, mac="AA", now=NOW, timers=[
        Timer(2, True, 0, "01:00", 0, "1111111", True, 1, 0)])  # fmt: skip
    store.close_chat(db, chat_id=store.open_chat(db, site_id=1, mac="AA", kind="setup", now=NOW),
                     status="closed")  # fmt: skip
    page = c.get("/setup/Plug-1").text
    assert "Timers on the plug:<br>Timer 2: Off at 01:00, every day (night off)" in page


def test_before_the_logger_starts_the_devices_page_shows_no_reading(client, tmp_path):  # noqa: F811
    c, _, _ = client
    cfg = web.app.dependency_overrides[web.settings]()
    cfg = replace(cfg, energy_db=tmp_path / "none.db")
    web.app.dependency_overrides[web.settings] = lambda: cfg
    assert "no reading now" in c.get("/setup").text
