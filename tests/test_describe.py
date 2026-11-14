import pytest

from agentc.devices.base import Timer
from agentc.timers import describe


def t(action=1, time="07:30", days="1111111", mode=0, repeat=True, window=0, enable=True):
    return Timer(1, enable, mode, time, window, days, repeat, 1, action)


@pytest.mark.parametrize(
    ("timer", "words"),
    [
        (t(days="0111110"), "On at 07:30, Monday to Friday"),  # Plug-1 to 3 today
        (t(action=0, time="01:00"), "Off at 01:00, every day"),  # the TVs
        (t(days="1000001"), "On at 07:30, weekends"),
        (t(days="0101010"), "On at 07:30, Monday, Wednesday and Friday"),
        (t(days="0111100"), "On at 07:30, Monday to Thursday"),
        (t(days="-MTWTF-"), "On at 07:30, Monday to Friday"),  # letters also mean on
        (t(days="0000001"), "On at 07:30, Saturday"),
        (t(mode=1, time="00:00"), "On at sunrise, every day"),
        (t(mode=2, time="00:30"), "On at sunset (offset 00:30), every day"),
        (t(repeat=False), "On at 07:30, every day, once"),
        (t(window=10), "On at 07:30, every day, up to 10 min later"),
        (t(action=2), "Toggle at 07:30, every day"),
        (t(enable=False), "Not set"),
        (t(days="0000000"), "On at 07:30, no days"),
    ],
)
def test_describe(timer, words):
    assert describe(timer) == words
