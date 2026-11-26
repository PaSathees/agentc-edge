"""The housemate's evals: real nights, recorded live, replayed free on every commit."""

import json
import re

import pytest

from agentc import agent, cli, evals, review, setup
from agentc.house import Card

OFF = [{"label": "Switch off", "does": "switch_off_now"},
       {"label": "That's normal", "does": "save_habit"}]  # fmt: skip


def decide(action, choices=()):
    args = {"action": action, "text": "Still on at night." if choices else "Normal.",
            "choices": list(choices)}  # fmt: skip
    return [{"type": "tool_use", "id": "t1", "name": "decide", "input": args}]


class Wise:
    """A stand-in model: asks about Desk 2, ignores the rest. Counts its steps."""

    def __init__(self):
        self.steps = 0

    def __call__(self, *, tier, system, messages, tools, on_text=None):
        self.steps += 1
        if any(t["name"] == "propose_plan" for t in tools):  # the Planner: explain each chosen
            ids = re.findall(r"^(c\d+): ", messages[0]["content"], flags=re.M)
            short = re.search(r"([\d.]+ kgCO2e) over the goal", messages[0]["content"])
            args = {"summary": f"Short by {short.group(1)}." if short else "A quiet week.",
                    "items": [{"id": i, "why": "It idles when nobody uses it."} for i in ids],
                    "left_out": []}  # fmt: skip
            return (
                [{"type": "tool_use", "id": "p1", "name": "propose_plan", "input": args}],
                ("tool_use", 1000, 100)[0],
                1000,
                100,
            )
        if any(t["name"] == "month" for t in tools):  # the Assistant: by what is asked
            return ask_reply(messages[0]["content"], messages), "tool_use", 1000, 100
        if any(t["name"] == "try_change" for t in tools):  # the review chat: by what is said
            return review_reply(messages[-1]["content"]), "tool_use", 1000, 100
        if any(t["name"] == "propose_card" for t in tools):  # the setup chat: usual, then a card
            read = any(isinstance(m["content"], list) for m in messages)  # a tool result came
            name, args = ("propose_card", TV_CARD) if read else ("usual", USUAL)
            return (
                [{"type": "tool_use", "id": "t1", "name": name, "input": args}],
                "tool_use",
                1000,
                100,
            )
        desk = messages[0]["content"].startswith("Signal: Desk 2:")
        return (decide("ask", OFF) if desk else decide("ignore")), "tool_use", 1000, 100


def ask_reply(said, messages):
    """The Assistant's right answers: a Friday habit, Desk 2, the box switched off."""
    said = said.split("The owner says:")[-1]  # not the brief, which names every device
    base = {"device": "LG TV", "kind": "", "timer": 0, "time": "", "off": "", "on": "",
            "days": [], "start": "", "end": "", "text": "", "message": "Here it is."}  # fmt: skip
    if "Fridays" in said:
        args = {**base, "action": "habit", "days": ["Friday"], "start": "19:00", "end": "01:00",
                "text": "Late TV"}  # fmt: skip
    elif "PeoTV" in said:
        args = {**base, "action": "switch_off", "device": "PeoTV box"}
    else:
        read = any(isinstance(m["content"], list) for m in messages)
        return ([{"type": "text", "text": "Desk 2 used the most."}] if read else
                [{"type": "tool_use", "id": "c1", "name": "cost", "input": COST}])  # fmt: skip
    return [{"type": "tool_use", "id": "a1", "name": "propose", "input": args}]


COST = {"device": "Desk 2", "start": "2026-11-09 00:00", "end": "2026-11-15 02:30"}


def review_reply(said):
    """The review chat's right answers: keep for weekends, anyway when told, else explain."""
    base = {"item": 2, "device": "LG TV", "kind": "move_timer", "timer": 2, "time": "",
            "off": "", "on": "", "days": [], "reason": "",
            "message": "Here it is."}  # fmt: skip
    if "Leave it" in said:
        args = {**base, "action": "keep", "reason": "We watch TV late at weekends."}
    elif "anyway" in said:
        args = {**base, "action": "anyway"}
    else:
        text = {"weekends": "Shall I leave it as it is?", "cut": "Late TV on 3 of 14 days.",
                "saves the most": "The 3D printer: about 3.55 kWh."}  # fmt: skip
        return [{"type": "text", "text": next(v for k, v in text.items() if k in said)}]
    return [{"type": "tool_use", "id": "r1", "name": "propose", "input": args}]


