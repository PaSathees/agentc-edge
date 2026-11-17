import json
from datetime import UTC, datetime, timedelta

import pytest

from agentc import responder, store
from agentc.config import Budget, Device, Rule, Tier
from agentc.llm import Llm
from agentc.options import Change, Option, Proposal, Use, of
from agentc.policy import Policy
from agentc.watcher import Event, Run, Usual
from tests.test_options import MONEY, OFF, desk, night

NOW = datetime(2026, 11, 16, 4, 0, tzinfo=UTC)
DESK = Device("Desk 2")
A = Option("A", (Change(9, "off", "01:30", "1111111"), Change(10, "on", "07:00", "1111111")),
           kwh=0.031, lkr=3.2, kg=0.013)  # fmt: skip
B = Option("B", (Change(9, "off", "01:30", "0111110"), Change(10, "on", "07:00", "0111110")),
           kwh=0.031, lkr=3.2, kg=0.013)  # fmt: skip
RUN = Run(datetime(2026, 11, 2, 19, 30, tzinfo=UTC), datetime(2026, 11, 3, 0, 29, tzinfo=UTC), 5.0)
GOOD = {"option": "A", "reason": "Desk 2 was left on at 5 W. Off at 01:30 saves LKR 3.2 a week."}


def case(device=DESK, options=(A, B), rejected=()):
    return responder.Case("AA", device, RUN, 1.0, Use(of("00:52"), of("07:45")), list(options),
                          list(rejected), days=14)  # fmt: skip


class Script:
    """A fake model that gives these answers in turn and notes each prompt."""

    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def __call__(self, *, tier, system, prompt, schema):
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        return (answer if isinstance(answer, str) else json.dumps(answer)), 1000, 100


@pytest.fixture
def db(tmp_path):
    db = store.connect(tmp_path / "app.db")
    store.add_site(db, name="home", tz="Asia/Colombo")
    store.add_event(db, site_id=1, event=Event("unusual_use", None, RUN.start, {}), dedupe="e1")
    return db


def respond(db, model, c=None):
    llm = Llm(db, 1, "Asia/Colombo", {"small": Tier("m", 1.0, 10.0, "low")},
              Budget(month_usd=1.0, run_usd=0.05), model, clock=lambda: NOW)  # fmt: skip
    return responder.respond(c or case(), llm=llm, db=db, site_id=1, event_id=1, run_id="r1",
                             tz="Asia/Colombo", now=NOW)  # fmt: skip


def audit(db):
    rows = db.execute("SELECT action, ref, detail FROM audit")
    return [(action, ref, json.loads(detail)) for action, ref, detail in rows]


# gather: code works out the case before any model call


def test_gather_finds_the_use_and_the_fixes():
    c = responder.gather(night(1), mac="AA", device=DESK, readings=desk(), usual=OFF, timers=[],
                         money=MONEY, rejected=[], tz="Asia/Colombo", days=14)  # fmt: skip
    assert c is not None
    assert (c.use, c.usual_w, [o.id for o in c.options]) == (Use(of("00:50"), of("07:45")), 0.0,
                                                              ["A", "B"])  # fmt: skip


def test_options_breaking_a_hard_rule_are_dropped_before_the_model():
    late = Rule("latest_off", "02:00", hard=True, text="Desk 2 may run until 02:00")
    c = responder.gather(night(1), mac="AA", device=Device("Desk 2", rules=(late,)),
                         readings=desk(), usual=OFF, timers=[], money=MONEY, rejected=[],
                         tz="Asia/Colombo", days=14)  # fmt: skip
    assert c is None  # nothing left to offer, so no model call


def test_options_the_owner_rejected_are_dropped_before_the_model():  # EV-02
    old = Proposal("AA", "Desk 2", A.changes, "", 0, 0, 0, "", "responder", NOW)
    c = responder.gather(night(1), mac="AA", device=DESK, readings=desk(), usual=OFF, timers=[],
                         money=MONEY, rejected=[old], tz="Asia/Colombo", days=14)  # fmt: skip
    assert c is None  # B is A on fewer days: a repeat too


def test_no_use_means_no_case():
    quiet = [(t, 0.0) for t, _ in desk()]
    c = responder.gather(night(1), mac="AA", device=DESK, readings=quiet, usual=OFF, timers=[],
                         money=MONEY, rejected=[], tz="Asia/Colombo", days=14)  # fmt: skip
    assert c is None


def test_the_usual_watts_come_from_the_hour_the_run_started():
    usual = {**OFF, 2 * 24 + 1: Usual(watts=1.4, on_share=0.0)}  # Wednesday 01:00
    c = responder.gather(night(1), mac="AA", device=DESK, readings=desk(), usual=usual, timers=[],
                         money=MONEY, rejected=[], tz="Asia/Colombo", days=14)  # fmt: skip
    assert c is not None and c.usual_w == 1.4  # night(1) is Tuesday night, into Wednesday


# the facts and the answer shape


def test_the_facts_the_model_reads():
    rule = Rule("latest_off", "01:00", hard=False, text="Desk 2 may run until 01:00 on work nights")
    assert responder.describe(case(Device("Desk 2", rules=(rule,))), tz="Asia/Colombo") == (
        "Device: Desk 2\n"
        "Seen: on at up to 5 W from 01:00 to 05:59 on Tue 03 Nov. Usual at that hour: about 1 W.\n"
        "Over the last 14 days it was never in use between 00:52 and 07:45."
        " The options switch it off only inside that gap.\n"
        "Owner rules: Desk 2 may run until 01:00 on work nights\n"
        "Options:\n"
        "A: off at 01:30, on at 07:00, every day. Saves 0.031 kWh, LKR 3.2, 0.013 kgCO2e a week.\n"
        "B: off at 01:30, on at 07:00, weekdays. Saves 0.031 kWh, LKR 3.2, 0.013 kgCO2e a week."
    )  # fmt: skip


