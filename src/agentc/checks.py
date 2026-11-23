"""Checks shared by every agent (guardrails, layer 3): numbers must come from the facts,
and an off time must keep the owner's rules. Code, after the model."""

import re

from agentc.config import Rule

NUMBER = re.compile(r"\d+(?:[.:]\d+)?")  # 12, 0.015, 01:30
DAY = 24 * 60  # minutes


def made_up(text: str, facts: str) -> list[str]:
    """Numbers in text that are not in the facts. Times and dates count (NFR-11)."""
    known = {_norm(n) for n in NUMBER.findall(facts)}
    return [n for n in NUMBER.findall(text) if _norm(n) not in known]


def _norm(n: str) -> str:
    """One spelling per number: 7:00 = 07:00, 1.50 = 1.5, 03 = 3."""
    if ":" in n:
        h, m = n.split(":")
        return f"{int(h):02d}:{m}"
    return str(float(n))


def breaks(off: str, rule: Rule) -> bool:
    """An off time in the 12 hours before the time the owner said the device may run until.

    On the clock, so no day start is needed: off at 01:30 breaks "may run until 02:00";
    off at 08:00 does not break "may run until 01:00".
    """
    if rule.kind != "latest_off":
        return False
    return 0 < (_minutes(rule.time) - _minutes(off)) % DAY <= DAY // 2


def _minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m
