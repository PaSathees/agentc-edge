"""The housemate agent's loop: look with tools, then decide; code checks the decision."""

import pytest

from agentc import agent
from agentc.agent import Choice, Decision, check_decision
from agentc.config import Budget, Device
from agentc.house import Card
from agentc.llm import Llm, OverBudget, Unreachable
from tests.test_agent_tools import CFG, NOW, tools  # noqa: F401 (tools is a fixture)

ASK = {"action": "ask", "text": "Desk 2 has drawn about 5 W since 01:51. Switch it off now?",
       "choices": [{"label": "Switch off", "does": "switch_off_now"},
                   {"label": "That's normal", "does": "save_habit"}]}  # fmt: skip


PLAIN = {**ASK, "text": "Desk 2 is still on and nothing else is. Switch it off now?"}  # no numbers


def use(name, args, n=1):
    return {"type": "tool_use", "id": f"t{n}", "name": name, "input": args}


class Script:
    """A stand-in model: answers each step with the next list of blocks. Notes what it saw."""

    def __init__(self, *turns):
        self.turns, self.seen = list(turns), []

    def __call__(self, *, tier, system, messages, **kw):  # kw["tools"]: the tools offered
        self.seen.append({"messages": list(messages), "tools": [t["name"] for t in kw["tools"]]})
        return self.turns.pop(0), "tool_use", 1000, 100


def model(db, script, run_usd=0.05):
    return Llm(db, 1, CFG.tz, CFG.tiers, Budget(3.0, run_usd), script, clock=lambda: NOW)


def look(tools, script, **kw):  # noqa: F811
    signal = "Desk 2: left on since Mon 02 Nov 01:37, 33 min so far, about 30 W"
    m = model(tools.app, script, **kw)
    return agent.look(signal, "Plug-1", tools=tools, llm=m, run_id="event:1")


def test_the_agent_looks_then_decides(tools):  # noqa: F811
    script = Script([use("power", {"device": "Desk 2", "hours": 1})], [use("decide", ASK, 2)])
    run = look(tools, script)
    assert run.decision == Decision(
        "ask",
        ASK["text"],
        (Choice("Switch off", "switch_off_now"), Choice("That's normal", "save_habit")),
    )
    assert run.tools == ["power"] and run.blocked is None
    result = script.seen[1]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and "about 5 W" in result["content"]


def test_the_brief_holds_the_signal_and_the_card(tools):  # noqa: F811
    script = Script([use("decide", {"action": "ignore", "text": "Normal.", "choices": []})])
    look(tools, script)
    brief = script.seen[0]["messages"][0]["content"]
    assert "left on since Mon 02 Nov 01:37" in brief and "Plugged in: monitor." in brief
    assert "Now: Mon 02 Nov 02:10" in brief


