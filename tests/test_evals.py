"""The housemate's evals: real nights, recorded live, replayed free on every commit."""

import json

import pytest

from agentc import agent, cli, evals

NONE = {"off": None, "on": None, "days": None}
OFF = [{"label": "Switch off", "does": "switch_off_now", **NONE},
       {"label": "That's normal", "does": "save_habit", **NONE}]  # fmt: skip


def decide(action, choices=()):
    args = {"action": action, "text": "Still on at night." if choices else "Normal.",
            "choices": list(choices)}  # fmt: skip
    return [{"type": "tool_use", "id": "t1", "name": "decide", "input": args}]


class Wise:
    """A stand-in model: asks about Desk 2, ignores the rest. Counts its steps."""

    def __init__(self):
        self.steps = 0

    def __call__(self, *, tier, system, messages, tools):
        self.steps += 1
        desk = messages[0]["content"].startswith("Signal: Desk 2:")
        return (decide("ask", OFF) if desk else decide("ignore")), "tool_use", 1000, 100


def always(blocks):
    def send(*, tier, system, messages, tools):
        return blocks, "tool_use", 1000, 100

    return send


# the cases: real readings, as the house was when the agent looks


def test_ev01_is_desk_2s_first_left_on_night():
    signal, tools = evals.case(evals.LIVE[0], evals.settings())
    assert signal == "Desk 2: left on since Mon 02 Nov 01:37, 33 min so far, about 37 W"
    assert tools.run("house_now", {}) == "Desk 2: 6 W\n3D printer: 5 W\nLG TV: 0 W\nPeoTV box: 0 W"
    assert "iPhone sleep shortcut" in tools.run("device", {"device": "Desk 2"})


def test_ev05_is_the_3d_printer_on_at_night():
    signal, tools = evals.case(evals.LIVE[1], evals.settings())
    assert signal.startswith("3D printer: left on since Mon 02 Nov 01:00")
    assert "Access: none." in tools.run("device", {"device": "3D printer"})


def test_ev22_is_the_tv_on_at_midday():
    signal, tools = evals.case(evals.LIVE[2], evals.settings())
    assert signal == "LG TV: on at an unusual time from Thu 12 Nov 11:46, 44 min, peak 29 W"
    assert tools.run("power", {"device": "LG TV", "hours": 1}).endswith("12:29: about 27 W")


def test_a_model_that_decides_well_passes(tmp_path):
    scores = evals.run(Wise(), samples=2, path=tmp_path / "r.json")
    assert [s.passed for s in scores] == [True, True, True]
    assert scores[0].line() == "EV-01: right 2/2, actions ask 2, 1.0 steps each, USD 0.0003: PASS"


def test_asking_without_a_switch_off_is_wrong_for_a_device_still_on(tmp_path):
    normal = [{**OFF[1]}, {**OFF[1], "label": "Later", "does": "remind_later"}]
    ev01, *_ = evals.run(always(decide("ask", normal)), samples=1, path=tmp_path / "r.json")
    assert not ev01.passed


def test_no_decision_fails_even_when_most_are_right():
    right = evals.Sample([], "ignore", True, [], 0.0)
    none = evals.Sample([], "none", True, [], 0.0)
    assert not evals.Score("EV-05", [right] * 4 + [none]).passed
    assert evals.Score("EV-05", [right] * 4 + [evals.Sample([], "ask", False, [], 0.0)]).passed


def test_live_steps_are_recorded_and_replayed_at_no_cost(tmp_path):
    path = tmp_path / "r.json"
    live = evals.run(Wise(), samples=2, path=path)
    saved = json.loads(path.read_text())
    assert saved["EV-01"]["model"] == "claude-haiku-5-5" and len(saved["EV-01"]["samples"]) == 2
    again = evals.run(None, path=path)
    assert [s.line().replace("0.0003", "0.0000") for s in live] == [s.line() for s in again]


@pytest.mark.parametrize("what", ["SYSTEM", "VERSION"])
def test_a_change_to_what_the_model_reads_makes_the_recording_stale(tmp_path, monkeypatch, what):
    path = tmp_path / "r.json"
    evals.run(Wise(), samples=1, path=path)
    monkeypatch.setattr(agent, what, getattr(agent, what) + " v")
    with pytest.raises(evals.Stale, match="EV-01: the prompt changed"):
        evals.run(None, path=path)


# from the command line


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    """The evals record to a temporary file, never to evals/recorded.json."""
    path = tmp_path / "recorded.json"
    monkeypatch.setattr(evals, "RECORDED", path)
    return path


def agentc(capsys, *args, step=None):
    code = cli.main(list(args), step=step)  # eval needs no config.toml
    return code, capsys.readouterr().out


def test_eval_live_records_then_eval_replays(recorded, capsys):
    code, out = agentc(capsys, "eval", "--live", "--samples", "1", step=Wise())
    assert (code, out.splitlines()[1]) == (
        0, "EV-05: right 1/1, actions ignore 1, 1.0 steps each, USD 0.0001: PASS")  # fmt: skip
    assert recorded.exists()
    assert agentc(capsys, "eval")[1].endswith("USD 0.0000: PASS\n")


def test_a_failing_case_fails_the_command(recorded, capsys):
    assert agentc(capsys, "eval", "--live", "--samples", "1", step=always(decide("note")))[0] == 1


def test_eval_live_needs_a_key(recorded, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert agentc(capsys, "eval", "--live") == (1, cli.NO_KEY + "\n")


def test_a_stale_recording_is_reported(recorded, capsys, monkeypatch):
    agentc(capsys, "eval", "--live", "--samples", "1", step=Wise())
    monkeypatch.setattr(agent, "SYSTEM", agent.SYSTEM + " Be brief.")
    assert agentc(capsys, "eval") == (
        1,
        "EV-01: the prompt changed. Run `agentc eval --live` again.\n",
    )


def test_the_recorded_live_steps_still_pass():  # free on every commit
    assert [s.passed for s in evals.run(None)] == [True, True, True]
