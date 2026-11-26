"""The Actuator: the only code that switches a plug, and only after every check (FR-20, FR-32)."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agentc import actuator, config, store
from agentc.config import Device
from agentc.devices.base import PlugError, Timer
from agentc.house import Card
from tests.test_cli import EXAMPLE

NOW = datetime(2026, 11, 2, 1, 30, tzinfo=UTC)  # 07:00 in Colombo


@pytest.fixture
def home(tmp_path, plug, monkeypatch):
    """app.db with Plug-6 seen, its device set to write, shadow mode off, a login set; the
    fake plug on."""
    monkeypatch.setenv("AGENTC_PASSWORD", "pw")
    base = config.load(EXAMPLE)
    cfg = replace(base, plugs={"Plug-6": plug.ip},
                  devices={"Plug-6": Device("Desk 2", access="write")})  # fmt: skip
    app = store.connect(tmp_path / "app.db")
    store.add_site(app, name=cfg.site, tz=cfg.tz)
    store.plug_seen(app, site_id=1, mac="AA", name="Plug-6", ip=plug.ip, timers_on=True, now=NOW)
    store.set_shadow(app, site_id=1, on=False, now=NOW)
    plug.relay_on = True
    return app, cfg, plug


def off(app, cfg, now=NOW, **kw):
    return actuator.switch(app, cfg, site_id=1, plug="Plug-6", on=False, why="question:1",
                           now=now, **kw)  # fmt: skip


def rows(app, action):
    sql = "SELECT ref, detail FROM audit WHERE action = ? ORDER BY id"
    return [(ref, json.loads(d)) for ref, d in app.execute(sql, (action,))]


def test_a_switch_off_is_sent_read_back_and_audited(home):
    app, cfg, plug = home
    assert off(app, cfg) == "Done: Desk 2 is off."
    assert not plug.relay_on and plug.commands == ["Power Off", "Power"]
    assert rows(app, "write") == [("plug:Plug-6", {"does": "switch_off", "why": "question:1"})]


def test_switch_on_works_the_same_way(home):
    app, cfg, plug = home
    plug.relay_on = False
    r = actuator.switch(app, cfg, site_id=1, plug="Plug-6", on=True, why="undo:1", now=NOW)
    assert (r, plug.relay_on) == ("Done: Desk 2 is on.", True)


def test_shadow_mode_records_and_sends_nothing(home):
    app, cfg, plug = home
    store.set_shadow(app, site_id=1, on=True, now=NOW)
    assert (
        off(app, cfg) == "Recorded: switch Desk 2 off. Shadow mode: nothing was sent to the plug."
    )
    assert plug.commands == [] and plug.relay_on
    assert rows(app, "shadow_action") == [
        ("question:1", {"does": "switch_off", "why": "question:1", "plug": "Plug-6"})
    ]


def test_shadow_mode_is_on_until_the_owner_turns_it_off(tmp_path):
    app = store.connect(tmp_path / "app.db")
    store.add_site(app, name="home", tz="Asia/Colombo")
    assert store.shadow(app, site_id=1)
    store.set_shadow(app, site_id=1, on=False, now=NOW)
    store.set_shadow(app, site_id=1, on=False, now=NOW)  # twice: still one setting
    assert not store.shadow(app, site_id=1)
    assert [d for _, d in rows(app, "shadow")] == [{"shadow": "off"}] * 2


def test_a_device_at_advice_is_recorded_with_how_to_allow_it(home):
    app, cfg, plug = home
    cfg = replace(cfg, devices={"Plug-6": Device("Desk 2")})
    assert off(app, cfg) == ("Recorded: switch Desk 2 off. Desk 2 is set to advice, so nothing"
                             " was sent. To let me change it, set its access to write in"
                             " config.toml.")  # fmt: skip
    assert plug.commands == []


@pytest.mark.parametrize(
    ("device", "card", "why"),
    [(Device("Desk 2", access="none"), Card(), "Not sent: Desk 2 is never changed (its access"
      " is none)."),
     (Device("Desk 2", access="write"), Card("fridge"), "Not sent: Desk 2 must always run, so"
      " it is never switched off.")],
)  # fmt: skip
def test_never_sent_to_access_none_or_a_must_run_device(home, device, card, why):
    app, cfg, plug = home
    store.save_card(app, site_id=1, mac="AA", card=card, now=NOW)
    assert off(app, replace(cfg, devices={"Plug-6": device})) == why
    assert plug.commands == [] and rows(app, "write_refused")[0][1]["reason"] == why


def test_a_must_run_device_can_still_be_switched_on(home):
    app, cfg, plug = home
    store.save_card(app, site_id=1, mac="AA", card=Card("fridge"), now=NOW)
    plug.relay_on = False
    r = actuator.switch(app, cfg, site_id=1, plug="Plug-6", on=True, why="undo:1", now=NOW)
    assert r == "Done: Desk 2 is on."


def test_at_most_4_writes_a_plug_a_day_failed_ones_too(home):
    app, cfg, plug = home
    plug.offline = True
    for n in range(actuator.WRITES_A_DAY):
        assert off(app, cfg, now=NOW + timedelta(minutes=n),
                   connect=lambda ip: _offline()).startswith("Could not")  # fmt: skip
    assert off(app, cfg) == "Not sent: Desk 2 was changed 4 times today already."
    tomorrow = NOW + timedelta(days=1)  # a new local day
    plug.offline = False
    assert off(app, cfg, now=tomorrow) == "Done: Desk 2 is off."


def test_the_day_starts_at_local_midnight(home):
    app, cfg, _ = home
    late = datetime(2026, 11, 1, 18, 0, tzinfo=UTC)  # 23:30 in Colombo, the day before NOW
    for _ in range(actuator.WRITES_A_DAY):
        store.add_audit(app, site_id=1, at=late, actor="code", action="write", ref="plug:Plug-6",
                        detail={"does": "switch_off"})  # fmt: skip
    assert off(app, cfg) == "Done: Desk 2 is off."  # yesterday's 4 do not count


def test_a_plug_that_does_not_answer_is_a_failure_and_a_note(home):
    app, cfg, plug = home
    r = off(app, cfg, connect=lambda ip: _offline())
    assert r == ("Could not switch Desk 2 off: the plug did not answer (gone). It may be off:"
                 " Switch back on is in the inbox.")  # fmt: skip
    assert rows(app, "write_failed")[0][1]["error"] == r and rows(app, "note") == [
        ("plug:Plug-6", {"text": r})
    ]
    assert rows(app, "write") == []


def test_a_relay_that_did_not_change_is_caught_by_the_read_back(home):
    app, cfg, plug = home
    plug.stuck = True
    assert off(app, cfg) == "Could not switch Desk 2 off: the plug still says on."
    assert rows(app, "write") == [] and len(rows(app, "write_failed")) == 1


def test_undoable_lists_switch_offs_with_nothing_written_after(home):
    app, cfg, plug = home
    off(app, cfg)
    ((first, _),) = store.undoable(app, site_id=1, since=NOW - timedelta(hours=1))
    assert store.undoable(app, site_id=1, since=NOW + timedelta(seconds=1)) == []
    actuator.switch(app, cfg, site_id=1, plug="Plug-6", on=True, why=f"undo:{first}", now=NOW)
    assert store.undoable(app, site_id=1, since=NOW - timedelta(hours=1)) == []


class _offline:
    """A plug that never answers: every call fails as a real one would."""

    def _gone(self, *args):
        raise PlugError("gone")

    info = timers = clock = automations = power = switch = set_timer = _gone


# a timer pair, all or nothing (4c)


def timer(app, cfg, off="01:30", on="07:00", days="weekdays", **kw):
    return actuator.set_timer(app, cfg, site_id=1, plug="Plug-6", off=off, on=on, days=days,
                              why="question:2", now=NOW, **kw)  # fmt: skip


def test_a_timer_pair_goes_in_two_free_slots_on_first_then_read_back(home):
    app, cfg, plug = home
    plug.timers[9].update(Enable=1, Time="23:00")  # a slot in use: skipped
    r = timer(app, cfg)
    assert r == ("Done: Desk 2 now switches off at 01:30, on at 07:00, Monday to Friday"
                 " (timers 10 and 11).")  # fmt: skip
    on, off = plug.timers[10], plug.timers[11]
    assert (on["Time"], on["Action"], off["Time"], off["Action"]) == ("07:00", 1, "01:30", 0)
    assert on["Days"] == off["Days"] == "0111110" and on["Enable"] == off["Enable"] == 1
    sent = [c.split()[0] for c in plug.commands]
    assert sent == ["Timers", "Timer10", "Timers", "Timer11", "Timers"]  # on first, each read
    assert {t.slot: t.time for t in store.load_timers(app, mac="AA") if t.enable} == {
        9: "23:00",
        10: "07:00",
        11: "01:30",
    }
    why = store.load_card(app, mac="AA").why
    assert why[10] == why[11] == ("set by AgentC after your tap: off at 01:30, on at 07:00,"
                                  " Monday to Friday")  # fmt: skip
    ((_, d),) = rows(app, "write")
    old, new = json.loads(d.pop("old")), json.loads(d.pop("new"))
    assert [(t["slot"], t["time"]) for t in new] == [(10, "07:00"), (11, "01:30")]
    assert d == {"does": "set_timer", "why": "question:2", "off": "01:30", "on": "07:00",
                 "days": "weekdays", "slots": "10,11"}  # fmt: skip
    assert [(t["slot"], t["enable"]) for t in old] == [(10, False), (11, False)]  # to put back


def test_the_owners_own_slots_are_never_used(home):
    app, cfg, plug = home
    for n in range(9, 16):  # 9 to 15 in use: only 16 is free
        plug.timers[n]["Enable"] = 1
    assert timer(app, cfg) == "Not sent: Desk 2: it has no 2 free timer slots of 9 to 16."
    assert [c for c in plug.commands if c != "Timers"] == []  # slots 1 to 8 stayed free


def test_no_timer_on_a_plug_with_its_timers_switch_off(home):
    app, cfg, plug = home
    plug.timers_on = False
    r = timer(app, cfg)
    assert r.startswith("Not sent: Desk 2: its Timers switch is off, so no timer would fire.")
    assert plug.commands == ["Timers"]


def test_a_lost_write_puts_the_old_timers_back(home):
    app, cfg, plug = home
    plug.lost = {10}  # the off timer is answered, but not kept
    r = timer(app, cfg)
    assert r.startswith("Could not set Desk 2's timer") and r.endswith(
        "The old timers were put back."
    )
    assert plug.timers[9]["Enable"] == 0 and plug.timers[10]["Enable"] == 0  # as before
    assert rows(app, "write") == [] and len(rows(app, "note")) == 1


def test_if_putting_back_fails_too_the_owner_is_told(home):
    app, cfg, plug = home
    real = actuator.TasmotaPlug(plug.ip)

    class Breaks:  # reads work; the second timer write and the put back fail
        def __init__(self):
            self.writes = 0

        def timers(self):
            return real.timers()

        def set_timer(self, t):
            self.writes += 1
            if self.writes > 1:
                raise PlugError("gone")
            real.set_timer(t)

    r = timer(app, cfg, connect=lambda ip: Breaks())
    assert r.endswith("Putting the old timers back failed too: check the plug's Timers page.")


@pytest.mark.parametrize(
    ("args", "why"),
    [({"off": "1:30"}, "Not sent: Desk 2's timer needs HH:MM times and known days."),
     ({"days": "sometimes"}, "Not sent: Desk 2's timer needs HH:MM times and known days.")],
)  # fmt: skip
def test_a_bad_timer_is_refused_before_the_plug(home, args, why):
    app, cfg, plug = home
    assert timer(app, cfg, **args) == why and plug.commands == []


def test_a_hard_rule_is_checked_again_at_write_time(home):
    app, cfg, plug = home
    late = config.Rule("latest_off", "02:00", True, "Desk 2 may run until 02:00")
    cfg = replace(cfg, devices={"Plug-6": Device("Desk 2", access="write", rules=(late,))})
    assert timer(app, cfg) == "Not sent: Desk 2 may run until 02:00."
    assert plug.commands == []


def test_no_timer_ever_switches_off_a_must_run_device(home):
    app, cfg, plug = home
    store.save_card(app, site_id=1, mac="AA", card=Card("router"), now=NOW)
    assert timer(app, cfg).startswith("Not sent: Desk 2 must always run")


def test_a_timer_in_shadow_mode_is_recorded(home):
    app, cfg, plug = home
    store.set_shadow(app, site_id=1, on=True, now=NOW)
    said = timer(app, cfg)
    assert said == ("Recorded: set Desk 2's timer (off at 01:30, on at 07:00, Monday to"
                    " Friday). Shadow mode: nothing was sent to the plug.")  # fmt: skip


def test_a_plug_that_does_not_answer_gets_no_timer(home):
    app, cfg, plug = home
    plug.offline = True
    r = timer(app, cfg, connect=lambda ip: actuator.TasmotaPlug(ip, timeout_s=0.5))
    assert r.startswith("Could not set Desk 2's timer") and "did not answer" in r


def test_days_read_back_as_letters_still_match():
    want = Timer(9, True, 0, "07:00", 0, "0111110", True, 1, 1)
    assert actuator._same(replace(want, days="-MTWTF-"), want)  # pyright: ignore[reportPrivateUsage]
    assert not actuator._same(replace(want, days="SMTWTF-"), want)  # pyright: ignore[reportPrivateUsage]


# from the review before merge


def test_with_no_login_set_nothing_is_written_even_with_shadow_mode_off(home, monkeypatch):
    app, cfg, plug = home
    monkeypatch.delenv("AGENTC_PASSWORD")
    assert off(app, cfg) == ("Recorded: switch Desk 2 off. No login is set in .env, so nothing"
                             " was sent to the plug.")  # fmt: skip
    assert plug.commands == []


def test_undo_works_in_shadow_mode_and_without_a_login(home, monkeypatch):
    app, cfg, plug = home
    off(app, cfg)
    store.set_shadow(app, site_id=1, on=True, now=NOW)  # the owner stops everything
    monkeypatch.delenv("AGENTC_PASSWORD")
    r = actuator.switch(app, cfg, site_id=1, plug="Plug-6", on=True, why="undo:2", now=NOW)
    assert (r, plug.relay_on) == ("Done: Desk 2 is on.", True)
    on = actuator.switch(app, cfg, site_id=1, plug="Plug-6", on=True, why="question:3", now=NOW)
    assert on.startswith("Recorded")  # only an undo skips shadow mode


def test_a_failed_undo_keeps_its_button(home):
    app, cfg, plug = home
    off(app, cfg)
    ((wid, _),) = store.undoable(app, site_id=1, since=NOW)
    plug.offline = True
    actuator.switch(app, cfg, site_id=1, plug="Plug-6", on=True, why=f"undo:{wid}", now=NOW,
                    connect=lambda ip: actuator.TasmotaPlug(ip, timeout_s=0.5))  # fmt: skip
    assert store.undoable(app, site_id=1, since=NOW) == [(wid, "Plug-6")]


def test_a_switch_off_that_lost_its_read_back_can_be_undone(home):
    app, cfg, plug = home
    off(app, cfg, connect=lambda ip: _offline())
    assert [p for _, p in store.undoable(app, site_id=1, since=NOW)] == ["Plug-6"]


def test_a_put_back_that_does_not_read_back_is_reported(home):
    app, cfg, plug = home
    plug.lost = {10}  # the off timer is lost, so the "on" timer in 9 must go back
    real = actuator.TasmotaPlug(plug.ip)

    class Drifts:  # the put back of slot 9 is taken, but changed on the way
        def timers(self):
            return real.timers()

        def set_timer(self, t):
            real.set_timer(replace(t, time="06:00") if t.slot == 9 and not t.enable else t)

    r = timer(app, cfg, connect=lambda ip: Drifts())
    assert r.endswith("Putting the old timers back failed too: check the plug's Timers page.")


# a plan item's Do it: one timer moved, and put back (5b5)


def owners(app, plug, slot=2, time="06:00", action=1):
    """The owner's own clock timer on the plug, as the plan saw it (stored)."""
    plug.timers[slot].update(Enable=1, Time=time, Days="1111111", Repeat=1, Action=action)
    found = actuator.TasmotaPlug(plug.ip).timers()
    store.save_timers(app, site_id=1, mac="AA", timers=found.timers, now=NOW)


