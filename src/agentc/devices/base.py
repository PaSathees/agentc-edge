"""What every device adapter returns. Tasmota now; MQTT or Modbus later (M-03)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class DeviceError(Exception):
    """The device did not answer, or answered with something we can't read."""


@dataclass(frozen=True)
class Info:
    mac: str
    hostname: str


@dataclass(frozen=True)
class Timer:
    """One of a plug's 16 timers, fields as Tasmota names them."""

    slot: int
    enable: bool
    mode: int  # 0 time, 1 sunrise, 2 sunset
    time: str  # HH:MM, plug local time
    window: int  # random offset, minutes
    days: str  # 7 chars, Sunday first; "1" or a letter = on
    repeat: bool
    output: int
    action: int  # 0 off, 1 on, 2 toggle, 3 rule


@dataclass(frozen=True)
class TimerSet:
    enabled: bool  # the plug's global Timers switch: off means no timer fires
    timers: list[Timer]


@dataclass(frozen=True)
class Clock:
    utc: datetime  # the plug's own idea of now
    timezone: str  # e.g. "+05:30"


class Device(Protocol):
    def info(self) -> Info: ...
    def timers(self) -> TimerSet: ...
    def clock(self) -> Clock: ...