def test_a_tool_error_goes_back_to_the_model_and_the_loop_goes_on(tools):  # noqa: F811
    script = Script([use("power", {"device": "Toaster", "hours": 1})], [use("decide", PLAIN, 2)])
    run = look(tools, script)
    result = script.seen[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and run.decision is not None


def test_text_with_no_tool_call_gets_one_nudge(tools):  # noqa: F811
    script = Script([{"type": "text", "text": "Thinking..."}], [use("decide", PLAIN)])
    assert look(tools, script).decision is not None
    assert "call decide" in script.seen[1]["messages"][-1]["content"]


def test_no_decision_after_the_nudge_is_dropped(tools):  # noqa: F811
    script = Script([{"type": "text", "text": "Hmm."}], [{"type": "text", "text": "Still hmm."}])
    run = look(tools, script)
    assert (run.decision, run.blocked) == (None, "format: no decision")


def test_at_the_step_limit_only_decide_is_offered(tools):  # noqa: F811
    turns = [[use("house_now", {}, n)] for n in range(agent.MAX_STEPS - 1)]
    script = Script(*turns, [use("decide", PLAIN, 99)])
    run = look(tools, script)
    assert script.seen[-1]["tools"] == ["decide"] and run.decision is not None
    assert len(script.seen) == agent.MAX_STEPS


def test_numbers_from_a_tool_are_grounded(tools):  # noqa: F811
    script = Script([use("power", {"device": "Desk 2", "hours": 1})], [use("decide", ASK, 2)])
    assert look(tools, script).blocked is None


def test_numbers_no_tool_gave_are_blocked(tools):  # noqa: F811 (5 W and 01:51 never looked up)
    script = Script([use("decide", ASK)], [use("decide", ASK, 2)])
    assert look(tools, script).blocked == "grounded: not in the facts: 5, 01:51"


def test_a_blocked_decision_gets_one_retry_with_the_reason(tools):  # noqa: F811
    made_up = {**ASK, "text": "Desk 2 has used 42 W all night. Switch it off?"}
    script = Script([use("decide", made_up)], [use("decide", PLAIN, 2)])
    run = look(tools, script)
    assert (
        run.decision is not None
        and "not in the facts: 42" in script.seen[1]["messages"][-1]["content"][0]["content"]
    )


def test_a_decision_blocked_twice_is_dropped(tools):  # noqa: F811
    made_up = {**ASK, "text": "It used 42 W. Off?"}
    run = look(tools, Script([use("decide", made_up)], [use("decide", made_up, 2)]))
    assert run.decision is None and (run.blocked or "").startswith("grounded")


def test_a_model_out_of_reach_stops_the_look(tools):  # noqa: F811
    def gone(**_):
        raise Unreachable("no internet")

    with pytest.raises(Unreachable):
        look(tools, gone)


def test_the_run_cap_stops_the_look(tools):  # noqa: F811
    script = Script(*[[use("house_now", {}, n)] for n in range(9)])
    with pytest.raises(OverBudget):
        look(tools, script, run_usd=0.0002)


# checks on a decision: code, after the model


DESK, CARD, FACTS = Device("Desk 2"), Card("computer"), "about 5 W since 01:51"


def decision(action="ask", text="About 5 W since 01:51. Off?", *choices):
    return Decision(action, text, tuple(choices) or (Choice("Off", "switch_off_now"),
                                                     Choice("Normal", "save_habit")))  # fmt: skip


@pytest.mark.parametrize(
    ("d", "device", "card", "why"),
    [
        (decision(), DESK, CARD, None),
        (decision("ask", "Off?", Choice("Off", "switch_off_now")), DESK, CARD,
         "format: a question needs 2 or 3 choices"),
        (Decision("note", "Quiet night.", (Choice("Off", "switch_off_now"),)), DESK, CARD,
         "format: ignore and note take no choices"),
        (decision(), Device("3D printer", access="none"), CARD,
         "access: 3D printer is never changed, so only ignore or note"),
        (decision(), DESK, Card("fridge"), "must run: Desk 2 is never switched off"),
        (decision("ask", "It used 42 W. Off?"), DESK, CARD, "grounded: not in the facts: 42"),
        (decision("ask", "About 5 W since 01:51. It is late. Off?"), DESK, CARD,
         "format: 3 sentences; at most 2 for ask, read on a phone"),
        (Decision("note", "Desk 2 ran late. Move its timer?", ()), DESK, CARD,
         "format: 2 sentences; at most 1 for note, read on a phone"),
        (Decision("ignore", "Normal. It runs at night.", ()), DESK, CARD,
         "format: 2 sentences; at most 1 for ignore, read on a phone"),
        (Decision("ignore", "Normal for this hour.", ()), Device("3D printer", access="none"),
         CARD, None),
        (decision("ask", "About 5 W since 01:51. Off?", Choice("Off", "switch_off_now"),
                  Choice("Off now", "switch_off_now")), DESK, CARD,
         "format: two choices do the same"),
    ],
)  # fmt: skip
def test_check_decision(d, device, card, why):
    assert check_decision(d, device=device, card=card, facts=FACTS) == why


def test_the_housemate_cannot_propose_or_offer_a_timer(tools):  # noqa: F811 (Planner's job)
    for raw in ({"action": "propose", "text": "Timer?", "choices": []},
                {"action": "ask", "text": "x", "choices": [{"label": "Timer",
                                                             "does": "set_timer"}]}):  # fmt: skip
        run = look(tools, Script([use("decide", raw)], [use("decide", raw, 2)]))
        assert run.decision is None and (run.blocked or "").startswith("format:")


def test_a_model_that_ignores_the_last_offer_ends_with_no_decision(tools):  # noqa: F811
    script = Script(*[[use("house_now", {}, n)] for n in range(agent.MAX_STEPS)])
    run = look(tools, script)
    assert (run.decision, run.blocked) == (None, "format: no decision")


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ({"action": "shout", "text": "x", "choices": []}, "format: action must be one of"),
        ({"action": "ask", "text": "x", "choices": [{"label": "a", "does": "explode"}]},
         "format: a choice must do one of"),
        ({"action": "ask", "text": "x"}, "format: 'choices'"),
    ],
)  # fmt: skip
def test_a_malformed_decision_is_blocked(tools, raw, why):  # noqa: F811
    run = look(tools, Script([use("decide", raw)], [use("decide", raw, 2)]))
    assert run.decision is None and (run.blocked or "").startswith(why)


def _enums(schema):
    """Every (declared type, enum) pair in a JSON schema, at any depth."""
    if isinstance(schema, dict):
        if "enum" in schema:
            yield schema.get("type"), schema["enum"]
        for v in schema.values():
            yield from _enums(v)
    elif isinstance(schema, list):
        for v in schema:
            yield from _enums(v)


def test_each_enum_holds_only_values_of_its_type():  # the API refuses a null mixed in (live run)
    for t in agent.TOOLS:
        for kind, values in _enums(t["input_schema"]):
            assert kind == "string" and all(isinstance(v, str) for v in values), t["name"]


@pytest.mark.parametrize(
    ("text", "n"),
    [("About 5 W since 01:51. Off?", 2), ("It used 0.031 kWh and 2.5 W", 1), ("Normal.", 1),
     ("Wait... ok", 2), ("", 0)],
)  # fmt: skip
def test_sentences_end_at_a_mark_before_a_space_not_in_a_number(text, n):
    assert agent.sentences(text) == n


def test_the_times_the_prompt_gives_are_known():  # "it will reach you at 07:00"
    d = decision("ask", "About 5 W since 01:51. Off now, or ask me at 07:00?")
    assert check_decision(d, device=DESK, card=CARD, facts=FACTS) is None
