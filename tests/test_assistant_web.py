"""The Ask tab: the Assistant's chat, and the owner's tap on its draft (5d)."""

from agentc import app as web
from agentc import assistant, house, notify, planner, store
from tests.test_actions_web import live  # noqa: F401 (the fixture)
from tests.test_app import KEY, NOW, client, events, speaks  # noqa: F401 (the fixtures)
from tests.test_plan_web import home  # noqa: F401 (the fixture)

TOK = notify.token(KEY, "ask")


def draft(db, d):
    chat = store.open_assistant(db, site_id=1, now=NOW)
    store.set_draft(db, chat_id=chat, draft=d.to_json())
    return chat


def tap(c):
    return c.post("/ask/act", data={"tok": TOK})


HABIT = house.Habit("Late games", "0000010", "20:00", "01:00", "chat")


def test_the_ask_tab_greets_by_code_once(home):  # noqa: F811
    c, db, _, _ = home
    page = c.get("/ask").text
    assert "Ask me about your home&#39;s electricity" in page
    assert "About your home&#39;s electricity" in page and 'hx-post="/ask/say"' in page
    assert '<a href="/ask" >Ask</a>' in c.get("/plan").text  # the header link, on every page
    c.get("/ask")
    assert len(store.turns(db, chat_id=1)) == 1


def test_a_question_and_its_streamed_answer(home):  # noqa: F811
    c, db, _, _ = home
    c.get("/ask")
    r = c.post("/ask/say", data={"text": "What is on?", "tok": TOK})
    assert f'sse-connect="/ask/reply?tok={TOK}"' in r.text
    assert c.post("/ask/say", data={"text": "Again", "tok": TOK}).status_code == 409
    web.app.dependency_overrides[web.stepper] = lambda: speaks(
        [{"type": "text", "text": "Desk 2 is on."}]
    )
    got = events(c.get(f"/ask/reply?tok={TOK}").text)
    assert got[0] == ("text", "Desk 2 is on.") and "Desk 2 is on.</div>" in got[-1][1]
    assert [role for _, role, _ in store.turns(db, chat_id=1)] == ["agent", "owner", "agent"]


def test_forms_need_the_token_and_words(home):  # noqa: F811
    c, _, _, _ = home
    assert c.post("/ask/say", data={"text": "x", "tok": "forged"}).status_code == 403
    assert c.post("/ask/say", data={"text": " ", "tok": TOK}).status_code == 422
    assert tap(c).status_code == 409  # no draft yet


def test_a_habit_is_saved_only_on_the_tap(home):  # noqa: F811
    c, db, _, _ = home
    draft(db, assistant.Draft("habit", "Plug-1", "Desk 2", "Late games: Friday, 20:00 to 01:00",
                              habit=HABIT))  # fmt: skip
    page = c.get("/ask").text
    assert ">Save habit</button>" in page and "I won't ask about it" in page
    assert store.habits(db, mac="AA") == []
    assert "Saved: Late games: Friday, 20:00 to 01:00." in tap(c).text
    assert [h.text for _, h in store.habits(db, mac="AA")] == ["Late games"]
    assert tap(c).status_code == 409  # the draft is gone


def test_a_timer_change_goes_through_the_actuator(home):  # noqa: F811
    c, db, _, plug = home
    move = planner.Change("move_timer", slot=2, time="07:30", from_time="06:00")
    draft(db, assistant.Draft("anyway", "Plug-1", "Desk 2", "On at 06:00 to On at 07:30", move,
                              "Saves a little. In use then on 2 of 14 days.", 2))  # fmt: skip
    page = c.get("/ask").text
    assert ">Do it anyway</button>" in page and "would have got in the way" in page
    done = tap(c).text
    assert "now runs at 07:30 (was 06:00). You can put it back on the Plan tab" in done
    assert plug.timers[2]["Time"] == "07:30"


def test_a_switch_off_has_its_undo_in_the_inbox(home):  # noqa: F811
    c, db, _, plug = home
    draft(db, assistant.Draft("switch_off", "Plug-1", "Desk 2", "Switch Desk 2 off now"))
    assert ">Switch off</button>" in c.get("/ask").text
    r = tap(c)
    assert "Done: Desk 2 is off. Switch it back on from the inbox for a day." in r.text
    assert not plug.relay_on and "Switch back on" in c.get("/").text


def test_a_refused_switch_off_says_why(home):  # noqa: F811
    c, db, _, plug = home
    store.save_card(db, site_id=1, mac="AA", card=house.Card("router"), now=NOW)
    draft(db, assistant.Draft("switch_off", "Plug-1", "Desk 2", "Switch Desk 2 off now"))
    assert "must always run" in tap(c).text and plug.relay_on


def test_a_gone_device_or_an_empty_change_does_nothing(home):  # noqa: F811
    c, db, _, _ = home
    draft(db, assistant.Draft("habit", "Plug-99", "Old", "x", habit=HABIT))
    assert "not in config.toml any more" in tap(c).text
    draft(db, assistant.Draft("change", "Plug-1", "Desk 2", "x"))
    assert tap(c).status_code == 409
    store.close_chat(db, chat_id=1, status="closed")  # the habit's device seen no more
    db.execute("UPDATE plugs SET name = 'Plug-gone' WHERE mac = 'AA'")
    db.commit()
    draft(db, assistant.Draft("habit", "Plug-1", "Desk 2", "x", habit=HABIT))
    assert "that device is not seen any more" in tap(c).text


def test_start_again_and_a_tap_during_a_reply(home):  # noqa: F811
    from agentc.web import chat  # noqa: PLC0415

    c, db, _, _ = home
    c.get("/ask")
    chat.RUNS[1] = chat.Run()
    try:
        assert tap(c).status_code == 409
    finally:
        del chat.RUNS[1]
    r = c.post("/ask/restart", data={"tok": TOK}, follow_redirects=False)
    assert r.status_code == 303 and store.chat(db, chat_id=1).status == "closed"  # pyright: ignore[reportOptionalMemberAccess]
    c.get("/ask")
    assert store.open_assistant(db, site_id=1, now=NOW) == 2


def test_a_tap_waits_while_the_owners_message_has_no_reply_and_reads_are_cleared(home):  # noqa: F811
    c, db, _, _ = home
    chat = draft(db, assistant.Draft("habit", "Plug-1", "Desk 2", "x", habit=HABIT))
    store.add_reads(db, chat_id=chat, reads=[("device {}", "Desk 2: no habits")])
    store.add_turn(db, chat_id=chat, site_id=1, role="agent", text="Hello.", now=NOW)
    store.add_turn(db, chat_id=chat, site_id=1, role="owner", text="And?", now=NOW)
    assert tap(c).status_code == 409  # its reply may bring a new draft
    store.add_turn(db, chat_id=chat, site_id=1, role="agent", text="Here.", now=NOW)
    assert "Saved" in tap(c).text
    assert store.chat(db, chat_id=chat).reads == ()  # pyright: ignore[reportOptionalMemberAccess]


def test_a_draft_from_an_older_version_is_dropped(home):  # noqa: F811
    c, db, _, _ = home
    chat = store.open_assistant(db, site_id=1, now=NOW)
    store.set_draft(db, chat_id=chat, draft={"action": "habit"})
    assert c.get("/ask").status_code == 200
