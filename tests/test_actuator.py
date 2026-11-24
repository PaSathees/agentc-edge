"""The Actuator: the only code that switches a plug, and only after every check (FR-20, FR-32)."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agentc import actuator, config, store
from agentc.config import Device
from agentc.devices.base import PlugError
from agentc.house import Card
from tests.test_cli import EXAMPLE

NOW = datetime(2026, 11, 2, 1, 30, tzinfo=UTC)  # 07:00 in Colombo


@pytest.fixture
def home(tmp_path, plug):
    """app.db with Plug-6 seen, its device set to write, shadow mode off; the fake plug on."""
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
                             " was sent. To let me switch it, set its access to write in"
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
    assert r == "Could not switch Desk 2 off: the plug did not answer (gone)."
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
    def switch(self, on):
        raise PlugError("gone")

    def power(self):  # pragma: no cover (never reached)
        raise PlugError("gone")
