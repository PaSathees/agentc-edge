import json
import sqlite3
from datetime import datetime

import pytest

from agentc import calc, cli, evals, jobs, responder, store
from agentc.config import Device
from agentc.llm import Llm
from agentc.options import Money
from agentc.watcher import Event, Run
from tests.fake_model import FakeModel

AA = {"option": "A", "reason": "It stays on all night."}
NONE = {"option": "none", "reason": "Desk 2 may run until 02:00 on work nights."}


class Script:
    """A fake model: these answers in turn, again from the start for each new try."""

    def __init__(self, *answers):
        self.answers, self.n = [json.dumps(a) if isinstance(a, dict) else a for a in answers], 0

    def __call__(self, *, tier, system, prompt, schema):
        self.n += 1
        return self.answers[(self.n - 1) % len(self.answers)], 1000, 100


# the live cases: Desk 2's real readings


def test_the_case_is_desk_2s_first_wasted_night():
    facts = responder.describe(evals.case(Device("Desk 2")), tz=evals.TZ)
    assert "on at up to 37 W from 01:37 to 06:29 on Mon 02 Nov." in facts
    assert "Over the last 13 days it was never in use between 00:52 and 07:45." in facts
    assert "\nA: off at 01:30, on at 07:00, every day." in facts


def test_a_model_that_answers_well_passes(tmp_path):
    def well(*, tier, system, prompt, schema):  # the fix, unless a rule says no
        return json.dumps(NONE if "until 02:00" in prompt else AA), 1000, 100

    scores = evals.run(well, samples=2, path=tmp_path / "r.json")
    assert [s.line() for s in scores] == [
        "EV-01: right 2/2, checks 2/2, format 2/2, picked A 2, USD 0.0003: PASS",
        "EV-03: right 2/2, checks 2/2, format 2/2, picked none 2, USD 0.0003: PASS",
    ]


def test_one_wrong_choice_in_ten_still_passes_two_do_not():
    right, wrong = (
        evals.Sample([], True, True, True, "A", 0),
        evals.Sample([], False, True, True, "B", 0),
    )
    assert evals.Score("EV-01", [right] * 9 + [wrong]).passed
    assert not evals.Score("EV-01", [right] * 8 + [wrong] * 2).passed


def test_a_number_not_in_the_facts_fails_the_checks(tmp_path):  # every try is blocked
    bad = {"option": "A", "reason": "It saves LKR 40 a week."}
    (ev01, _) = evals.run(Script(bad), samples=1, path=tmp_path / "r.json")
    assert ev01.line() == "EV-01: right 0/1, checks 0/1, format 1/1, picked A 1, USD 0.0003: FAIL"


def test_an_answer_that_is_not_json_fails_the_format(tmp_path):
    (ev01, _) = evals.run(Script("Off at 01:30.", AA), samples=1, path=tmp_path / "r.json")
    assert ev01.line() == "EV-01: right 1/1, checks 1/1, format 0/1, picked A 1, USD 0.0003: FAIL"


def test_no_action_must_name_the_rule(tmp_path):  # EV-03: "none" alone is not enough
    vague = {"option": "none", "reason": "No change is needed."}
    (_, ev03) = evals.run(Script(vague), samples=1, path=tmp_path / "r.json")
    assert ev03.line().startswith("EV-03: right 0/1,")


# recorded, then replayed for free


def test_live_answers_are_recorded_and_replayed_at_no_cost(tmp_path):
    path = tmp_path / "r.json"
    live = evals.run(FakeModel(), samples=2, path=path)
    saved = json.loads(path.read_text())
    assert saved["EV-01"]["model"] == "claude-haiku-5-5"
    assert len(saved["EV-01"]["answers"]) == 2
    again = evals.run(None, path=path)
    assert [s.line().replace("0.0003", "0.0000") for s in live] == [s.line() for s in again]


@pytest.mark.parametrize(
    ("what", "change"),
    [
        ("SYSTEM", lambda: responder.SYSTEM + " Be brief."),
        ("schema", lambda: lambda c: {**responder.Answer.model_json_schema(), "title": "New"}),
    ],
)
def test_a_change_to_what_the_model_reads_makes_the_recording_stale(tmp_path, monkeypatch,
                                                                    what, change):  # fmt: skip
    path = tmp_path / "r.json"
    evals.run(FakeModel(), samples=1, path=path)
    monkeypatch.setattr(responder, what, change())
    with pytest.raises(evals.Stale, match="EV-01: the prompt changed"):
        evals.run(None, path=path)


