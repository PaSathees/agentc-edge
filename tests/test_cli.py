import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from agentc import cli, store, watcher
from agentc.calc import ProfileHour
from agentc.watcher import Event
from tests.fake_model import FakeModel
from tests.fake_tasmota import FakePlug
from tests.test_options import START, desk, night

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


def run(cfg, capsys, *args, send=None):
    code = cli.main(["--config", str(cfg), *args], send=send)
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
        assert set(p.commands) <= {"Status 5", "Timers", "Status 7"}  # no write commands


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
    assert "Plug-2: on at an unusual time from " in out and ", 119 min, peak 30 W\n" in out
    assert run(cfg, capsys, "watch")[1] == f"Nothing new.\n{cli.NO_KEY}\n"  # 15 min: not yet


def test_replay_lists_the_events_and_leaves_app_db_alone(cfg, capsys, tmp_path):
    run(cfg, capsys, "timers")
    spike_days(tmp_path)
    code, out = run(cfg, capsys, "replay", "--days", "8")
    assert code == 0
    assert out.startswith("1. Plug-2: on at an unusual time from ")
    assert out.endswith("Events in 8 days: 1\n")
    assert store.load_events(store.connect(tmp_path / "app.db"), site_id=1) == []


@pytest.mark.parametrize(
    ("event", "words"),
    [
        (Event("offline", "AA", AT, {"plug": "Plug-6", "rounds": 3}),
         "Plug-6: not answering since Tue 03 Nov 01:00"),
        (Event("power_cut", None, AT, {"plugs": 10}),
         "Power cut at Tue 03 Nov 01:00: all 10 plugs stopped answering"),
        (Event("timer_mismatch", "AA", AT, {"slots": "1, 9"}),
         "Plug-6: timers changed on the plug (slots 1, 9)"),
    ],
)  # fmt: skip
def test_event_words(event, words):
    assert cli.event_words(event, {"AA": "Plug-6"}, tz="Asia/Colombo") == words


# the Responder from the command line


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


def test_watch_shows_new_proposals(cfg, capsys, tmp_path):
    run(cfg, capsys, "timers")
    start = desk_till_yesterday(tmp_path)
    app = store.connect(tmp_path / "app.db")
    mac = "AA:00:00:00:00:01"
    store.save_profile(
        app, site_id=1, mac=mac, profile=[ProfileHour(h, 0.0, 0.0, 3) for h in range(168)]
    )
    left_on = Event("unusual_use", mac, start, {})
    store.add_event(
        app, site_id=1, event=left_on, dedupe=watcher.dedupe_key(left_on, tz="Asia/Colombo")
    )
    code, out = run(cfg, capsys, "watch", send=FakeModel())
    assert code == 0
    assert "Proposal for Desk 2: off at 01:30, on at 07:00, every day. Saves LKR " in out
    assert out.count("Proposal for") == 1
    assert out.endswith(" a week.\n  It stays on all night.\n")


def test_watch_with_a_key_runs_the_responder(cfg, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")  # nothing to answer, so no call is made
    run(cfg, capsys, "timers")
    spike_days(tmp_path)
    assert cli.NO_KEY not in run(cfg, capsys, "watch")[1]


def test_replay_can_ask_the_model_too(cfg, capsys, tmp_path):
    run(cfg, capsys, "timers")
    desk_till_yesterday(tmp_path)
    code, out = run(cfg, capsys, "replay", "--respond", send=FakeModel())
    assert code == 0
    assert "Events in 14 days: 1\nProposal for Desk 2: off at 01:30, on at 07:00, every day." in out
    assert out.endswith("\n  It stays on all night.\nProposals: 1. Model calls cost USD 0.0001.\n")
    assert store.load_proposals(store.connect(tmp_path / "app.db"), site_id=1) == []  # a copy


def test_replay_needs_a_key_to_ask_the_model(cfg, capsys):
    assert run(cfg, capsys, "replay", "--respond") == (1, cli.NO_KEY + "\n")


# thresholds


def test_policy_shows_each_value_where_it_came_from_and_why(cfg, capsys):
    code, out = run(cfg, capsys, "policy", "Plug-1")
    assert code == 0
    assert out.startswith("Plug-1: Desk 2\n")
    assert (
        "  max_off_h           12  default No fix keeps a plug off longer than this many hours\n"
        in out
    )
    assert len(out.splitlines()) == 1 + 16


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
    assert run(cfg, capsys, "replay", "--days", "8")[1] == "Events in 8 days: 0\n"
