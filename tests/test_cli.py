import ipaddress
import sqlite3
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from agentc import cli, config, jobs, notify, owner, store, watcher, words
from agentc.devices import tasmota
from agentc.owner import Decision
from agentc.policy import Policy
from agentc.watcher import Event
from tests.desk import START, desk, night
from tests.fake_tasmota import FakePlug

COLOMBO = ZoneInfo("Asia/Colombo")
AT = datetime(2026, 11, 3, 1, 0, tzinfo=COLOMBO)  # Tuesday 01:00 local
EXAMPLE = Path(__file__).parent.parent / "config.example.toml"


@pytest.fixture
def plugs():
    """Two running fake plugs, with clocks set to now."""
    now = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    ps = {"Plug-1": FakePlug(mac="AA:00:00:00:00:01"), "Plug-2": FakePlug(mac="AA:00:00:00:00:02")}
    for p in ps.values():
        p.utc = now
        p.start()
    yield ps
    for p in ps.values():
        p.stop()


@pytest.fixture
def cfg(tmp_path, plugs):
    """The example config, with its two plugs pointed at the fakes."""
    text = EXAMPLE.read_text()
    text = text.replace("192.0.2.11", plugs["Plug-1"].ip).replace("192.0.2.12", plugs["Plug-2"].ip)
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def run(cfg, capsys, *args, step=None, publish=None):
    code = cli.main(["--config", str(cfg), *args], step=step, publish=publish)
    return code, capsys.readouterr().out


def test_timers_shows_each_plug_in_plain_words(cfg, plugs, capsys):
    plugs["Plug-2"].timers[1].update(Enable=1, Time="07:30", Days="0111110", Repeat=1, Action=1)
    code, out = run(cfg, capsys, "timers")
    assert code == 0
    assert "Plug-1\n  No timers set\n" in out
    assert "Plug-2\n  Timer1: On at 07:30, Monday to Friday\n" in out
    assert "Needs you" not in out


def test_problems_are_listed_at_the_end(cfg, plugs, capsys):
    plugs["Plug-1"].offline = True
    _, out = run(cfg, capsys, "timers")
    assert out.startswith("Plug-1\n  Did not answer\nPlug-2\n")
    assert out.endswith("Needs you:\n  - Plug-1 did not answer; retried next pass\n")


def test_timers_only_reads_the_plugs(cfg, plugs, capsys):
    run(cfg, capsys, "timers")
    for p in plugs.values():
        reads = {"Status 5", "Timers", "Status 7", "PulseTime1", "Rule1", "Rule2", "Rule3"}
        assert set(p.commands) <= reads  # each asked with no value: nothing is written


def test_the_site_is_added_once(cfg, capsys, tmp_path):
    run(cfg, capsys, "timers")
    run(cfg, capsys, "timers")
    db = store.connect(tmp_path / "app.db")
    assert db.execute("SELECT name, tz FROM sites").fetchall() == [("home", "Asia/Colombo")]


def write_energy_db(path, plug, days, watts=None, end=None):
    """A minute of readings for `days` days up to `end` (now if not given).

    watts(utc_time) gives the power; by default 10 W at 01:00 local, else 1 W.
    The plug's meter (total_kwh) goes up 0.001 kWh each minute.
    """
    end = end or datetime.now(UTC).replace(second=0, microsecond=0)
    rows = []
    for n, m in enumerate(range(days * 24 * 60, 0, -1)):
        ts = end - timedelta(minutes=m)
        w = watts(ts) if watts else 10.0 if ts.astimezone(COLOMBO).hour == 1 else 1.0
        rows.append((ts.isoformat(), plug, w, n * 0.001, None))
    db = sqlite3.connect(path)
    with db:
        db.execute("CREATE TABLE readings (ts_utc, plug, power_w, total_kwh, error)")
        db.executemany("INSERT INTO readings VALUES (?, ?, ?, ?, ?)", rows)
    db.close()


