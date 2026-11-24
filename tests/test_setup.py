"""The setup chat's agent: it reads the plug, drafts a card, and never saves on its own."""

from datetime import timedelta

import pytest

from agentc import setup, store
from agentc.config import Budget
from agentc.house import Card, Habit
from agentc.llm import Llm
from tests.test_agent_tools import CFG, NOW, tools  # noqa: F401 (tools is a fixture)

GREETING = ("agent", "Let's set up Desk 2. What is plugged into it?")
CARD = {"kind": "computer", "room": "study", "what": "monitor and speakers", "notes": "",
        "must_run": "no", "message": "Here is the card. Save it, or tell me what to change.",
        "habits": [{"text": "Work, weekdays 09:00 to 18:00", "days": "weekdays",
                    "start": "09:00", "end": "18:00"}]}  # fmt: skip


def use(name, args, n=1):
    return {"type": "tool_use", "id": f"t{n}", "name": name, "input": args}


class Script:
    """A stand-in model: each step's blocks in turn. Streams any text it gives."""

    def __init__(self, *turns):
        self.turns, self.seen = list(turns), []

    def __call__(self, *, tier, system, messages, tools, on_text=None):  # noqa: F811
        self.seen.append({"messages": list(messages), "tools": [t["name"] for t in tools]})
        blocks = self.turns.pop(0)
        for b in blocks:
            if b["type"] == "text" and on_text:
                on_text(b["text"])
        return blocks, "end_turn", 1000, 100


def chat(tools, script, talk=None, events=None):  # noqa: F811
    m = Llm(tools.app, 1, CFG.tz, CFG.tiers, Budget(3.0, 0.05), script, clock=lambda: NOW)
    talk = talk or [GREETING, ("owner", "my work desk: monitor and speakers")]
    on = (lambda kind, text: events.append((kind, text))) if events is not None else None
    return setup.reply(talk, "Plug-1", tools=tools, llm=m, run_id="setup:1", on_event=on)


def test_it_looks_at_the_power_then_drafts_a_card(tools):  # noqa: F811
    events = []
    r = chat(tools, Script([use("power", {"device": "Desk 2", "hours": 24})],
                           [use("propose_card", CARD, 2)]), events=events)  # fmt: skip
    assert r.text == CARD["message"] and r.tools == ("power",)
    assert r.draft == setup.Draft(
        Card("computer", "study", "monitor and speakers", "", None, {}),
        (Habit("Work, weekdays 09:00 to 18:00", "0111110", "09:00", "18:00", "setup"),),
    )
    assert events == [("step", "looking at its power")]


def test_a_question_is_plain_text_and_streams(tools):  # noqa: F811
    events = []
    r = chat(tools, Script([{"type": "text", "text": "Which room is it in?"}]), events=events)
    assert (r.text, r.draft) == ("Which room is it in?", None)
    assert events == [("text", "Which room is it in?")]


def test_the_brief_holds_the_card_the_greeting_and_the_owners_words(tools):  # noqa: F811
    script = Script([{"type": "text", "text": "Which room?"}])
    chat(tools, script, talk=[GREETING, ("owner", "my desk"), ("agent", "Which room?"),
                              ("owner", "the study")])  # fmt: skip
    first, second, third = script.seen[0]["messages"]
    assert first["role"] == "user" and "Plug: Plug-1. Device name: Desk 2." in first["content"]
    assert "You opened the chat with: Let's set up Desk 2." in first["content"]
    assert first["content"].endswith("The owner says: my desk")
    assert (second["role"], third["content"]) == ("assistant", "the study")


