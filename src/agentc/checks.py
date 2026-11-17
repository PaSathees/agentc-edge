"""Checks every AI draft must pass before the owner sees it (guardrails, layer 3).

Cheap checks run first. The first one that fails blocks the draft and says why.
Soft rules never block: they only add a warning for the owner.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from agentc.config import Rule
from agentc.options import DAY, Change, Option, Proposal, of

SENTENCES = 2  # the reason is 1 or 2 sentences
SAME_WITHIN = 30  # minutes: two changes are the same if the action matches and days overlap
NUMBER = re.compile(r"\d+(?:[.:]\d+)?")  # 12, 0.015, 01:30


@dataclass(frozen=True)
class Draft:
    option: str  # an option id, or "none"
    reason: str


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
    draft: Draft, *, options: list[Option], facts: str, access: str, slots: Sequence[int],
    rules: Sequence[Rule], rejected: list[Proposal],
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
    if conflict := _conflict(chosen.changes, rules, rejected):
        return Verdict(conflict)
    return Verdict(None, tuple(r.text for r in rules if _breaks(chosen.changes, r)))


def allowed(option: Option, rules: Sequence[Rule], rejected: list[Proposal]) -> bool:
    """No hard rule broken and not a repeat: code drops other options before the model sees them."""
    return _conflict(option.changes, rules, rejected) is None


def _conflict(
    changes: tuple[Change, ...], rules: Sequence[Rule], rejected: list[Proposal]
) -> str | None:
    """Why these changes may never be offered: a hard rule broken, or a repeat. None if neither."""
    if hard := [r for r in rules if r.hard and _breaks(changes, r)]:
        return f"rule: {hard[0].text}"
    if any(_same(c, old) for c in changes for p in rejected for old in p.changes):
        return "repeat: the owner rejected the same change in the last 14 days"
    return None


def _breaks(changes: tuple[Change, ...], rule: Rule) -> bool:
    """An off timer earlier than the time the owner said the device may run until."""
    if rule.kind != "latest_off":
        return False
    return any(c.action == "off" and of(c.time) < of(rule.time) for c in changes)


def _same(a: Change, b: Change) -> bool:
    """Same action, a day in common, and times within 30 minutes of each other.

    So "off at 01:30 on weekdays" repeats a rejected "off at 01:30 every day".
    """
    gap = (of(a.time) - of(b.time)) % DAY
    overlap = int(a.days, 2) & int(b.days, 2)
    return a.action == b.action and overlap > 0 and min(gap, DAY - gap) <= SAME_WITHIN