def test_profile_prints_a_week_table_and_saves_it(cfg, capsys, tmp_path):
    run(cfg, capsys, "timers")  # the plug must be known first
    write_energy_db(tmp_path / "energy.db", "Plug-2", days=8)
    code, out = run(cfg, capsys, "profile", "Plug-2")
    assert code == 0
    assert "Hour     Mon    Tue    Wed    Thu    Fri    Sat    Sun\n" in out
    assert "01:00" + "   10.0" * 7 + "\n" in out
    assert "02:00" + "    1.0" * 7 + "\n" in out
    assert out.endswith("Plug-2 used 11.52 kWh in the last 28 days.\n")  # 11,519 steps of 0.001
    db = store.connect(tmp_path / "app.db")
    assert len(store.load_profile(db, mac="AA:00:00:00:00:02")) == 168


def test_profile_needs_seven_days_of_data(cfg, capsys, tmp_path):
    run(cfg, capsys, "timers")
    write_energy_db(tmp_path / "energy.db", "Plug-2", days=5)  # touches 6 dates, not 7
    code, out = run(cfg, capsys, "profile", "Plug-2")
    assert (code, out) == (1, "Plug-2: not enough data yet. A profile needs 7 days.\n")


def test_profile_of_a_plug_never_reached(cfg, capsys):
    code, out = run(cfg, capsys, "profile", "Plug-2")
    assert (code, out) == (1, "Plug-2 is not known yet. Run `agentc timers` first.\n")


# watch and replay


def spike_days(tmp_path, days=8):
    """`days` of Plug-2 at 1 W, except 30 W from 3 h to 1 h before now."""
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    start, stop = end - timedelta(hours=3), end - timedelta(hours=1)
    watts = lambda ts: 30.0 if start <= ts < stop else 1.0  # noqa: E731
    write_energy_db(tmp_path / "energy.db", "Plug-2", days, watts=watts, end=end)


def test_watch_shows_new_events_once(cfg, capsys, tmp_path):
    run(cfg, capsys, "timers")
    spike_days(tmp_path)
    run(cfg, capsys, "profile", "Plug-2")
    code, out = run(cfg, capsys, "watch")
    assert code == 0
    assert "3D printer: on at an unusual time from " in out and ", 119 min, peak 30 W\n" in out
    again = run(cfg, capsys, "watch")[1]  # 15 min: not yet
    assert again == f"Nothing new.\n{cli.NO_KEY}\n{cli.NO_HUB}\n"


def test_watch_flags_parked_events(cfg, capsys, tmp_path):
    run(cfg, capsys, "timers")
    spike_days(tmp_path)
    db = store.connect(tmp_path / "app.db")
    store.add_event(db, site_id=1, event=Event("unusual_use", None, AT, {}), dedupe="x")
    for _ in range(3):
        store.event_failed(db, event_id=1, limit=3)
    out = run(cfg, capsys, "watch")[1]
    assert "1 event parked after 3 failed tries: see the audit log\n" in out


def test_house_shows_each_card_without_reading_the_plugs(cfg, capsys, plugs):
    run(cfg, capsys, "timers")
    plugs["Plug-1"].offline = True  # house reads only what is saved
    code, out = run(cfg, capsys, "house")
    assert code == 0
    assert out.startswith("Desk 2: nothing known yet. Ask the owner what is plugged in.\n")
    assert "\n\n3D printer: nothing known yet." in out


def test_house_sets_a_card_and_audits_it(cfg, capsys, tmp_path):
    run(cfg, capsys, "timers")
    code, out = run(cfg, capsys, "house", "Plug-1", "kind=computer", "room=study", "what=monitor")
    assert (code, out.splitlines()[0]) == (
        0,
        "Desk 2: computer. Where: study. Plugged in: monitor.",
    )
    assert run(cfg, capsys, "house", "Plug-1")[1].startswith("Desk 2: computer. Where: study.")
    db = store.connect(tmp_path / "app.db")
    assert db.execute("SELECT actor, action FROM audit").fetchall() == [("owner", "card_set")]


def test_house_refuses_a_wrong_change(cfg, capsys):
    run(cfg, capsys, "timers")
    assert run(cfg, capsys, "house", "Plug-1", "kind=telly")[0] == 1


def test_house_needs_the_plug_seen_first(cfg, capsys):
    assert run(cfg, capsys, "house", "Plug-1", "kind=fan") == (
        1,
        "Plug-1: not seen yet. Run `agentc timers` first.\n",
    )


