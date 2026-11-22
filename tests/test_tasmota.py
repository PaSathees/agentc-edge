from datetime import UTC, datetime

import pytest

from agentc.devices.base import PlugError, PlugRule, Timer
from agentc.devices.tasmota import TasmotaPlug


def test_reads_mac(plug):
    assert TasmotaPlug(plug.ip).info().mac == "AA:BB:CC:00:00:01"


def test_reads_16_timers_and_the_global_switch(plug):
    plug.timers[1].update(Enable=1, Time="07:30", Days="0111110", Repeat=1, Action=1)
    ts = TasmotaPlug(plug.ip).timers()
    assert ts.enabled is True
    assert [t.slot for t in ts.timers] == list(range(1, 17))
    assert ts.timers[0] == Timer(
        slot=1, enable=True, mode=0, time="07:30", window=0,
        days="0111110", repeat=True, output=1, action=1,
    )  # fmt: skip


def test_global_switch_off_is_reported(plug):
    plug.timers_on = False
    assert TasmotaPlug(plug.ip).timers().enabled is False


def test_reads_the_clock_in_utc_with_the_timezone(plug):
    clock = TasmotaPlug(plug.ip).clock()
    assert clock.utc == datetime(2026, 11, 13, 7, 8, 23, tzinfo=UTC)
    assert clock.timezone == "+05:30"


def test_offline_plug_raises_plug_error(plug):
    plug.offline = True
    with pytest.raises(PlugError):
        TasmotaPlug(plug.ip, timeout_s=1).timers()


def test_unexpected_answer_raises_plug_error(plug, monkeypatch):
    monkeypatch.setattr(plug, "handle", lambda cmnd: {"Command": "Unknown"})
    with pytest.raises(PlugError):
        TasmotaPlug(plug.ip).clock()


def test_timers_answer_missing_a_slot_raises_plug_error(plug, monkeypatch):
    monkeypatch.setattr(plug, "handle", lambda cmnd: {"Timers": "ON", "Timer1": {}})
    with pytest.raises(PlugError):
        TasmotaPlug(plug.ip).timers()


# automations on the plug itself: auto-off and rules (3b2)


@pytest.mark.parametrize(("pulse", "seconds"), [(0, None), (14500, 14400), (50, 5.0), (112, 12)])
def test_reads_the_auto_off_in_seconds(plug, pulse, seconds):  # 14500 = 4 h, the battery charger
    plug.pulse_set = pulse
    assert TasmotaPlug(plug.ip).automations().auto_off_s == seconds


def test_reads_only_the_rules_that_are_set(plug):
    plug.rules[1] = ("ON Time#Initialized DO Power 1 ENDON", True)
    plug.rules[3] = ("ON Power1#State DO Var1 1 ENDON", False)
    assert TasmotaPlug(plug.ip).automations().rules == (
        PlugRule(1, True, "ON Time#Initialized DO Power 1 ENDON"),
        PlugRule(3, False, "ON Power1#State DO Var1 1 ENDON"),
    )


def test_an_unreadable_automations_answer_raises_plug_error(plug, monkeypatch):
    monkeypatch.setattr(plug, "handle", lambda cmnd: {"Command": "Unknown"})
    with pytest.raises(PlugError):
        TasmotaPlug(plug.ip).automations()


def test_an_automations_answer_missing_a_field_raises_plug_error(plug, monkeypatch):
    monkeypatch.setattr(plug, "handle", lambda cmnd: {"PulseTime1": {"Remaining": 0}})
    with pytest.raises(PlugError, match="unreadable PulseTime or Rule"):
        TasmotaPlug(plug.ip).automations()