def move(app, cfg, slot=2, time="07:30", **kw):
    return actuator.move_timer(app, cfg, site_id=1, plug="Plug-6", slot=slot, time=time,
                               why="plan:1", now=NOW, **kw)  # fmt: skip


def test_the_owners_own_timer_is_moved_read_back_and_put_back(home):
    app, cfg, plug = home
    owners(app, plug)
    assert move(app, cfg) == "Done: Desk 2's timer 2 now runs at 07:30 (was 06:00)."
    assert plug.timers[2]["Time"] == "07:30" and plug.timers[2]["Action"] == 1
    assert {t.slot: t.time for t in store.load_timers(app, mac="AA") if t.enable} == {2: "07:30"}
    assert store.load_card(app, mac="AA").why[2] == ("moved to 07:30 by AgentC after your tap"
                                                     " (was 06:00)")  # fmt: skip
    ((wid, p, d, _),) = store.timer_undoable(app, site_id=1, since=NOW - timedelta(hours=1))
    assert (p, d["does"], d["slot"]) == ("Plug-6", "move_timer", "2")
    store.set_shadow(app, site_id=1, on=True, now=NOW)  # an undo still goes through
    r = actuator.put_back(app, cfg, site_id=1, plug=p, write_id=wid, old=d["old"], now=NOW)
    assert r == "Done: Desk 2's timers are back as they were."
    assert plug.timers[2]["Time"] == "06:00" and 2 not in store.load_card(app, mac="AA").why
    assert store.timer_undoable(app, site_id=1, since=NOW - timedelta(hours=1)) == []


