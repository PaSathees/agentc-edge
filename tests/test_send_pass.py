"""Sending to the phone (3d): the night in one message, off by morning dropped, retries,
the evening summary, and one tap for the night."""

import sqlite3
from dataclasses import replace
from datetime import UTC, date, timedelta

import pytest

from agentc import jobs, notify, readings, store
from agentc.owner import Choice, Decision
from agentc.watcher import Event
from tests.test_jobs import CFG, MONDAY, app  # noqa: F401 (app is a fixture)

HUB, KEY = "https://hub.example", "s3cret"
MORNING = MONDAY + timedelta(hours=7)
ASK = Decision("ask", "Still on. Switch it off?",
               (Choice("Off", "switch_off_now"), Choice("Normal", "save_habit")))  # fmt: skip


class Phone:
    """A stand-in for ntfy: keeps each message. Fails while `down`."""

    def __init__(self):
        self.got, self.down = [], False

    def __call__(self, m):
        if self.down:
            raise notify.NotSent("no ntfy")
        self.got.append(m)


@pytest.fixture
def phone():
    return Phone()


def sender(phone):  # noqa: F811
    return jobs.Sender(phone, HUB, KEY)


def energy_at(tmp_path, watts):
    """energy.db from 00:00 to 23:59 on Monday: watts[plug] all day."""
    rows = [((MONDAY + timedelta(minutes=m)).astimezone(UTC).isoformat(), plug, w, None)
            for m in range(24 * 60) for plug, w in watts.items()]  # fmt: skip
    path = tmp_path / f"energy{len(list(tmp_path.glob('energy*.db')))}.db"  # a fresh one each call
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE readings (ts_utc, plug, power_w, error)")
        db.executemany("INSERT INTO readings VALUES (?, ?, ?, ?)", rows)
    db.close()
    return readings.connect(path)


def asked(app, mac="AA", due=MORNING, decision=ASK, n=1):  # noqa: F811
    e = Event("left_on", mac, MONDAY + timedelta(hours=1), {"minutes": 40, "peak_w": 5.0})
    store.add_event(app, site_id=1, event=e, dedupe=f"{mac}{n}")
    eid = app.execute("SELECT MAX(id) FROM events").fetchone()[0]
    return store.add_question(app, site_id=1, event_id=eid, mac=mac, decision=decision,
                              send_at=due, now=MONDAY + timedelta(hours=1, minutes=40))  # fmt: skip


def send(app, energy, phone, now=MORNING):  # noqa: F811
    return jobs.send_pass(app, energy, CFG, sender(phone), site_id=1, now=now)


def audit(app):  # noqa: F811
    return [a for (a,) in app.execute("SELECT action FROM audit ORDER BY id")]


# questions due now


def test_the_nights_questions_go_as_one_message(app, tmp_path, phone):  # noqa: F811 (FM-07)
    asked(app, "AA")
    asked(app, "BB", n=2)
    (m,) = send(app, energy_at(tmp_path, {"Plug-6": 5.0, "Plug-2": 12.0}), phone)
    assert m.title == "2 devices stayed on last night"
    assert m.text == "Plug-6: about 5 W now\n3D printer: about 12 W now"
    assert phone.got == [m]
    assert send(app, energy_at(tmp_path, {"Plug-6": 5.0}), phone) == []  # sent once


def test_a_device_off_by_morning_is_not_asked_about(app, tmp_path, phone):  # noqa: F811
    asked(app, "AA")
    assert send(app, energy_at(tmp_path, {"Plug-6": 1.0}), phone) == []
    assert [q.status for q in store.questions(app, site_id=1)] == ["expired"]
    assert audit(app) == ["off_by_morning"]


def test_one_night_question_goes_with_its_own_buttons(app, tmp_path, phone):  # noqa: F811
    asked(app, "AA")
    (m,) = send(app, energy_at(tmp_path, {}), phone)  # no reading: kept, not dropped
    assert (m.title, [b.label for b in m.buttons]) == ("Plug-6", ["Off", "Normal"])


def test_a_day_question_goes_on_its_own(app, tmp_path, phone):  # noqa: F811
    noon = MONDAY + timedelta(hours=12)
    asked(app, "AA", due=noon)
    asked(app, "BB", due=noon, n=2)
    assert [m.title for m in send(app, energy_at(tmp_path, {}), phone, now=noon)] == [
        "Plug-6", "3D printer"]  # fmt: skip


def test_a_question_not_due_yet_waits(app, tmp_path, phone):  # noqa: F811
    asked(app, "AA")
    assert send(app, energy_at(tmp_path, {"Plug-6": 5.0}), phone, now=MORNING - timedelta(1)) == []


def test_if_ntfy_fails_the_question_goes_on_the_next_pass(app, tmp_path, phone):  # noqa: F811
    asked(app, "AA", due=MONDAY + timedelta(hours=12))
    db, noon = energy_at(tmp_path, {}), MONDAY + timedelta(hours=12)
    phone.down = True
    assert send(app, db, phone, now=noon) == [] and audit(app) == ["not_sent"]
    phone.down = False
    assert len(send(app, db, phone, now=noon)) == 1


def test_remind_later_sends_it_again(app, tmp_path, phone):  # noqa: F811
    qid, noon = asked(app, "AA", due=MONDAY + timedelta(hours=12)), MONDAY + timedelta(hours=12)
    db = energy_at(tmp_path, {})
    send(app, db, phone, now=noon)
    assert qid is not None
    store.ask_again(app, question_id=qid, at=noon + timedelta(hours=2))
    assert len(send(app, db, phone, now=noon + timedelta(hours=2))) == 1


# the evening summary