def test_house_of_an_unknown_plug(cfg, capsys):
    assert run(cfg, capsys, "house", "Plug-9") == (1, "Plug-9: not in config.toml.\n")


def test_replay_lists_the_events_and_leaves_app_db_alone(cfg, capsys, tmp_path):
    run(cfg, capsys, "timers")
    spike_days(tmp_path)
    code, out = run(cfg, capsys, "replay", "--days", "8")
    assert code == 0
    assert out.startswith("1. 3D printer: on at an unusual time from ")  # the device's name
    assert out.endswith("Signals in 8 days: 1\n")
    assert store.load_events(store.connect(tmp_path / "app.db"), site_id=1) == []


@pytest.mark.parametrize(
    ("event", "said"),
    [
        (Event("offline", "AA", AT, {"plug": "Plug-6", "rounds": 3}),
         "Plug-6: not answering since Tue 03 Nov 01:00"),
        (Event("no_contact", None, AT, {"plugs": 10}),
         "Lost contact with all 10 plugs at Tue 03 Nov 01:00: "
         "a power cut, or the hub's Wi-Fi is down"),
        (Event("logger_stopped", None, AT, {"minutes": 12}),
         "No readings since Tue 03 Nov 01:00: the logger has stopped"),
        (Event("left_on", "AA", AT, {"minutes": 45, "peak_w": 5.0}),
         "Plug-6: left on since Tue 03 Nov 01:00, 45 min so far, about 5 W"),
        (Event("automation_drift", "AA", AT, {"slot": 1, "time": "06:00", "hours": 3,
                                              "peak_w": 0.5}),
         "Plug-6: timer 1 switched it on at 06:00 on Tue 03 Nov, but it did nothing for 3 h"),
        (Event("override", "AA", AT, {"slot": 2, "time": "01:00", "peak_w": 60.0}),
         "Plug-6: timer 2 should have switched it off at 01:00, but it still drew 60 W"),
        (Event("change_point", "AA", AT, {"old_w": 25.0, "new_w": 60.0}),
         "Plug-6: new device? Its power when on went from about 25 W to 60 W"),
        (Event("timer_mismatch", "AA", AT, {"slots": "1, 9"}),
         "Plug-6: timers changed on the plug (slots 1, 9)"),
    ],
)  # fmt: skip
def test_event_words(event, said):
    assert words.event_words(event, {"AA": "Plug-6"}, tz="Asia/Colombo") == said


# the housemate from the command line (3c2)


def desk_till_yesterday(tmp_path):
    """14 days of Desk 2 on Plug-1, moved by whole days to end 1 to 2 days ago, at 05:59.

    Left on at 5 W on the last night. Returns that night's run as the Watcher saved it.
    """
    shift = timedelta(days=(datetime.now(COLOMBO) - (START + timedelta(days=15))).days)
    rows = [((at + shift).isoformat(), "Plug-1", w, 0.0, None) for at, w in desk(waste=(13,))]
    db = sqlite3.connect(tmp_path / "energy.db")
    with db:
        db.execute("CREATE TABLE readings (ts_utc, plug, power_w, total_kwh, error)")
        db.executemany("INSERT INTO readings VALUES (?, ?, ?, ?, ?)", rows)
    db.close()
    return night(13).start + shift


def asked(cfg, capsys, tmp_path):
    """Desk 2's last night saved as a left-on signal, then one watch: the housemate asks."""
    run(cfg, capsys, "timers")
    start = desk_till_yesterday(tmp_path)
    app = store.connect(tmp_path / "app.db")
    left_on = Event("left_on", "AA:00:00:00:00:01", start, {"minutes": 40, "peak_w": 30.0})
    store.add_event(
        app, site_id=1, event=left_on, dedupe=watcher.dedupe_key(left_on, tz="Asia/Colombo")
    )
    return run(cfg, capsys, "watch", step=decides("ask"))


def test_watch_shows_what_the_housemate_made_of_a_signal(cfg, capsys, tmp_path):
    code, out = asked(cfg, capsys, tmp_path)
    assert code == 0
    assert "Desk 2: ask: Desk 2 is still on. Switch it off?\n  1. Off\n  2. Normal\n" in out
    assert "  Question 1, reaches you " in out
    assert out.count("Question ") == 1