def test_a_pair_is_put_back_to_empty_slots(home):
    app, cfg, plug = home
    timer(app, cfg)
    ((wid, _, d, _),) = store.timer_undoable(app, site_id=1, since=NOW - timedelta(hours=1))
    actuator.put_back(app, cfg, site_id=1, plug="Plug-6", write_id=wid, old=d["old"], now=NOW)
    assert plug.timers[10]["Enable"] == plug.timers[11]["Enable"] == 0
    assert store.load_card(app, mac="AA").why == {}


def test_a_timer_changed_since_the_plan_is_not_moved(home):
    app, cfg, plug = home
    owners(app, plug)
    plug.timers[2]["Time"] = "06:15"  # the owner changed it on the plug
    assert move(app, cfg).startswith("Not sent: Desk 2's timer 2 changed since the plan")
    assert plug.timers[2]["Time"] == "06:15" and rows(app, "write") == []


@pytest.mark.parametrize(
    ("slot", "time", "why"),
    [(3, "07:30", "Not sent: Desk 2 has no clock timer 3 to move."),
     (2, "7:30", "Not sent: Desk 2's timer needs an HH:MM time.")],
)  # fmt: skip
def test_a_missing_timer_or_a_bad_time_is_refused_before_the_plug(home, slot, time, why):
    app, cfg, plug = home
    owners(app, plug)
    plug.commands.clear()
    assert move(app, cfg, slot=slot, time=time) == why and plug.commands == []