def noted(app, text, at):  # noqa: F811
    store.add_audit(app, site_id=1, at=at, actor="agent", action="note", ref="event:1",
                    detail={"text": text, "why": "", "tools": ""})  # fmt: skip


def due(app, energy, phone, now):  # noqa: F811
    cfg = replace(CFG, plugs={})
    return jobs.run_due(app, energy, cfg, site_id=1, now=now, sender=sender(phone))[2]


def test_the_days_notes_go_at_21_00_once(app, tmp_path, phone):  # noqa: F811
    db = energy_at(tmp_path, {})
    noted(app, "Desk 2 ran late.", MONDAY + timedelta(hours=10))
    noted(app, "Too old.", MONDAY - timedelta(hours=10))
    assert due(app, db, phone, MONDAY + timedelta(hours=20, minutes=55)) == []
    (m,) = due(app, db, phone, MONDAY + timedelta(hours=21))
    assert (m.title, m.text, m.quiet) == ("Today: 1 note", "- Desk 2 ran late.", True)
    assert due(app, db, phone, MONDAY + timedelta(hours=23)) == []


def test_no_notes_no_summary(app, tmp_path, phone):  # noqa: F811
    assert due(app, energy_at(tmp_path, {}), phone, MONDAY + timedelta(hours=21)) == []
    assert store.last_run(app, site_id=1, job="summary") == MONDAY + timedelta(hours=21)


def test_a_summary_ntfy_refused_goes_on_the_next_pass(app, tmp_path, phone):  # noqa: F811
    db = energy_at(tmp_path, {})
    noted(app, "Small.", MONDAY + timedelta(hours=10))
    phone.down = True
    assert due(app, db, phone, MONDAY + timedelta(hours=21)) == []
    phone.down = False
    assert len(due(app, db, phone, MONDAY + timedelta(hours=21, minutes=5))) == 1


def test_a_broken_send_pass_never_stops_the_jobs(app, tmp_path, phone, monkeypatch):  # noqa: F811
    def broken(*args, **kwargs):
        raise RuntimeError("a bug")

    monkeypatch.setattr(jobs, "send_pass", broken)
    due(app, energy_at(tmp_path, {}), phone, MONDAY + timedelta(hours=12))
    assert "send_pass_failed" in audit(app)
    assert store.last_run(app, site_id=1, job="watch") == MONDAY + timedelta(hours=12)


def test_questions_due_in_the_watch_pass_are_sent(app, tmp_path, phone):  # noqa: F811
    asked(app, "AA", due=MONDAY + timedelta(hours=12))
    assert [m.title for m in due(app, energy_at(tmp_path, {}), phone,
                                  MONDAY + timedelta(hours=12))] == ["Plug-6"]  # fmt: skip


# one tap for the night


def tap_night(app, does, night=date(2026, 11, 2)):  # noqa: F811
    return jobs.answer_night(app, CFG, site_id=1, night=night, does=does, now=MORNING)


def test_all_normal_saves_a_habit_for_each_device(app):  # noqa: F811
    asked(app, "AA")
    asked(app, "BB", n=2)
    assert tap_night(app, "save_habit").count("Saved: ") == 2
    assert [q.status for q in store.questions(app, site_id=1)] == ["answered", "answered"]


def test_a_night_tap_skips_questions_without_that_choice_and_other_nights(app):  # noqa: F811
    asked(app, "AA")
    asked(app, "BB", decision=Decision("ask", "Off?", (Choice("Later", "remind_later"),
                                                     Choice("No", "nothing"))), n=2)  # fmt: skip
    asked(app, "AA", due=MORNING + timedelta(days=1), n=3)
    assert tap_night(app, "switch_off_now").startswith("Recorded: switch Plug-6 off.")
    assert [q.status for q in store.questions(app, site_id=1)] == ["answered", "open", "open"]


def test_a_question_answered_at_the_same_moment_does_not_stop_the_rest(app, monkeypatch):  # noqa: F811
    asked(app, "AA")
    asked(app, "BB", n=2)
    real = jobs.answer

    def first_taken(app, cfg, *, question_id, **kw):  # question 1 was tapped in the inbox
        if question_id == 1:
            raise LookupError("No open question 1.")
        return real(app, cfg, question_id=question_id, **kw)

    monkeypatch.setattr(jobs, "answer", first_taken)
    assert tap_night(app, "save_habit").count("Saved: ") == 1


def test_a_night_tap_with_nothing_to_answer(app):  # noqa: F811
    with pytest.raises(LookupError, match="No open questions from the night before 2026-11-02"):
        tap_night(app, "save_habit")


def test_after_one_refusal_the_rest_wait_for_the_next_pass(app, tmp_path, phone):  # noqa: F811
    noon = MONDAY + timedelta(hours=12)
    asked(app, "AA", due=noon)
    asked(app, "BB", due=noon, n=2)
    calls = []

    def flaky(m):
        calls.append(m)
        if len(calls) == 1:
            raise notify.NotSent("busy")

    assert jobs.send_pass(app, energy_at(tmp_path, {}), CFG, jobs.Sender(flaky, HUB, KEY),
                          site_id=1, now=noon) == []  # fmt: skip
    assert len(calls) == 1  # no second try in the same pass


def test_a_question_is_answered_once_in_the_database(app):  # noqa: F811 (two taps at once)
    qid = asked(app, "AA")
    assert qid is not None
    assert store.answer_question(app, question_id=qid, answer=2, now=MORNING) is True
    assert store.answer_question(app, question_id=qid, answer=1, now=MORNING) is False
    assert store.questions(app, site_id=1)[0].answer == 2  # the first tap stands