def test_watch_sends_due_questions_to_the_phone(cfg, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTC_SECRET", "s3cret")
    monkeypatch.setenv("AGENTC_HUB", "http://192.0.2.50")
    sent = []
    run(cfg, capsys, "timers")
    start = desk_till_yesterday(tmp_path)
    app = store.connect(tmp_path / "app.db")
    left_on = Event("left_on", "AA:00:00:00:00:01", start, {"minutes": 40, "peak_w": 30.0})
    store.add_event(app, site_id=1, event=left_on, dedupe="d")
    monkeypatch.setattr(owner, "send_at", lambda local, watts, **kw: local)  # day or night: now
    out = run(cfg, capsys, "watch", step=decides("ask"), publish=sent.append)[1]
    assert cli.NO_HUB not in out
    assert "Sent: Desk 2\n" in out and len(sent) == 1


def test_watch_with_a_key_runs_the_housemate(cfg, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")  # nothing to look at, so no call is made
    run(cfg, capsys, "timers")
    spike_days(tmp_path)
    assert cli.NO_KEY not in run(cfg, capsys, "watch")[1]


def test_questions_lists_the_open_ones(cfg, capsys, tmp_path):
    asked(cfg, capsys, tmp_path)
    code, out = run(cfg, capsys, "questions")
    assert code == 0
    assert out.startswith("Question 1, Desk 2, from ")
    assert out.endswith(":\n  ask: Desk 2 is still on. Switch it off?\n  1. Off\n  2. Normal\n")


def test_no_open_questions(cfg, capsys):
    assert run(cfg, capsys, "questions") == (0, "No open questions.\n")


def test_a_that_is_normal_answer_saves_a_habit(cfg, capsys, tmp_path):
    asked(cfg, capsys, tmp_path)
    code, out = run(cfg, capsys, "answer", "1", "2")
    assert code == 0 and out.startswith("Saved: Desk 2 is normal on ")
    assert out.endswith("s, 01:00 to 07:00.\n")  # the night's weekday, from its hour to 07:00
    assert "Habit: Desk 2 is normal on " in run(cfg, capsys, "house", "Plug-1")[1]
    assert run(cfg, capsys, "questions")[1] == "No open questions.\n"


def test_a_switch_off_answer_is_only_recorded_in_shadow_mode(cfg, capsys, tmp_path):
    asked(cfg, capsys, tmp_path)
    assert run(cfg, capsys, "answer", "1", "1") == (
        0, "Recorded: switch Desk 2 off. Desk 2 is set to advice, so nothing was sent. To let me"
        " change it, set its access to write in config.toml.\n")  # fmt: skip


def test_an_answer_to_no_open_question(cfg, capsys, tmp_path):
    asked(cfg, capsys, tmp_path)
    assert run(cfg, capsys, "answer", "9", "1") == (1, "No open question 9.\n")
    assert run(cfg, capsys, "answer", "1", "3") == (1, "Question 1 has choices 1 to 2.\n")


@pytest.mark.parametrize(
    ("outcome", "said"),
    [
        (jobs.Outcome(1, "Desk 2", None, "habit: Desk 2 is normal on Mondays", looked=False),
         "Desk 2: habit: Desk 2 is normal on Mondays"),
        (jobs.Outcome(1, "Desk 2", Decision("note", "Small."), "held: 5 questions that day"),
         "Desk 2: note: Small.\n  (held: 5 questions that day)"),
        (jobs.Outcome(1, "Desk 2", Decision("ignore", "Normal."), ""), "Desk 2: ignore: Normal."),
    ],
)  # fmt: skip
def test_outcome_words(outcome, said):
    assert cli.outcome_words(outcome) == said


def test_replay_can_let_the_housemate_look_too(cfg, capsys, tmp_path):
    run(cfg, capsys, "timers")
    desk_till_yesterday(tmp_path)
    code, out = run(cfg, capsys, "replay", "--look", step=decides("ask"))
    assert code == 0
    assert "No readings since " in out  # the data ends a day ago: the heartbeat sees it
    assert "Desk 2: ask: Desk 2 is still on. Switch it off?" in out
    assert out.endswith("Looked at 1, questions 1. Model calls cost USD 0.0001.\n")
    assert store.questions(store.connect(tmp_path / "app.db"), site_id=1) == []  # a copy


def test_replay_needs_a_key_to_let_the_housemate_look(cfg, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert run(cfg, capsys, "replay", "--look") == (1, cli.NO_KEY + "\n")


# thresholds


def test_policy_shows_each_value_where_it_came_from_and_why(cfg, capsys):
    code, out = run(cfg, capsys, "policy", "Plug-1")
    assert code == 0
    assert out.startswith("Plug-1: Desk 2\n")
    assert (
        "  night_wake_w        50  default At night, ask at once only if the device draws more"
        " than this\n" in out
    )
    assert len(out.splitlines()) == 1 + len(fields(Policy))  # one line a value


def test_policy_of_the_whole_site(cfg, capsys):
    assert run(cfg, capsys, "policy")[1].startswith(
        "Site: home\n  factor               2  default "
    )


def test_policy_of_an_unknown_plug(cfg, capsys):
    assert run(cfg, capsys, "policy", "Plug-9") == (1, "Plug-9 is not in Plug-1, Plug-2.\n")


def with_policy(cfg, text):
    cfg.write_text(cfg.read_text() + f"\n[policy]\n{text}\n")


def test_profile_uses_the_plugs_policy(cfg, capsys, tmp_path):
    run(cfg, capsys, "timers")
    write_energy_db(tmp_path / "energy.db", "Plug-2", days=7)  # touches 8 dates
    with_policy(cfg, "min_days = 9")
    code, out = run(cfg, capsys, "profile", "Plug-2")
    assert (code, out) == (1, "Plug-2: not enough data yet. A profile needs 9 days.\n")


def test_replay_uses_each_plugs_policy(cfg, capsys, tmp_path):
    run(cfg, capsys, "timers")
    spike_days(tmp_path)  # 8 days: touches 9 dates
    with_policy(cfg, "min_days = 10")  # so no profile, and no event
    assert run(cfg, capsys, "replay", "--days", "8")[1] == "Signals in 8 days: 0\n"


# learning (B3b)


def test_learn_says_what_each_plug_learned(cfg, capsys, tmp_path):
    run(cfg, capsys, "timers")
    desk_till_yesterday(tmp_path)  # Plug-1 only
    code, out = run(cfg, capsys, "learn")
    assert (code, out) == (0, "Plug-1: standby 5 W, active 30 W\n"
                              "Plug-2: not enough days of readings yet\n")  # fmt: skip
    policy = run(cfg, capsys, "policy", "Plug-1")[1]
    assert "  small_w            2.5  learned half the standby" in policy


def test_learn_says_when_a_plug_has_one_level(cfg, capsys, tmp_path):
    run(cfg, capsys, "timers")
    write_energy_db(tmp_path / "energy.db", "Plug-2", days=8, watts=lambda _: 5.0)
    assert "Plug-2: one level or none, so it keeps the defaults\n" in run(cfg, capsys, "learn")[1]


# look: the housemate agent on one saved event (3c)


def saved_event(cfg, capsys, tmp_path):
    """Plug-1 (Desk 2) read, 2 days of readings, and one left-on event saved. Returns its id."""
    run(cfg, capsys, "timers")
    write_energy_db(tmp_path / "energy.db", "Plug-1", 2)
    db = store.connect(tmp_path / "app.db")
    mac = store.plug_mac(db, name="Plug-1")
    start = datetime.now(UTC) - timedelta(minutes=40)
    event = Event("left_on", mac, start, {"minutes": 33, "peak_w": 5.0})
    store.add_event(db, site_id=1, event=event, dedupe="e")
    return 1


def decides(action):
    def step(*, tier, system, messages, tools):
        text = "Desk 2 is still on. Switch it off?" if action == "ask" else "Normal."
        pairs = (("Off", "switch_off_now"), ("Normal", "save_habit")) if action == "ask" else ()
        choices = [{"label": a, "does": b, "off": None, "on": None, "days": None} for a, b in pairs]
        args = {"action": action, "text": text, "choices": choices}
        return (
            [{"type": "tool_use", "id": "t1", "name": "decide", "input": args}],
            "tool_use",
            900,
            90,
        )

    return step


def test_look_shows_the_agents_decision(cfg, capsys, tmp_path):
    eid = saved_event(cfg, capsys, tmp_path)
    code, out = run(cfg, capsys, "look", str(eid), step=decides("ask"))
    assert code == 0
    assert out.startswith("Signal: Desk 2: left on since ")
    assert "ask: Desk 2 is still on. Switch it off?\n  1. Off\n  2. Normal\n" in out
    assert out.endswith("Model calls cost USD 0.0001.\n")


def test_look_of_an_unknown_event(cfg, capsys, tmp_path):
    run(cfg, capsys, "timers")
    assert run(cfg, capsys, "look", "9", step=decides("ask")) == (1, "No event 9.\n")


def test_look_needs_a_model(cfg, capsys, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    eid = saved_event(cfg, capsys, tmp_path)
    assert run(cfg, capsys, "look", str(eid)) == (1, cli.NO_KEY + "\n")


def test_look_of_a_whole_site_event(cfg, capsys, tmp_path):
    run(cfg, capsys, "timers")
    db = store.connect(tmp_path / "app.db")
    store.add_event(db, site_id=1, event=Event("no_contact", None, AT, {"plugs": 2}), dedupe="s")
    assert run(cfg, capsys, "look", "1", step=decides("ask"))[1].startswith(
        "Event 1 is about the whole site"
    )


def test_look_uses_the_real_model_when_a_key_is_set(cfg, capsys, tmp_path, monkeypatch):
    eid = saved_event(cfg, capsys, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(cli.llm, "claude_step", lambda client: decides("ignore"))  # no real call
    assert "ignore: Normal.\n" in run(cfg, capsys, "look", str(eid))[1]


def test_look_with_no_decision_says_why(cfg, capsys, tmp_path):
    eid = saved_event(cfg, capsys, tmp_path)

    def mute(**_):
        return [{"type": "text", "text": "Hmm."}], "end_turn", 900, 90

    assert "No decision: format: no decision\n" in run(cfg, capsys, "look", str(eid), step=mute)[1]


def test_look_can_look_as_of_a_past_time(cfg, capsys, tmp_path):
    eid = saved_event(cfg, capsys, tmp_path)
    seen = {}

    def step(**kw):
        seen["brief"] = kw["messages"][0]["content"]
        return decides("ignore")(**kw)

    run(cfg, capsys, "look", str(eid), "--at", "2026-11-02 02:10", step=step)
    assert "Now: Mon 02 Nov 02:10" in seen["brief"]


def test_ping_sends_one_test_message(cfg, capsys, monkeypatch):
    monkeypatch.setenv("AGENTC_SECRET", "s3cret")
    monkeypatch.setenv("AGENTC_HUB", "http://192.0.2.50")
    sent = []
    code, out = run(cfg, capsys, "ping", publish=sent.append)
    topic = notify.topic(config.Owner("http://192.0.2.50"), "s3cret")
    assert topic.startswith("agentc-") and len(topic) == 19  # hard to guess, not "agentc"
    assert code == 0 and out.startswith(
        f"In the ntfy app: add server http://192.0.2.50:8090, subscribe to {topic}\n"
        "Inbox: http://192.0.2.50:8765/\nSent at "
    )
    (m,) = sent
    assert m.title == "AgentC test" and [b.label for b in m.buttons] == ["Tap test", "Open inbox"]


def test_ping_without_a_hub_says_why(cfg, capsys, monkeypatch):
    monkeypatch.delenv("AGENTC_HUB", raising=False)
    assert run(cfg, capsys, "ping") == (1, cli.NO_HUB + "\n")


def test_ping_with_ntfy_out_of_reach(cfg, capsys, monkeypatch):
    monkeypatch.setenv("AGENTC_SECRET", "s3cret")
    monkeypatch.setenv("AGENTC_HUB", "http://192.0.2.50")

    def down(m):
        raise notify.NotSent("ntfy at x: refused")

    code, out = run(cfg, capsys, "ping", publish=down)
    assert code == 1 and out.endswith("ntfy at x: refused\n")


# one-command setup (3d2): find, log, run


def test_find_prints_the_plugs_lines_in_address_order(capsys, monkeypatch, tmp_path):
    info = tasmota.Info(mac="AA", hostname="tasmota-1")
    monkeypatch.setattr(tasmota, "find", lambda hosts: [("192.0.2.12", info), ("192.0.2.9", info)])
    monkeypatch.setattr(cli.logger, "read_plug", lambda *a, **kw: ("", "", None, None))  # metered
    code = cli.main(["--config", str(tmp_path / "none.toml"), "find", "192.0.2.0/24"])
    out = capsys.readouterr().out
    assert code == 0 and out.startswith("Asking 254 addresses in 192.0.2.0/24 ...\nFound 2 ")
    assert '"Plug-1" = "192.0.2.9"  # tasmota-1\n"Plug-2" = "192.0.2.12"  # tasmota-1\n' in out
    assert out.endswith(f"AGENTC_HUB=http://{tasmota.this_ip()}\n")


def test_find_skips_known_plugs_and_devices_without_a_meter(cfg, plugs, capsys, monkeypatch):
    extra = FakePlug(mac="AA:00:00:00:00:03")
    extra.start()
    bulb = tasmota.Info(mac="BB", hostname="bulb-4")
    found = [(plugs["Plug-1"].ip, tasmota.Info("AA", "p1")), (extra.ip, tasmota.Info("AC", "p3")),
             ("127.0.0.1:1", bulb)]  # fmt: skip
    monkeypatch.setattr(tasmota, "find", lambda hosts: found)
    try:
        out = run(cfg, capsys, "find", "192.0.2.0/24")[1]
    finally:
        extra.stop()
    assert "Found 3 Tasmota devices, 2 with an energy sensor." in out
    assert f'"Plug-3" = "{extra.ip}"  # p3\n' in out
    assert "Already in config.toml: Plug-1\n" in out
    assert "No energy sensor, so of no use here: 127.0.0.1:1 (bulb-4)\n" in out


def test_find_with_nothing_found(capsys, monkeypatch):
    monkeypatch.setattr(tasmota, "find", lambda hosts: [])
    monkeypatch.setattr(tasmota, "home_network", lambda: ipaddress.IPv4Network("192.0.2.0/30"))
    assert cli.main(["find"]) == 1
    assert "No Tasmota plugs found" in capsys.readouterr().out


def test_log_runs_the_logger_on_the_configs_plugs(cfg, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(cli.logger, "run", lambda db, plugs: seen.update(db=db, plugs=plugs))
    assert cli.main(["--config", str(cfg), "log"]) == 0
    assert seen["db"] == tmp_path / "energy.db" and list(seen["plugs"]) == ["Plug-1", "Plug-2"]


def test_run_starts_the_jobs_loop(cfg, capsys, monkeypatch):
    monkeypatch.setattr(cli, "show_run", lambda db, cfg, **kw: 7)
    assert run(cfg, capsys, "run")[0] == 7


def test_the_jobs_loop_waits_for_the_logger_then_runs_each_minute(cfg, capsys, tmp_path):
    c = config.load(cfg)
    db = store.connect(c.app_db)
    store.add_site(db, name=c.site, tz=c.tz)
    naps = []
    cli.show_run(db, c, site_id=1, model=None, sender=None, rounds=1, sleep=naps.append)
    assert store.last_run(db, site_id=1, job="watch") is None  # no energy.db yet: waits
    write_energy_db(tmp_path / "energy.db", "Plug-1", 1)
    cli.show_run(db, c, site_id=1, model=None, sender=None, rounds=2, sleep=naps.append)
    out = capsys.readouterr().out
    assert store.last_run(db, site_id=1, job="watch") is not None and naps == [60, 60, 60]
    assert out.startswith("Running every 60 s.") and "Nothing new" not in out


def test_a_failed_pass_is_printed_and_the_loop_goes_on(cfg, capsys, tmp_path, monkeypatch):
    c = config.load(cfg)
    db = store.connect(c.app_db)
    store.add_site(db, name=c.site, tz=c.tz)
    write_energy_db(tmp_path / "energy.db", "Plug-1", 1)

    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cli, "show_watch", locked)
    naps = []
    cli.show_run(db, c, site_id=1, model=None, sender=None, rounds=2, sleep=naps.append)
    out = capsys.readouterr().out
    assert out.count("Pass failed, trying again in 60 s: OperationalError: database is locked") == 2
    assert naps == [60, 60]
