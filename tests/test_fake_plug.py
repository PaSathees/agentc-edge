import json
import urllib.request
from typing import Any
from urllib.parse import quote

import pytest

from tests.fake_tasmota import FakePlug


def send(plug: FakePlug, cmnd: str) -> dict[str, Any]:
    """What the device adapter will do in B1: one HTTP GET per command."""
    url = f"http://{plug.ip}/cm?cmnd={quote(cmnd)}"
    with urllib.request.urlopen(url, timeout=2) as r:
        return json.load(r)


def test_reports_power(plug):
    assert send(plug, "Status 8")["StatusSNS"]["ENERGY"]["Power"] == 10.0


def test_lists_16_timers_and_global_switch(plug):
    r = send(plug, "Timers")
    assert r["Timers"] == "ON"
    assert all(f"Timer{n}" in r for n in range(1, 17))


def test_writes_timer9_and_reads_it_back(plug):
    new = {"Enable": 1, "Mode": 0, "Time": "01:30", "Window": 0,
           "Days": "1111111", "Repeat": 1, "Output": 1, "Action": 0}  # fmt: skip
    assert send(plug, f"Timer9 {json.dumps(new)}") == {"Timer9": new}
    assert send(plug, "Timer9") == {"Timer9": new}


def test_partial_write_keeps_other_fields(plug):
    send(plug, 'Timer9 {"Time": "01:30", "Days": "1111111"}')
    send(plug, 'Timer9 {"Enable": 1}')
    t = send(plug, "Timer9")["Timer9"]
    assert (t["Enable"], t["Time"], t["Days"]) == (1, "01:30", "1111111")


def test_owner_slots_untouched_by_a_slot_9_write(plug):
    before = send(plug, "Timer1")
    send(plug, 'Timer9 {"Enable": 1}')
    assert send(plug, "Timer1") == before


def test_offline_plug_fails_like_a_dead_one(plug):
    plug.offline = True
    with pytest.raises(OSError):
        send(plug, "Status 8")
