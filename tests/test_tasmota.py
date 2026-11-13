from datetime import UTC, datetime

import pytest

from agentc.devices.base import DeviceError, Timer
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


def test_offline_plug_raises_device_error(plug):
    plug.offline = True
    with pytest.raises(DeviceError):
        TasmotaPlug(plug.ip, timeout_s=1).timers()


def test_unexpected_answer_raises_device_error(plug, monkeypatch):
    monkeypatch.setattr(plug, "handle", lambda cmnd: {"Command": "Unknown"})
    with pytest.raises(DeviceError):
        TasmotaPlug(plug.ip).clock()


def test_timers_answer_missing_a_slot_raises_device_error(plug, monkeypatch):
    monkeypatch.setattr(plug, "handle", lambda cmnd: {"Timers": "ON", "Timer1": {}})
    with pytest.raises(DeviceError):
        TasmotaPlug(plug.ip).timers()
