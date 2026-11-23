"""Messages to the phone: signed buttons, the night message, the summary, ntfy (3d)."""

import json
import threading
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agentc import notify
from agentc.config import Owner
from agentc.notify import Button, Message
from agentc.owner import Choice, Decision, Question

HUB, KEY = "https://hub.example", "s3cret"
ASK = Decision("ask", "Desk 2 is still on at 5 W. Switch it off?",
               (Choice("Switch off", "switch_off_now"), Choice("Normal", "save_habit"),
                Choice("Later", "remind_later")))  # fmt: skip
DUE = datetime(2026, 11, 2, 1, 30, tzinfo=UTC)  # 07:00 in Colombo


def q(qid=1, decision=ASK):
    return Question(qid, 1, "AA", decision, "open", DUE)


def test_a_token_is_signed_by_the_secret():
    t = notify.token(KEY, 1, 2)
    assert len(t) == 20 and notify.valid(KEY, t, 1, 2)
    assert not notify.valid(KEY, t, 1, 3) and not notify.valid("other", t, 1, 2)


def test_a_question_becomes_one_message_with_its_choices_as_buttons():
    m = notify.single(q(), "Desk 2", hub=HUB, secret=KEY)
    assert (m.title, m.text, m.quiet) == ("Desk 2", ASK.text, False)
    assert [b.label for b in m.buttons] == ["Switch off", "Normal", "Later"]
    assert m.buttons[1].url == f"{HUB}/answer/1/2/{notify.token(KEY, 1, 2)}"


def test_the_night_is_one_message_with_one_tap_for_all():  # FM-07
    m = notify.night([(q(1), "Desk 1", 12.4), (q(2), "Desk 2", 5.0)], day=date(2026, 11, 2),
                     hub=HUB, secret=KEY)  # fmt: skip
    assert m.title == "2 devices stayed on last night"
    assert m.text == "Desk 1: about 12 W now\nDesk 2: about 5 W now"
    assert [b.label for b in m.buttons] == ["Switch all off", "All normal", "Open inbox"]
    tok = notify.token(KEY, date(2026, 11, 2), "save_habit")
    assert m.buttons[1].url == f"{HUB}/night/2026-11-02/save_habit/{tok}"
    assert m.buttons[2] == Button("Open inbox", f"{HUB}/", post=False)


def test_a_night_button_is_offered_only_if_some_question_has_it():
    later = Decision("ask", "Off?", (Choice("Later", "remind_later"), Choice("No", "nothing")))
    m = notify.night([(q(1, later), "Desk 1", 3.0), (q(2, later), "Desk 2", 4.0)],
                     day=date(2026, 11, 2), hub=HUB, secret=KEY)  # fmt: skip
    assert [b.label for b in m.buttons] == ["Open inbox"]


@pytest.mark.parametrize(
    ("notes", "title"), [(["a"], "Today: 1 note"), (["a", "b"], "Today: 2 notes")]
)
def test_the_summary_is_quiet(notes, title):
    m = notify.summary(notes)
    assert (m.title, m.quiet) == (title, True) and m.text.startswith("- a")


# ntfy over HTTP: a stand-in server on this machine


class Ntfy(BaseHTTPRequestHandler):
    got: list[dict] = []

    def do_POST(self):  # noqa: N802 (the http.server name)
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Ntfy.got.append(json.loads(body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format, *args):  # noqa: A002 (the base name) quiet
        pass


@pytest.fixture
def server():
    Ntfy.got = []
    s = HTTPServer(("127.0.0.1", 0), Ntfy)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}"
    s.shutdown()


def test_ntfy_gets_the_message_as_json(server):
    publish = notify.ntfy(Owner(HUB, "agentc", server))
    publish(
        Message("Desk 2", "Off?", (Button("Off", f"{HUB}/x"), Button("Inbox", HUB, post=False)))
    )
    publish(Message("Today: 1 note", "- a", quiet=True))
    first, second = Ntfy.got
    assert first == {
        "topic": "agentc", "title": "Desk 2", "message": "Off?", "priority": 3,
        "actions": [
            {"action": "http", "label": "Off", "url": f"{HUB}/x", "method": "POST", "clear": True},
            {"action": "view", "label": "Inbox", "url": HUB},
        ],
    }  # fmt: skip
    assert second["priority"] == 2 and second["actions"] == []


def test_ntfy_out_of_reach_is_not_sent():
    publish = notify.ntfy(Owner(HUB, "agentc", "http://127.0.0.1:9"), timeout_s=1)
    with pytest.raises(notify.NotSent, match="ntfy at http://127.0.0.1:9"):
        publish(Message("t", "x"))
