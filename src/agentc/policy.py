"""Every threshold the agents decide with, in one place, each with why.

Defaults are here. config.toml can change them for the whole site ([policy]) or for one
device ([devices."Plug-6".policy]). Code reads only a device's own policy.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from typing import Any


def value(default: float, why: str) -> Any:  # noqa: ANN401 (a dataclass field)
    """A policy value with its reason, in plain words."""
    return field(default=default, metadata={"why": why})


@dataclass(frozen=True)
class Policy:
    # unusual use (the signals)
    factor: float = value(2.0, "Unusual: more than this many times the usual power for the hour")
    more_w: float = value(10.0, "... and at least this many watts more, so 0 W to 3 W is no alert")
    run_min: int = value(30, "... for at least this many minutes")
    small_w: float = value(3.0, "A small load left on: at least this many watts more ...")
    left_on_min: int = value(30, "... counts as left on after this many minutes")
    rarely_on: float = value(0.2, "Alert only at hours the plug is on for under this share of time")
    # the plug's usual week (profile)
    on_w: float = value(2.0, "Above this many watts, a plug counts as on")
    min_days: int = value(7, "Days of readings needed before a profile")
    # in use (learning standby, timer drift)
    use_w: float = value(10.0, "In use: at least this many watts above standby")
    # history (learning, the change check)
    history_days: int = value(14, "Days of readings behind learning and the change check")
    # automations (what the plug's own timers should have done)
    drift_h: float = value(3, "Drift: a timer switched it on, but it stayed idle this many hours")
    override_min: int = value(30, "Override: a timer switched it off, but it drew power this long")
    drift_days: int = value(3, "Ask about a drifting timer once it drifted on this many of 7 days")
    # questions to the owner (quiet hours are 23:30 to 07:00)
    night_wake_w: float = value(50, "At night, ask at once only if the device draws more than this")
    questions_a_day: int = value(5, "At most this many questions a day; the rest become notes")


DEFAULT = Policy()
NAMES = tuple(f.name for f in fields(Policy))


def override(base: Policy, values: dict[str, Any], *, where: str) -> Policy:
    """`base` with some values changed. A wrong name or a non-number fails here, at load."""
    if bad := sorted(set(values) - set(NAMES)):
        raise ValueError(f"{where}: unknown policy {', '.join(bad)}. Known: {', '.join(NAMES)}")
    if bad := sorted(k for k, v in values.items() if isinstance(v, bool) or
                     not isinstance(v, int | float)):  # fmt: skip
        raise ValueError(f"{where}: {', '.join(bad)} must be a number")
    return replace(base, **values)


def explain(
    policy: Policy, *, site: frozenset[str], device: frozenset[str] = frozenset(),
    learned: Mapping[str, str] | None = None,
) -> list[tuple[str, float, str, str]]:  # fmt: skip
    """Each value as (name, value, where it came from, why).

    site, device: the names the owner set in [policy] and in the device's own section.
    learned: the names learned from the plug's readings, each with why.
    """
    learned = learned or {}
    rows = []
    for f in fields(Policy):
        name = f.name
        source = ("device" if name in device else "site" if name in site else
                  "learned" if name in learned else "default")  # fmt: skip
        why = learned[name] if source == "learned" else f.metadata["why"]
        rows.append((name, getattr(policy, name), source, why))
    return rows