def test_a_bad_draft_goes_back_with_the_reason(tools):  # noqa: F811
    bad = {**CARD, "habits": [{**CARD["habits"][0], "start": "9am"}]}
    script = Script([use("propose_card", bad)], [use("propose_card", CARD, 2)])
    r = chat(tools, script)
    assert r.draft is not None
    result = script.seen[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "times as HH:MM" in result["content"]


def test_the_last_step_may_only_draft(tools):  # noqa: F811
    turns = [[use("usual", {"device": "Desk 2", "day": "Monday", "start": "00:00",
                            "end": "06:00"}, n)] for n in range(setup.MAX_STEPS)]  # fmt: skip
    script = Script(*turns)
    r = chat(tools, script)
    assert script.seen[-1]["tools"] == ["propose_card"] and r.draft is None
    assert r.text.startswith("Sorry, I could not finish that.")


@pytest.mark.parametrize(
    ("change", "why"),
    [({"kind": "toaster"}, "kind and must_run"), ({"what": " "}, "what is plugged in"),
     ({"habits": [{**CARD["habits"][0], "days": "sometimes"}]}, "days from the list")],
)  # fmt: skip
def test_parse_refuses_a_bad_draft(change, why):
    with pytest.raises(ValueError, match=why):
        setup.parse({**CARD, **change}, base=Card())


def test_must_run_yes_is_kept_and_no_follows_the_kind():
    assert setup.parse({**CARD, "must_run": "yes"}, base=Card()).card.must_run is True
    assert setup.parse({**CARD, "kind": "fridge"}, base=Card()).card.must_run is None


def test_a_draft_keeps_the_cards_timer_reasons():
    base = Card(why={1: "wakes the TV"})
    d = setup.parse(CARD, base=base)
    assert d.card.why == {1: "wakes the TV"}
    assert setup.Draft.from_json(d.to_json(), base=base) == d


def test_the_greeting_is_made_by_code():
    assert setup.greeting("Desk 2", Card()) == "Let's set up Desk 2. What is plugged into it?"
    assert setup.greeting("Desk 2", Card("computer")).startswith("Here is what I know")


# only the owner's Save writes


def opened(tools, draft=True):  # noqa: F811
    chat_id = store.open_chat(tools.app, site_id=1, mac="AA", kind="setup", now=NOW)
    if draft:
        store.set_draft(tools.app, chat_id=chat_id, draft=setup.parse(CARD, base=Card()).to_json())
    return chat_id


def test_save_writes_the_card_and_habits_and_closes_the_chat(tools):  # noqa: F811
    chat_id = opened(tools)
    said = setup.save(tools.app, CFG, site_id=1, chat_id=chat_id, now=NOW)
    assert said == "Saved the card for Desk 2 and 1 habit."
    assert store.load_card(tools.app, mac="AA").room == "study"
    assert [h.source for _, h in store.habits(tools.app, mac="AA")] == ["setup"]
    assert store.chat(tools.app, chat_id=chat_id)[1] == "saved"  # type: ignore[index]
    with pytest.raises(LookupError, match="no open draft"):
        setup.save(tools.app, CFG, site_id=1, chat_id=chat_id, now=NOW)


def test_nothing_is_saved_without_a_draft(tools):  # noqa: F811 (FR-30: owner confirms first)
    chat_id = opened(tools, draft=False)
    before = store.load_card(tools.app, mac="AA")
    with pytest.raises(LookupError):
        setup.save(tools.app, CFG, site_id=1, chat_id=chat_id, now=NOW)
    assert store.load_card(tools.app, mac="AA") == before


def test_a_chat_is_kept_and_reopened(tools):  # noqa: F811
    first = store.open_chat(tools.app, site_id=1, mac="AA", kind="setup", now=NOW)
    store.add_turn(tools.app, chat_id=first, site_id=1, role="owner", text="hi", now=NOW)
    assert store.open_chat(tools.app, site_id=1, mac="AA", kind="setup", now=NOW) == first
    assert [r for _, r, _ in store.turns(tools.app, chat_id=first)] == ["owner"]
    store.close_chat(tools.app, chat_id=first, status="closed")
    later = NOW + timedelta(minutes=1)
    assert store.open_chat(tools.app, site_id=1, mac="AA", kind="setup", now=later) != first
    assert store.chat(tools.app, chat_id=999) is None
