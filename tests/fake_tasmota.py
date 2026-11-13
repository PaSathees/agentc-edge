"""A fake Tasmota plug for tests. Same HTTP API as a real one (/cm?cmnd=...).

Response shapes were copied from a real plug of the reference home (2026-11-12).
State lives in memory; `offline = True` makes it drop every request.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

EMPTY_TIMER = {
    "Enable": 0, "Mode": 0, "Time": "00:00", "Window": 0,
    "Days": "0000000", "Repeat": 0, "Output": 1, "Action": 0,
}  # fmt: skip


class FakePlug:
    def __init__(self, power_w: float = 0.0, mac: str = "AA:BB:CC:00:00:01") -> None:
        self.power_w = power_w
        self.mac = mac
        self.utc = "2026-11-13T07:08:23"  # the plug's own clock; tests can skew it
        self.local = "2026-11-13T12:38:23"
        self.relay_on = False
        self.timers_on = True
        self.timezone = "+05:30"
        self.timers = {n: dict(EMPTY_TIMER) for n in range(1, 17)}
        self.offline = False
        self.commands: list[str] = []  # every command received, in order
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        self.ip = f"127.0.0.1:{self._server.server_port}"  # same use as a real IP

    def start(self) -> None:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def handle(self, cmnd: str) -> dict[str, Any]:
        """Answer one command the way Tasmota does."""
        self.commands.append(cmnd)
        name, _, arg = cmnd.partition(" ")
        key = name.lower()
        if key == "power":
            if arg:
                self.relay_on = arg.upper() in ("1", "ON")
            return {"POWER": "ON" if self.relay_on else "OFF"}
        if key == "status" and arg == "8":
            energy = {"Power": self.power_w, "ApparentPower": self.power_w, "ReactivePower": 0,
                      "Factor": 1.0, "Voltage": 230, "Current": 0.0, "Total": 1.0}  # fmt: skip
            return {"StatusSNS": {"Time": "2026-11-12T23:00:00", "ENERGY": energy}}
        if key == "status" and arg == "5":
            return {"StatusNET": {"Hostname": "fake-plug", "Mac": self.mac}}
        if key == "status" and arg == "7":
            return {"StatusTIM": {"UTC": self.utc, "Local": self.local, "Timezone": self.timezone}}
        if key == "timers":
            return {"Timers": "ON" if self.timers_on else "OFF",
                    **{f"Timer{n}": t for n, t in self.timers.items()}}  # fmt: skip
        if (n := key.removeprefix("timer")).isdigit():  # Timer1 .. Timer16
            return self._timer(int(n), arg)
        if key == "timezone":
            return {"Timezone": self.timezone}
        return {"Command": "Unknown"}

    def _timer(self, n: int, arg: str) -> dict[str, Any]:
        if not 1 <= n <= 16:
            return {"Command": "Error"}
        if arg:
            self.timers[n].update(json.loads(arg))
        return {f"Timer{n}": self.timers[n]}


def _handler_for(plug: FakePlug) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if plug.offline:
                self.close_connection = True  # drop it: the client sees a dead connection
                return
            cmnd = parse_qs(urlparse(self.path).query).get("cmnd", [""])[0]
            body = json.dumps(plug.handle(cmnd)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # keep test output quiet
            pass

    return Handler