USUAL = {"device": "LG TV", "day": "Monday", "start": "18:00", "end": "23:00"}
TV_CARD = {"kind": "media", "room": "living room", "what": "LG TV", "notes": "",
           "must_run": "no", "habits": [], "message": "Here is the card. Save it?"}  # fmt: skip


def always(blocks):
    def send(*, tier, system, messages, tools, on_text=None):
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
    assert [s.passed for s in scores] == [True] * 13
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
    free = [s.line() for s in again]
    assert [re.sub(r"USD [\d.]+", "USD 0.0000", s.line()) for s in live] == free


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
    assert [s.passed for s in evals.run(None)] == [True] * 13


def test_ev13_is_a_new_tv_on_real_readings():
    tools = evals.setup_case(evals.settings())
    assert "nothing known yet" in tools.run("device", {"device": "LG TV"})
    assert tools.run("power", {"device": "LG TV", "hours": 1}).endswith("12:29: about 27 W")


def test_ev13_scores_the_card_the_owner_described():
    right = evals._setup_right  # pyright: ignore[reportPrivateUsage]
    good, read = setup.parse(TV_CARD, base=Card()), ["power", "usual"]
    assert right(good, read)
    for change in ({"kind": "light"}, {"room": "study"}, {"must_run": "yes"}):
        assert not right(setup.parse({**TV_CARD, **change}, base=Card()), read)
    assert not right(None, read)
    assert not right(good, ["power"])  # never read the usual week, so suggested no times


def test_a_case_never_recorded_says_how_to_record_it_and_the_live_ones_are_kept(tmp_path):
    why = "EV-05: not recorded yet. Run `agentc eval --live --case EV-05`"
    path = tmp_path / "r.json"
    with pytest.raises(evals.Stale, match=why):  # only EV-01 is asked; EV-05 has nothing
        evals.run(Wise(), samples=1, path=path, cases=["EV-01"])
    assert list(json.loads(path.read_text())) == ["EV-01"]  # what it paid for is saved


def test_a_shortfall_check_needs_a_shortfall_and_the_exact_number():
    from dataclasses import replace  # noqa: PLC0415 (local to this check)

    from agentc.planner_agent import Item, Plan  # noqa: PLC0415

    short = "These changes leave about 1.0 kWh, LKR 5, 0.17 kgCO2e over the goal by 30 Nov."
    item = Item("Plug-1", "m", None, "w", "s", "", "")
    good = Plan("Short by 0.17 kgCO2e.", (item,), chosen=("c1",), shortfall=short)
    says = evals._says_shortfall  # pyright: ignore[reportPrivateUsage]
    assert says(good) and not says(replace(good, shortfall="", summary="Short by ?"))
    assert not says(replace(good, summary="Short by 10.17 kgCO2e."))


@pytest.mark.parametrize(
    ("case", "draft", "said", "right"),
    [(0, review.Draft("keep", 2, "Plug-3", "LG TV", None, "x", "", "Late at weekends."),
      ["Shall I?"], False),  # drafted before the owner decided
     (1, review.Draft("anyway", 2, "Plug-3", "LG TV", evals.TV_MOVE, "x", "s"),
      ["It saves a little.", "Here."], False),  # never said the days
     (2, None, ["The 3D printer saves the most."], False)],  # no number
)  # fmt: skip
def test_the_review_cases_need_the_right_moment_and_words(case, draft, said, right):
    assert evals.REVIEWS[case].right(draft, said) is right


def test_the_ask_cases_need_the_right_window_and_answer():
    from agentc import assistant, house  # noqa: PLC0415

    def habit(days, start, end):
        return assistant.Draft("habit", "Plug-3", "LG TV", "x",
                               habit=house.Habit("Late TV", days, start, end, "chat"))  # fmt: skip

    friday, most = evals.ASKS[0].right, evals.ASKS[1].right
    assert friday(habit("0000010", "19:00", "01:00"), "", [])
    assert not friday(habit("0000010", "00:00", "01:00"), "", [])  # Thursday night
    assert not friday(habit("1111111", "19:00", "01:00"), "", [])  # every day
    assert most(None, "Desk 2 used the most; then the LG TV.", ["cost"])
    assert not most(None, "The LG TV used the most; Desk 2 used less.", ["cost"])
    assert not most(None, "Desk 2 used the most.", [])  # never worked it out
