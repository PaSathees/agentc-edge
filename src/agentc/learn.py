"""Learn each plug's own policy values from its readings.

A plug's power sits at a few levels: off, standby and active. Code finds them, then sets
"in use" and "unusual" half way between standby and active, so a 3 W desk lamp and a
100 W TV are judged on their own scale. The owner's own settings always win (config.py).
"""

import math
import statistics
from dataclasses import dataclass

OFF_W = 0.5  # at or below this, the plug is off: about a meter's noise floor
MIN_SHARE = 0.05  # a level needs at least 5% of the readings above off ...
MIN_RATIO = 2.0  # ... and active must be at least 2 x standby, or it is one level, not two


@dataclass(frozen=True)
class Levels:
    standby: float  # the middle watts of the low group
    active: float  # the middle watts of the high group


def levels(watts: list[float]) -> Levels | None:
    """The plug's standby and active levels. None if its readings don't show two."""
    on = sorted(w for w in watts if w > OFF_W)
    if len(on) < 2:
        return None
    i = _split(on)
    low, high = on[:i], on[i:]
    if min(len(low), len(high)) < MIN_SHARE * len(on):
        return None
    found = Levels(statistics.median(low), statistics.median(high))
    return found if found.active >= MIN_RATIO * found.standby else None


def values(lv: Levels) -> dict[str, tuple[float, str]]:
    """The policy values these levels give, each with why."""
    half = round((lv.active - lv.standby) / 2, 1)
    small = max(1.0, round(lv.standby / 2, 1))
    seen = f"standby {lv.standby:g} W, active {lv.active:g} W"
    return {
        "use_w": (half, f"half way from standby to active ({seen})"),
        "more_w": (half, f"half way from standby to active ({seen})"),
        "small_w": (small, f"half the standby, so standby left on counts ({seen})"),
    }


def _split(on: list[float]) -> int:
    """Where to cut the sorted readings into a low and a high group (on a log scale).

    The cut that makes each group as tight as it can be: the least spread inside both.
    """
    logs = [math.log(w) for w in on]
    total, squares = [0.0], [0.0]
    for x in logs:
        total.append(total[-1] + x)
        squares.append(squares[-1] + x * x)

    def spread(a: int, b: int) -> float:  # sum of squared distances from the mean, logs[a:b]
        s, n = total[b] - total[a], b - a
        return squares[b] - squares[a] - s * s / n

    n = len(logs)
    return min(range(1, n), key=lambda i: spread(0, i) + spread(i, n))