def test_an_off_timer_moved_against_a_hard_rule_is_refused(home):
    app, cfg, plug = home
    late = config.Rule("latest_off", "02:00", True, "Desk 2 may run until 02:00")
    cfg = replace(cfg, devices={"Plug-6": Device("Desk 2", access="write", rules=(late,))})
    owners(app, plug, time="02:30", action=0)
    assert move(app, cfg, time="01:30") == "Not sent: Desk 2 may run until 02:00."
    owners(app, plug, slot=4, time="06:00", action=1)  # an on timer: the rule is about off
    assert move(app, cfg, slot=4, time="01:30").startswith("Done")


def test_shadow_mode_records_a_move(home):
    app, cfg, plug = home
    owners(app, plug)
    store.set_shadow(app, site_id=1, on=True, now=NOW)
    assert move(app, cfg).startswith("Recorded: move Desk 2's timer 2 to 07:30.")
    assert plug.timers[2]["Time"] == "06:00"


def test_a_lost_move_puts_the_old_time_back(home):
    app, cfg, plug = home
    owners(app, plug)
    plug.lost = {2}
    r = move(app, cfg)
    assert r.startswith("Could not move Desk 2's timer 2 to 07:30: timer 2 reads back as 06:00")
    assert r.endswith("The old timers were put back.") and plug.timers[2]["Time"] == "06:00"


