"""A tap that switches a plug, its undo, and the shadow mode switch, in the web app (4b)."""

from dataclasses import replace

import pytest

from agentc import app as web
from agentc import config, notify, store
from agentc.config import Device
from agentc.web import login
from tests.test_app import KEY, NOW, client  # noqa: F401 (the fixture)

TAP = f"/answer/1/1/{notify.token(KEY, 1, 1)}"  # "Switch off" on Desk 2's question


@pytest.fixture
def live(client, plug):  # noqa: F811 (the fixture)
    """Desk 2 (Plug-1) on the fake plug, set to write; shadow mode off; a phone to echo to."""
    c, db, said = client
    cfg = web.app.dependency_overrides[web.settings]()
    hub = config.Owner("http://192.0.2.50")
    cfg = replace(cfg, plugs={"Plug-1": plug.ip}, owner=hub,
                  devices={"Plug-1": Device("Desk 2", access="write")})  # fmt: skip
    web.app.dependency_overrides[web.settings] = lambda: cfg
    store.set_shadow(db, site_id=1, on=False, now=NOW)
    plug.relay_on = True
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
