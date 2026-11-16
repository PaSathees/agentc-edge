"""Checks every AI draft must pass before the owner sees it (guardrails, layer 3).

Cheap checks run first. The first one that fails blocks the draft and says why.
Soft rules never block: they only add a warning for the owner.
"""

import re
from dataclasses import dataclass

from agentc.options import DAY, Change, Option, Proposal, of

SENTENCES = 2  # the reason is 1 or 2 sentences
SAME_WITHIN = 30  # minutes: two changes are the same if action and days match too
NUMBER = re.compile(r"\d+(?:[.:]\d+)?")  # 12, 0.015, 01:30


@dataclass(frozen=True)
class Draft:
    option: str  # an option id, or "none"
    reason: str


@dataclass(frozen=True)
class Rule:
    kind: str  # "latest_off": the device may run until `time`; other kinds come later
    time: str  # local HH:MM
    hard: bool  # a hard rule blocks a draft; a soft one adds a warning
    text: str  # the owner's own words, shown with a warning


@dataclass(frozen=True)
class Verdict:
    blocked: str | None  # the first check that failed, and why; None = passed
    warnings: tuple[str, ...] = ()


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


def check(
    draft: Draft, *, options: list[Option], facts: str, access: str, slots: list[int],
    rules: list[Rule], rejected: list[Proposal],
) -> Verdict:  # fmt: skip
    """Run the checks in order (docs/design/guardrails.md).

    facts: the text the AI was given. rejected: this plug's proposals rejected in the
    last 14 days.
    """
    chosen = next((o for o in options if o.id == draft.option), None)
    if chosen is None and draft.option != "none":
        return Verdict(f"format: no option {draft.option!r}")
    sentences = [s for s in re.split(r"[.!?](?:\s|$)", draft.reason) if s.strip()]
    if not 1 <= len(sentences) <= SENTENCES:
        return Verdict("format: the reason must be 1 or 2 sentences")
    if missing := made_up(draft.reason, facts):
        return Verdict(f"numbers: not in the facts: {', '.join(missing)}")
    if chosen is None:
        return Verdict(None)  # "no action" changes nothing, so nothing else to check
    if access == "none":
        return Verdict("access: this device is never changed")
    if bad := [c.slot for c in chosen.changes if c.slot not in slots]:
        return Verdict(f"slots: timer {bad[0]} is not one the system may use")
    broken = [r for r in rules if _breaks(chosen.changes, r)]
    if hard := [r for r in broken if r.hard]:
        return Verdict(f"rule: {hard[0].text}")
    if any(_same(c, old) for c in chosen.changes for p in rejected for old in p.changes):
        return Verdict("repeat: the owner rejected the same change in the last 14 days")
    return Verdict(None, tuple(r.text for r in broken))


def _breaks(changes: tuple[Change, ...], rule: Rule) -> bool:
    """An off timer earlier than the time the owner said the device may run until."""
    if rule.kind != "latest_off":
        return False
    return any(c.action == "off" and of(c.time) < of(rule.time) for c in changes)


def _same(a: Change, b: Change) -> bool:
    """Same action and days, and times within 30 minutes of each other."""
    gap = (of(a.time) - of(b.time)) % DAY
    return a.action == b.action and a.days == b.days and min(gap, DAY - gap) <= SAME_WITHIN