def test_a_plug_that_does_not_answer_moves_and_puts_back_nothing(home):
    app, cfg, plug = home
    owners(app, plug)
    plug.offline = True
    assert move(app, cfg).startswith("Could not move Desk 2's timer 2 to 07:30: the plug did not")
    old = json.dumps([{"slot": 2, "enable": True, "mode": 0, "time": "06:00", "window": 0,
                       "days": "1111111", "repeat": True, "output": 1, "action": 1}])  # fmt: skip
    r = actuator.put_back(app, cfg, site_id=1, plug="Plug-6", write_id=1, old=old, now=NOW)
    assert r.startswith("Could not put Desk 2's timers back: the plug did not answer")
    plug.offline = False
    plug.lost = {2}
    plug.timers[2]["Time"] = "09:00"
    r = actuator.put_back(app, cfg, site_id=1, plug="Plug-6", write_id=1, old=old, now=NOW)
    assert (
        r == "Could not put Desk 2's timers back: they read back different. Check the plug's"
        " Timers page."
    )


def test_a_device_set_to_none_is_never_put_back_either(home):
    app, cfg, plug = home
    cfg = replace(cfg, devices={"Plug-6": Device("Desk 2", access="none")})
    r = actuator.put_back(app, cfg, site_id=1, plug="Plug-6", write_id=1, old="[]", now=NOW)
    assert r == "Not sent: Desk 2 is never changed (its access is none)." and plug.commands == []


def test_a_pair_on_any_days_goes_in_with_its_own_days(home):
    app, cfg, plug = home
    r = timer(app, cfg, days="1111100")  # Sunday to Thursday
    assert r == ("Done: Desk 2 now switches off at 01:30, on at 07:00, Sunday to Thursday"
                 " (timers 9 and 10).")  # fmt: skip
    assert plug.timers[9]["Days"] == plug.timers[10]["Days"] == "1111100"
    assert timer(app, cfg, days="0000000").endswith("needs HH:MM times and known days.")