# from the command line


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    """The evals record to a temporary file, never to evals/recorded.json."""
    path = tmp_path / "recorded.json"
    monkeypatch.setattr(evals, "RECORDED", path)
    return path


def agentc(capsys, *args, send=None):
    code = cli.main(list(args), send=send)  # eval needs no config.toml
    return code, capsys.readouterr().out


def test_eval_live_records_then_eval_replays(recorded, capsys):
    code, out = agentc(capsys, "eval", "--live", "--samples", "1", send=FakeModel())
    assert (code, out.splitlines()[1]) == (1, "EV-03: right 0/1, checks 1/1, format 1/1,"
                                              " picked A 1, USD 0.0001: FAIL")  # fmt: skip
    assert recorded.exists()
    assert agentc(capsys, "eval")[1].endswith("USD 0.0000: FAIL\n")


def test_eval_live_needs_a_key(recorded, capsys):
    assert agentc(capsys, "eval", "--live") == (1, cli.NO_KEY + "\n")


def test_a_stale_recording_is_reported(recorded, capsys, monkeypatch):
    agentc(capsys, "eval", "--live", "--samples", "1", send=FakeModel())
    monkeypatch.setattr(responder, "SYSTEM", responder.SYSTEM + " Be brief.")
    assert agentc(capsys, "eval") == (
        1,
        "EV-01: the prompt changed. Run `agentc eval --live` again.\n",
    )


# code-only cases on the same real readings


def test_late_use_gets_no_fix_so_the_model_is_not_asked():  # EV-03 (a)
    cfg, rs = evals.settings(), evals.readings()
    at = lambda h, m: datetime(2026, 11, 11, h, m, tzinfo=evals.NOW.tzinfo)  # noqa: E731
    late = Run(at(0, 0), at(0, 42), 29.0)  # in use until 00:42, as the Watcher saw it
    money = Money(cfg.tariff, cfg.month_kwh, cfg.levy, cfg.factor)
    case = responder.gather(late, mac="desk2", device=Device("Desk 2"), readings=rs, usual={},
                            timers=[], money=money, rejected=[], tz=cfg.tz, days=13)  # fmt: skip
    assert case is None


def test_two_events_in_one_pass_get_one_result_each(tmp_path):  # EV-06
    energy = tmp_path / "energy.db"
    with sqlite3.connect(energy) as db:
        db.execute("CREATE TABLE readings (ts_utc, plug, power_w, error)")
        db.executemany("INSERT INTO readings VALUES (?, 'Plug-6', ?, NULL)",
                       [(t.isoformat(), w) for t, w in evals.readings()])  # fmt: skip
    db.close()
    app = store.connect(tmp_path / "app.db")
    store.add_site(app, name="home", tz=evals.TZ)
    for name, mac in (("Plug-6", "DD"), ("Plug-5", "TV")):
        store.plug_seen(app, site_id=1, mac=mac, name=name, ip="192.0.2.1", timers_on=True,
                        now=evals.NOW)  # fmt: skip
    store.save_profile(app, site_id=1, mac="DD",
                       profile=calc.build_profile(evals.readings(), tz=evals.TZ) or [])  # fmt: skip
    night = evals.case(Device("Desk 2")).run.start
    store.add_event(app, site_id=1, event=Event("offline", "TV", night, {}), dedupe="tv")
    store.add_event(app, site_id=1, event=Event("unusual_use", "DD", night, {}), dedupe="desk")
    cfg = evals.settings()
    llm = Llm(app, 1, cfg.tz, cfg.tiers, cfg.budget, FakeModel(), clock=lambda: evals.NOW)
    made = jobs.respond_pass(app, store.open_readings(energy), jobs.Ask(cfg, llm), site_id=1,
                             now=evals.NOW)  # fmt: skip
    assert [p.plug_mac for p in made] == ["DD"]
    assert app.execute("SELECT event_id FROM proposals").fetchall() == [(2,)]
    assert app.execute("SELECT kind, status FROM events ORDER BY id").fetchall() == [
        ("offline", "new"), ("unusual_use", "done")
    ]  # fmt: skip


def test_the_recorded_live_answers_still_pass():  # free on every commit
    assert [s.passed for s in evals.run(None)] == [True, True]
