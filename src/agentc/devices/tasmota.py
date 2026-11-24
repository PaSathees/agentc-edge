"""Tasmota plugs over HTTP (/cm?cmnd=...). Standard library only, like the logger."""

import http.client
import ipaddress
import json
import socket
import urllib.request
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from agentc.devices.base import Automations, Clock, Info, PlugError, PlugRule, Timer, TimerSet


class TasmotaPlug:
    def __init__(self, ip: str, timeout_s: float = 5.0) -> None:
        self.ip = ip
        self.timeout_s = timeout_s

    def info(self) -> Info:
        net = self._get("Status 5", "StatusNET")
        return Info(mac=net["Mac"], hostname=net["Hostname"])

    def timers(self) -> TimerSet:
        r = self._cmd("Timers")
        try:
            timers = [_timer(n, r[f"Timer{n}"]) for n in range(1, 17)]
            return TimerSet(enabled=r["Timers"] == "ON", timers=timers)
        except (KeyError, TypeError, ValueError) as err:
            raise PlugError(f"{self.ip}: unreadable Timers answer: {err!r}") from err

    def clock(self) -> Clock:
        tim = self._get("Status 7", "StatusTIM")
        try:
            utc = datetime.fromisoformat(tim["UTC"]).replace(tzinfo=UTC)
            return Clock(utc=utc, timezone=tim["Timezone"])
        except (KeyError, TypeError, ValueError) as err:
            raise PlugError(f"{self.ip}: unreadable clock answer: {err!r}") from err

    def automations(self) -> Automations:
        """Auto-off (PulseTime1) and the 3 rules. Asking with no value only reads them."""
        try:
            pulse = self._get("PulseTime1", "PulseTime1")["Set"]
            rules = []
            for n in (1, 2, 3):
                r = self._get(f"Rule{n}", f"Rule{n}")
                if r["Rules"]:
                    rules.append(PlugRule(n, r["State"] == "ON", r["Rules"]))
        except (KeyError, TypeError) as err:
            raise PlugError(f"{self.ip}: unreadable PulseTime or Rule answer: {err!r}") from err
        return Automations(_pulse_seconds(pulse), tuple(rules))

    def power(self) -> bool:
        """Is the relay on? Asking with no value only reads it."""
        return self._relay(self._cmd("Power"))

    def switch(self, on: bool) -> None:
        """Switch the relay on or off. Read it back with power(): the answer here is not proof."""
        self._relay(self._cmd(f"Power {'On' if on else 'Off'}"))

    def _relay(self, r: dict[str, Any]) -> bool:
        if r.get("POWER") not in ("ON", "OFF"):
            raise PlugError(f"{self.ip}: unreadable Power answer: {r!r}")
        return r["POWER"] == "ON"

    def _get(self, cmnd: str, key: str) -> dict[str, Any]:
        """Run a command and return one section of its answer."""
        r = self._cmd(cmnd)
        if not isinstance(r.get(key), dict):
            raise PlugError(f"{self.ip}: {cmnd!r} answered without {key}")
        return r[key]

    def _cmd(self, cmnd: str) -> dict[str, Any]:
        url = f"http://{self.ip}/cm?cmnd={quote(cmnd)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as resp:
                return json.load(resp)
        except (OSError, ValueError, http.client.HTTPException) as err:
            raise PlugError(f"{self.ip}: {cmnd!r} failed: {err!r}") from err


def _timer(slot: int, t: dict[str, Any]) -> Timer:
    return Timer(
        slot=slot, enable=t["Enable"] == 1, mode=t["Mode"], time=t["Time"],
        window=t["Window"], days=t["Days"], repeat=t["Repeat"] == 1,
        output=t["Output"], action=t["Action"],
    )  # fmt: skip


def _pulse_seconds(value: int) -> float | None:
    """Tasmota's PulseTime: 0 = none, 1 to 111 = tenths of a second, more = seconds + 100."""
    if value == 0:
        return None
    return value / 10 if value <= 111 else value - 100


def find(
    hosts: Iterable[str], *, timeout_s: float = 1.0, workers: int = 64
) -> list[tuple[str, Info]]:
    """The hosts that answer as Tasmota plugs, with their hostname and MAC. Reads only."""

    def ask(host: str) -> tuple[str, Info] | None:
        try:
            return host, TasmotaPlug(host, timeout_s).info()
        except PlugError:
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [found for found in pool.map(ask, hosts) if found]


def this_ip() -> str:
    """This machine's address on the home network. No packet is sent: connecting a UDP
    socket only picks the route."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))  # a documentation address: never reached
        return s.getsockname()[0]


def home_network() -> ipaddress.IPv4Network:
    """The home network around this machine, as a /24 (e.g. 192.168.1.0/24)."""
    return ipaddress.IPv4Network(f"{this_ip()}/24", strict=False)
