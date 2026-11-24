"""Who may use the web pages, and on which addresses (Phase 4, web/login.py)."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from agentc import app as web
from agentc import notify
from agentc.web import login
from tests.test_app import KEY, NOW, client  # noqa: F401 (the fixture)

PASSWORD = "correct horse"
ME = "me@example.com"


@pytest.fixture
def locked(client, monkeypatch):  # noqa: F811 (the fixture)
    """The app with a password and a Tailscale user set; sleeps skipped."""
    c, db, said = client
    monkeypatch.setattr(login, "LAST_WRONG", [0.0])  # no wrong guess from an earlier test
    web.app.dependency_overrides[login.login] = lambda: login.Login(PASSWORD, frozenset({ME}))
    return c, db, said


def sign_in(c, password=PASSWORD, next="/"):  # noqa: A002
    return c.post("/login", data={"password": password, "next": next}, follow_redirects=False)


def test_with_no_login_set_the_pages_stay_open(client):  # noqa: F811
    c, _, _ = client
    assert c.get("/").status_code == 200 and c.get("/setup/Plug-1").status_code == 200


def test_a_page_sends_a_stranger_to_sign_in_and_back(locked):
    c, _, _ = locked
    r = c.get("/setup/Plug-1", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/login?next=/setup/Plug-1")
    assert c.get("/", follow_redirects=False).headers["location"] == "/login?next=/"


def test_a_form_or_stream_gets_401_not_a_login_page(locked):
    c, _, _ = locked
    tok = notify.token(KEY, "setup", "Plug-1")
    assert c.post("/setup/Plug-1/say", data={"text": "hi", "tok": tok}).status_code == 401
    assert c.get("/setup/Plug-1", headers={"HX-Request": "true"}).status_code == 401


def test_the_right_password_signs_in_for_30_days(locked):
    c, _, _ = locked
    r = sign_in(c, next="/setup")
    assert (r.status_code, r.headers["location"]) == (303, "/setup")
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=2592000" in cookie
    assert c.get("/setup").status_code == 200


def test_one_wrong_guess_a_second_for_the_whole_hub(locked, monkeypatch):
    c, _, _ = locked
    clock = [100.0]
    monkeypatch.setattr(login.time, "monotonic", lambda: clock[0])
    r = sign_in(c, "guess")
    assert r.status_code == 401 and "That is not the password." in r.text
    clock[0] += 0.5
    r = sign_in(c)  # even the right one waits, so guessing in parallel gains nothing
    assert r.status_code == 429 and "Wait a second, then try again." in r.text
    clock[0] += 0.6
    assert sign_in(c).status_code == 303
    assert c.get("/", follow_redirects=False).status_code == 200


def test_a_new_password_signs_everyone_out(locked):
    c, _, _ = locked
    sign_in(c)
    web.app.dependency_overrides[login.login] = lambda: login.Login("new one", frozenset({ME}))
    assert c.get("/", follow_redirects=False).status_code == 303


def test_a_login_ends_after_30_days(locked):
    c, _, _ = locked
    sign_in(c)
    later = NOW + timedelta(days=login.DAYS, seconds=1)
    web.app.dependency_overrides[web.clock] = lambda: later
    assert c.get("/", follow_redirects=False).status_code == 303


@pytest.mark.parametrize(
    "cookie", ["", "x.y", "9999999999.forged", f"1.{notify.token(KEY, 'login', 1)}"]
)
def test_a_made_up_or_old_cookie_does_not_sign_in(locked, cookie):
    c, _, _ = locked
    c.cookies.set(login.COOKIE, cookie)
    assert c.get("/", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("next", ["//evil.example", "https://evil.example", "/\\evil", "setup"])
def test_after_signing_in_it_goes_back_only_to_a_page_on_the_hub(locked, next):  # noqa: A002
    c, _, _ = locked
    assert sign_in(c, next=next).headers["location"] == "/"


def test_the_login_page_says_how_to_get_in(locked):
    c, _, _ = locked
    assert 'name="password"' in c.get("/login?next=/setup").text
    assert 'value="/setup"' in c.get("/login?next=/setup").text
    web.app.dependency_overrides[login.login] = lambda: login.Login("", frozenset({ME}))
    page = c.get("/login").text
    assert 'name="password"' not in page and "AGENTC_TAILSCALE_USER" in page
    assert sign_in(c, "anything").status_code == 401  # no password set: none works


def hub(c, user):
    """A request as Tailscale Serve makes it: from the hub itself, with the user's login."""
    return TestClient(web.app, client=("127.0.0.1", 50000)).get(
        "/", headers={"Tailscale-User-Login": user}, follow_redirects=False
    )


def test_tailscale_lets_in_the_named_user_through_serve_only(locked):
    c, _, _ = locked
    assert hub(c, ME).status_code == 200
    assert hub(c, ME.upper()).status_code == 200  # logins are not case sensitive
    assert hub(c, "someone@else.com").status_code == 303
    faked = c.get("/", headers={"Tailscale-User-Login": ME}, follow_redirects=False)
    assert faked.status_code == 303  # the same header from the home network counts for nothing


def test_the_phone_buttons_need_no_login(locked):
    c, _, said = locked
    assert c.post(f"/ping/{notify.token(KEY, 'ping')}").status_code == 200


def test_the_ways_in_come_from_env(monkeypatch):
    monkeypatch.setenv("AGENTC_PASSWORD", "pw")
    monkeypatch.setenv("AGENTC_TAILSCALE_USER", " Me@Example.com , you@example.com,")
    assert login.login() == login.Login("pw", frozenset({ME, "you@example.com"}))
    monkeypatch.delenv("AGENTC_PASSWORD")
    monkeypatch.delenv("AGENTC_TAILSCALE_USER")
    assert not login.login().set


# the host check


OURS = ["192.168.1.134:8765", "100.99.216.20", "[::1]:8765", "localhost:8765",
        "edge-hub.tail1.ts.net", "Hub.Local", "hub.local."]  # fmt: skip


@pytest.mark.parametrize("host", OURS)
def test_the_hub_answers_its_own_names_and_addresses(monkeypatch, host):
    monkeypatch.setenv("AGENTC_HUB", "https://edge-hub.tail1.ts.net")
    monkeypatch.setenv("AGENTC_HOSTS", "hub.local, other.lan")
    assert login.allowed_host(host)


@pytest.mark.parametrize("host", ["evil.example", "edge-hub.tail1.ts.net.evil.example", ""])
def test_any_other_name_is_refused(monkeypatch, host):
    monkeypatch.setenv("AGENTC_HUB", "https://edge-hub.tail1.ts.net")
    monkeypatch.delenv("AGENTC_HOSTS", raising=False)
    assert not login.allowed_host(host)


def test_a_request_for_another_name_never_reaches_a_page():
    r = TestClient(web.app).get("/health", headers={"Host": "evil.example"})
    assert (r.status_code, r.text) == (400, "Unknown host name. Add it to AGENTC_HOSTS in .env.")
    assert TestClient(web.app).get("/health").status_code == 200