def test_a_run_past_midnight_gives_the_day_it_started():
    late = Run(
        datetime(2026, 11, 2, 18, 0, tzinfo=UTC), datetime(2026, 11, 2, 20, 0, tzinfo=UTC), 5.0
    )
    c = responder.Case("AA", DESK, late, 1.0, Use(of("00:52"), of("07:45")), [A], [], days=14)
    assert "from 23:30 to 01:30 on Mon 02 Nov." in responder.describe(c, tz="Asia/Colombo")


def test_no_rules_say_none():
    assert "Owner rules: none" in responder.describe(case(), tz="Asia/Colombo")


def test_the_answer_may_only_name_an_option_offered_or_none():
    s = responder.schema(case(options=[B]))
    assert s["properties"]["option"]["enum"] == ["B", "none"]
    assert (s["required"], s["additionalProperties"]) == (["option", "reason"], False)


# respond: ask, check, save


def test_a_good_pick_becomes_an_open_proposal(db):  # EV-01
    p = respond(db, Script(GOOD))
    assert p == Proposal("AA", "Desk 2", A.changes, GOOD["reason"], 0.031, 3.2, 0.013,
                         "event:1; 14 days of readings to Mon 16 Nov", "responder",
                         NOW + timedelta(hours=24))  # fmt: skip
    assert store.load_proposals(db, site_id=1) == [(1, "open", p)]
    assert db.execute("SELECT proposal_id FROM llm_calls").fetchall() == [(1,)]
    assert audit(db) == [("proposal_open", "proposal:1", {"option": "A", "warnings": ""})]


def test_no_action_saves_no_proposal(db):  # EV-03
    reason = "Desk 2 may run until 01:00 on work nights, so no change."
    assert respond(db, Script({"option": "none", "reason": reason})) is None
    assert store.load_proposals(db, site_id=1) == []
    assert audit(db) == [("no_action", "event:1", {"reason": reason})]


def test_a_blocked_answer_gets_one_retry_with_the_reason(db):  # EV-04
    model = Script({"option": "A", "reason": "Off at 01:30 saves LKR 40 a week."}, GOOD)
    assert respond(db, model) is not None
    assert model.prompts[1].endswith(
        "Your last answer was blocked: numbers: not in the facts: 40. Answer again."
    )
    assert db.execute("SELECT proposal_id FROM llm_calls").fetchall() == [(1,), (1,)]


def test_blocked_twice_is_dropped_and_the_trace_kept(db):
    bad = {"option": "A", "reason": "Saves LKR 40."}
    assert respond(db, Script(bad, bad)) is None
    assert store.load_proposals(db, site_id=1) == []
    assert db.execute("SELECT COUNT(*) FROM llm_calls").fetchone() == (2,)
    assert audit(db) == [("draft_dropped", "event:1", {"check": "numbers: not in the facts: 40"})]


def test_an_answer_that_is_not_the_json_asked_for_is_blocked(db):
    assert respond(db, Script("Off at 01:30.", '{"option": "A"}')) is None
    assert audit(db)[0][2] == {"check": "format: not the JSON asked for"}


def test_a_device_with_access_none_is_never_asked_about(db):  # EV-05
    model = Script()
    assert respond(db, model, case(Device("3D printer", access="none"))) is None
    assert model.prompts == []


def test_a_soft_rule_warning_is_kept_with_the_proposal(db):
    rule = Rule("latest_off", "02:00", hard=False, text="Desk 2 may run until 02:00")
    respond(db, Script(GOOD), case(Device("Desk 2", rules=(rule,))))
    assert audit(db)[0][2] == {"option": "A", "warnings": "Desk 2 may run until 02:00"}


def test_the_policy_sets_when_a_proposal_expires(db):
    p = respond(db, Script(GOOD), case(Device("Desk 2", policy=Policy(expires_h=48))))
    assert p is not None and p.expires == NOW + timedelta(hours=48)


def test_the_device_policy_reaches_gather():
    strict = Device("Desk 2", policy=Policy(max_off_h=5))  # the 01:30 to 07:00 fix is 5.5 h
    c = responder.gather(night(1), mac="AA", device=strict, readings=desk(), usual=OFF, timers=[],
                         money=MONEY, rejected=[], tz="Asia/Colombo", days=14)  # fmt: skip
    assert c is None


def test_the_device_policy_sets_what_use_is():
    loud = Device("Desk 2", policy=Policy(use_w=40))  # 30 W at the desk is no longer use
    c = responder.gather(night(1), mac="AA", device=loud, readings=desk(), usual=OFF, timers=[],
                         money=MONEY, rejected=[], tz="Asia/Colombo", days=14)  # fmt: skip
    assert c is None


WIDE = Device("Desk 2", policy=Policy(same_within_min=60))  # 45 minutes apart is now a repeat
NEAR = Proposal("AA", "Desk 2", (Change(9, "off", "02:15", "1111111"),), "", 0, 0, 0, "",
                "responder", NOW)  # fmt: skip


def test_the_device_policy_sets_what_gather_drops_as_a_repeat():
    c = responder.gather(night(1), mac="AA", device=WIDE, readings=desk(), usual=OFF, timers=[],
                         money=MONEY, rejected=[NEAR], tz="Asia/Colombo", days=14)  # fmt: skip
    assert c is None


def test_the_device_policy_sets_what_the_checks_block_as_a_repeat(db):
    assert respond(db, Script(GOOD, GOOD), case(WIDE, rejected=[NEAR])) is None
    assert audit(db)[0][2]["check"].startswith("repeat: ")
