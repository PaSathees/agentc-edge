"""Tasmota plugs over HTTP (/cm?cmnd=...). Standard library only, like the logger."""

import http.client
import json
import urllib.request
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from agentc.devices.base import Clock, DeviceError, Info, Timer, TimerSet


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
            raise DeviceError(f"{self.ip}: unreadable Timers answer: {err!r}") from err

    def clock(self) -> Clock:
        tim = self._get("Status 7", "StatusTIM")
        utc = datetime.fromisoformat(tim["UTC"]).replace(tzinfo=UTC)
        return Clock(utc=utc, timezone=tim["Timezone"])

    def _get(self, cmnd: str, key: str) -> dict[str, Any]:
        """Run a command and return one section of its answer."""
        r = self._cmd(cmnd)
        if not isinstance(r.get(key), dict):
            raise DeviceError(f"{self.ip}: {cmnd!r} answered without {key}")
        return r[key]

    def _cmd(self, cmnd: str) -> dict[str, Any]:
        url = f"http://{self.ip}/cm?cmnd={quote(cmnd)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as resp:
                return json.load(resp)
        except (OSError, ValueError, http.client.HTTPException) as err:
            raise DeviceError(f"{self.ip}: {cmnd!r} failed: {err!r}") from err


def _timer(slot: int, t: dict[str, Any]) -> Timer:
    return Timer(
        slot=slot, enable=t["Enable"] == 1, mode=t["Mode"], time=t["Time"],
        window=t["Window"], days=t["Days"], repeat=t["Repeat"] == 1,
        output=t["Output"], action=t["Action"],
    )  # fmt: skip
