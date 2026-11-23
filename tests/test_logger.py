"""The logger (`agentc log`) and finding plugs (`agentc find`), against the fake plug."""

import ipaddress
import sqlite3

import pytest

from agentc import logger
from agentc.devices import tasmota
from tests.fake_tasmota import FakePlug


@pytest.fixture
def plug():
    p = FakePlug(power_w=42.0, mac="AA:00:00:00:00:07")
    p.start()
    yield p
    p.stop()


def test_a_read_gives_the_plugs_energy(plug):
    row = logger.read_plug("Plug-7", plug.ip, "2026-11-23T10:00:00+00:00")
    assert row == ("2026-11-23T10:00:00+00:00", "Plug-7", "2026-11-12T23:00:00", 42.0, 42.0, 0,
                   1.0, 230, 0.0, 1.0, None)  # fmt: skip


def test_a_plug_that_does_not_answer_is_a_row_with_an_error():
    row = logger.read_plug("Plug-9", "127.0.0.1:1", "t", timeout_s=1)
    assert row[:3] == ("t", "Plug-9", None) and set(row[3:10]) == {None}
    assert str(row[10]).startswith("URLError")


def test_each_round_saves_one_row_per_plug_on_the_minute(plug, tmp_path, capsys):
    now = [1_000_030.0]  # 30 s into a minute

    def sleep(s):
        now[0] += s

    plugs = {"Plug-7": plug.ip, "Plug-9": "127.0.0.1:1"}
    logger.run(tmp_path / "energy.db", plugs, rounds=2, sleep=sleep, clock=lambda: now[0])
    rows = (
        sqlite3.connect(tmp_path / "energy.db")
        .execute("SELECT ts_utc, plug, power_w, error IS NULL FROM readings ORDER BY ts_utc, plug")
        .fetchall()
    )
    assert [r[0][11:19] for r in rows] == [
        "13:48:00",
        "13:48:00",
        "13:49:00",
        "13:49:00",
    ]  # the next whole minutes
    assert [(r[1], r[2], r[3]) for r in rows[:2]] == [("Plug-7", 42.0, 1), ("Plug-9", None, 0)]
    assert "ok=1 failed=['Plug-9']" in capsys.readouterr().out


def test_finding_asks_each_address_and_keeps_the_tasmota_plugs(plug):
    found = tasmota.find([plug.ip, "127.0.0.1:1"], timeout_s=1)
    assert [(ip, info.hostname, info.mac) for ip, info in found] == [
        (plug.ip, "fake-plug", "AA:00:00:00:00:07")]  # fmt: skip


def test_the_home_network_is_this_machines_24():
    net = tasmota.home_network()
    assert isinstance(net, ipaddress.IPv4Network) and net.prefixlen == 24
